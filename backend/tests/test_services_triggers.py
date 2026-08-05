"""Triggers engine tests: normalizer, dedupe, fail-closed
identity, and all four guardrails — loop prevention, state flapping, bulk-edit
rate limit (queue + drain), gated-only trust. run_manager is a fake.
"""

import pytest

from app.db.models.thread import Thread
from app.db.models.run import Run
from app.db.models.trigger import Trigger, TriggerEventLog
from app.services import triggers
from zagent_contracts.triggers import TriggerEvent, TriggerSource


class FakeRM:
    def __init__(self):
        self.created = []
        self.nudges = []

    async def create_run(self, source, initiated_by, mode_name, task,
                         work_item_id=None, autonomy=None, **kw):
        import uuid
        run = type("R", (), {"id": str(uuid.uuid4())})()
        self.created.append({"id": run.id, "source": source, "by": initiated_by,
                             "mode": mode_name, "task": task,
                             "work_item_id": work_item_id, "autonomy": autonomy})
        return run

    async def nudge_thread(self, run_id, thread_id, text):
        self.nudges.append({"run_id": run_id, "thread_id": thread_id, "text": text})


def _event(descriptor="desc-ali", revision=3, state="zagent-plan", title="fix billing"):
    return TriggerEvent(
        source=TriggerSource.ADO_WEBHOOK, external_id="12345", revision=revision,
        event_type="work_item.updated", changed_by_descriptor=descriptor,
        payload={"state": state, "title": title})


def _trigger(session, name="plan-on-state", state="zagent-plan", mode="plan",
             autonomy="gated", owner="changed_by", rate=20):
    t = Trigger(name=name, source="ado_webhook",
                filter_json={"event_type": "work_item.updated", "state": state},
                mode=mode, autonomy=autonomy, owner_resolution=owner,
                rate_limit_per_hour=rate)
    session.add(t)
    session.commit()
    return t


@pytest.fixture
def bound_user(session, make_user):
    u = make_user()
    u.ado_descriptor = "desc-ali"
    u.status = "active"
    session.commit()
    return u


# ----------------------------------------------------------------- normalizer
def test_normalize_ado_work_item_payload():
    body = {"resource": {"workItemId": 12345, "rev": 7,
                         "revisedBy": {"id": "desc-ali"},
                         "fields": {"System.State": {"newValue": "zagent-plan"},
                                    "System.Title": {"newValue": "fix billing"}},
                         "_links": {"html": {"href": "https://ado/12345"}}}}
    ev = triggers.normalize_ado_work_item(body)
    assert ev.external_id == "12345" and ev.revision == 7
    assert ev.event_type == "work_item.updated"
    assert ev.changed_by_descriptor == "desc-ali"
    assert ev.payload["state"] == "zagent-plan"
    assert ev.payload["title"] == "fix billing"
    assert ev.idempotency_key == ("ado_webhook", "12345", 7)


def test_normalize_rejects_payload_without_id():
    with pytest.raises(triggers.TriggerError):
        triggers.normalize_ado_work_item({"resource": {"rev": 1}})


# --------------------------------------------------------------------- engine
async def test_matching_trigger_starts_gated_run_owned_by_resolver(session, bound_user):
    _trigger(session)
    rm = FakeRM()
    out = await triggers.process(_event(), rm)
    assert out["status"] == "matched"
    assert rm.created[0]["by"] == bound_user.id  # THEIR inbox, their steering
    assert rm.created[0]["autonomy"] == "gated"  # guardrail 4
    assert rm.created[0]["work_item_id"] == 12345
    assert "fix billing" in rm.created[0]["task"]
    log = session.query(TriggerEventLog).one()
    assert log.status == "matched" and log.run_id == rm.created[0]["id"]


async def test_unmatched_vocabulary_is_ignored(session, bound_user):
    _trigger(session, state="zagent-plan")
    out = await triggers.process(_event(state="some-other-state"), FakeRM())
    assert out == {"status": "ignored", "reason": "no_trigger_row"}
    assert session.query(TriggerEventLog).one().status == "ignored"


async def test_duplicate_revision_is_idempotent(session, bound_user):
    _trigger(session)
    rm = FakeRM()
    first = await triggers.process(_event(), rm)
    second = await triggers.process(_event(), rm)
    assert first["status"] == "matched"
    assert second == {"status": "duplicate"}
    assert len(rm.created) == 1


async def test_loop_prevention_ignores_service_account(session, bound_user, monkeypatch):
    monkeypatch.setattr(triggers.get_settings(), "service_account_descriptor", "desc-svc")
    _trigger(session)
    out = await triggers.process(_event(descriptor="desc-svc"), FakeRM())
    assert out == {"status": "ignored", "reason": "loop_prevention"}


async def test_identity_resolution_fail_closed(session, make_user):
    _trigger(session)
    rm = FakeRM()
    out = await triggers.process(_event(descriptor="desc-nobody"), rm)
    assert out["verdicts"][0]["status"] == "failed"
    assert out["verdicts"][0]["reason"] == "unresolved_identity"
    assert rm.created == []  # never attribute to a guess, never fall back


async def test_system_owner_only_by_explicit_row(session):
    from app.db.models.user import User
    session.add(User(username="system", display_name="sys", status="active", pin_hash="!"))
    session.commit()
    _trigger(session, owner="system")
    rm = FakeRM()
    out = await triggers.process(_event(descriptor=None), rm)
    assert out["status"] == "matched"
    assert rm.created[0]["by"] is not None


async def test_state_flapping_coalesces_into_nudge(session, bound_user):
    _trigger(session)
    rm = FakeRM()
    first = await triggers.process(_event(revision=3), rm)
    run_id = first["verdicts"][0]["run_id"]
    # an ACTIVE run exists for the same work item
    session.add(Run(id=run_id, created_by=bound_user.id, mode="plan", stage="planning"))
    session.add(Thread(id="thread-1", run_id=run_id, persona="lead", status="running"))
    session.commit()
    second = await triggers.process(_event(revision=4), rm)
    assert second["verdicts"][0]["status"] == "nudged"
    assert len(rm.created) == 1  # no second run
    assert rm.nudges[0]["run_id"] == run_id
    assert "rev 4" in rm.nudges[0]["text"]


async def test_rate_limit_queues_overflow(session, bound_user):
    _trigger(session, rate=1)
    rm = FakeRM()
    first = await triggers.process(_event(revision=3), rm)
    second = await triggers.process(_event(revision=4), rm)
    assert first["verdicts"][0]["status"] == "started"
    assert second["verdicts"][0]["status"] == "queued"
    assert len(rm.created) == 1


async def test_rate_limit_scoped_per_trigger_at_db_level(session, bound_user):
    """M-39 (coord D): the rate-limit check is scoped by trigger_name at DB
    level via the trigger_event_verdicts child table, not by loading every
    matched log and counting in Python. One event matching two triggers
    writes TWO verdict rows (one per trigger); each trigger's cap is checked
    against its OWN started verdicts, so a blast on trigger B can't push
    trigger A over its cap (per-trigger isolation), and the count is a
    server-side COUNT on the indexed trigger_name, not a Python scan."""
    from app.db.models.trigger import TriggerEventVerdict
    _trigger(session, name="A", rate=1)
    _trigger(session, name="B", rate=10)
    rm = FakeRM()
    out = await triggers.process(_event(revision=3), rm)
    # One event matched two triggers -> two started verdicts, two runs.
    started = [v for v in out["verdicts"] if v["status"] == "started"]
    assert len(started) == 2
    assert len(rm.created) == 2
    # The child table fans the multi-verdict log row out into per-trigger
    # rows (the core M-39 concern: a single log row carrying verdicts for
    # multiple triggers is countable per-trigger at DB level).
    rows = session.query(TriggerEventVerdict).all()
    assert len(rows) == 2
    assert {r.trigger_name for r in rows} == {"A", "B"}
    assert all(r.status == "started" for r in rows)
    # Per-trigger scoping: A (cap=1) is rate-limited by its own 1 started
    # verdict; B (cap=10) is NOT — B's count is 1, well under 10. The same
    # log row feeds both, but the child-table COUNT is per trigger_name, so
    # B's started verdict does NOT count against A and vice versa.
    a = session.query(Trigger).filter_by(name="A").one()
    b = session.query(Trigger).filter_by(name="B").one()
    assert triggers._rate_limited(a) is True
    assert triggers._rate_limited(b) is False


async def test_drain_queued_starts_when_capacity_returns(session, bound_user):
    _trigger(session, rate=1)
    rm = FakeRM()
    await triggers.process(_event(revision=3), rm)
    await triggers.process(_event(revision=4), rm)  # queued
    # time passes → the hourly window slides (simulate by aging the matched log)
    from datetime import datetime, timedelta, timezone
    log = session.query(TriggerEventLog).filter_by(status="matched").one()
    log.received_at = datetime.now(timezone.utc) - timedelta(hours=2)
    session.commit()
    started = await triggers.drain_queued(rm)
    assert len(started) == 1
    assert len(rm.created) == 2
    assert session.query(TriggerEventLog).filter_by(status="queued").count() == 0


async def test_drain_queued_passes_original_resolved_user_id(session, bound_user):
    """G-19: drain_queued must start the run as the QUEUED event's
    resolved_user_id (the resolver that owned the trigger at queue time),
    not a default system user — the run lands in the owner's inbox with
    their steering. The existing drain test verified the run started but
    not who it was initiated_by."""
    _trigger(session, rate=1)
    rm = FakeRM()
    await triggers.process(_event(revision=3), rm)   # matched (rate=1)
    await triggers.process(_event(revision=4), rm)   # queued (rate-limited)
    from datetime import datetime, timedelta, timezone
    log = session.query(TriggerEventLog).filter_by(status="matched").one()
    log.received_at = datetime.now(timezone.utc) - timedelta(hours=2)
    session.commit()
    await triggers.drain_queued(rm)
    # The drained run was initiated_by the original resolved_user_id.
    assert rm.created[-1]["by"] == bound_user.id
    assert rm.created[-1]["source"] == "trigger"


# ------------------------------------------------------------------ signature
def test_signature_verification(monkeypatch):
    import hashlib
    import hmac
    monkeypatch.setattr(triggers.get_settings(), "ado_webhook_secret", "s3cret")
    body = b'{"a":1}'
    sig = hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
    assert triggers.verify_signature(body, sig)
    assert triggers.verify_signature(body, f"sha256={sig}")
    assert not triggers.verify_signature(body, "wrong")
    assert not triggers.verify_signature(body, None)


def test_signature_fail_closed_without_secret(monkeypatch):
    monkeypatch.setattr(triggers.get_settings(), "ado_webhook_secret", "")
    assert not triggers.verify_signature(b"{}", "anything")
