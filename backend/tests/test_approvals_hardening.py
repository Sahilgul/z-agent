"""Wave 4 Stream C regression tests: approval hardening.

G1 decision vocabulary reconciled both directions (edited_allow round-trips;
deny_tool translates worker-side).
G3 decide takes a row lock, enforces expiry, and Redis decision keys TTL.
G6 re-driven cards are idempotent; expired cards stamp timeout, not 409.
G7 consumer reclaims orphaned pending entries on (re)registration.
G8 the run stage is re-published after a decision resolves.
"""

import json
from datetime import UTC, datetime, timedelta

import pytest

from app.db.models.approval import Approval
from app.db.models.run import Run
from tests.test_services_approvals import _svc


class _Ctl:
    def __init__(self):
        self.resolved: list[tuple] = []

    async def resolve_approval(self, approval_id, decision, reason="",
                               edited_args=None):
        self.resolved.append((approval_id, decision, reason, edited_args))


class _Relay:
    def __init__(self):
        self.stages: list[tuple] = []
        self.fanouts: list[dict] = []

    async def publish_run_stage(self, run_id, stage, actions):
        self.stages.append((run_id, stage))

    async def _fanout(self, run_id, message):
        self.fanouts.append(message)


def _card(session, make_user, *, expired=False, decision=None):
    u = make_user("a")
    if session.get(Run, "r1") is None:
        session.add(Run(id="r1", created_by=u.id, mode="ask", stage="investigating"))
    a = Approval(
        id="ap1", run_id="r1", thread_id="l1", kind="tool",
        payload={"tool": "terminal_exec", "input": {"command": "ls"}},
        expires_at=(datetime.now(UTC) + timedelta(
            seconds=-5 if expired else 900)),
        decision=decision,
        decided_at=datetime.now(UTC) if decision else None,
    )
    session.add(a)
    session.commit()
    return a


# ------------------------------------------------------------------- G1

async def test_deny_tool_translates_for_worker(session, make_user, fake_redis):
    """G1: the audit row keeps 'deny_tool' but the worker receives 'deny'
    with the intent in the reason — no unknown-decision silent degrade."""
    _card(session, make_user)
    ctl, relay = _Ctl(), _Relay()
    svc = _svc(fake_redis, relay, ctl)
    await svc.decide("ap1", "deny_tool", decided_by=1)
    assert session.get(Approval, "ap1").decision == "deny_tool"  # audit verbatim
    assert ctl.resolved[0][1] == "deny"                          # worker vocabulary
    assert "deny_tool" in ctl.resolved[0][2]                     # intent preserved


async def test_edited_allow_round_trips_with_args(session, make_user, fake_redis):
    _card(session, make_user)
    ctl = _Ctl()
    svc = _svc(fake_redis, _Relay(), ctl)
    await svc.decide("ap1", "edited_allow", decided_by=1,
                     edited_args={"command": "ls -la"})
    assert ctl.resolved[0] == ("ap1", "edited_allow", "", {"command": "ls -la"})


# ------------------------------------------------------------------- G3/G6

async def test_double_decide_same_is_idempotent(session, make_user, fake_redis):
    """G6: a retried click on a stale card must not 409 the human."""
    _card(session, make_user)
    svc = _svc(fake_redis, _Relay(), _Ctl())
    a1 = await svc.decide("ap1", "allow_once", decided_by=1)
    a2 = await svc.decide("ap1", "allow_once", decided_by=1)
    assert a1.decision == a2.decision == "allow_once"
    assert len(svc.control.resolved) == 1  # published once


async def test_double_decide_different_conflicts(session, make_user, fake_redis):
    _card(session, make_user)
    svc = _svc(fake_redis, _Relay(), _Ctl())
    await svc.decide("ap1", "allow_once", decided_by=1)
    with pytest.raises(ValueError, match="already decided"):
        await svc.decide("ap1", "deny", decided_by=2)


async def test_decide_after_expiry_stamps_timeout_not_decision(
        session, make_user, fake_redis):
    """G3: the worker's BLPOP already gave up — a late decide must not RPUSH
    into the void. The card records 'timeout' and resolves idempotently."""
    _card(session, make_user, expired=True)
    ctl = _Ctl()
    svc = _svc(fake_redis, _Relay(), ctl)
    a = await svc.decide("ap1", "allow_once", decided_by=1)
    assert a.decision == "timeout"
    assert ctl.resolved == []  # nothing published to a dead BLPOP


# ------------------------------------------------------------------- G8

async def test_decide_republishes_real_run_stage(session, make_user, fake_redis):
    """G8: the card painted 'awaiting_user'; resolving must restore the real
    stage so UI and DB agree."""
    _card(session, make_user)
    relay = _Relay()
    svc = _svc(fake_redis, relay, _Ctl())
    await svc.decide("ap1", "allow_once", decided_by=1)
    assert ("r1", "investigating") in relay.stages


# ------------------------------------------------------------------- G7

async def test_reclaim_processes_orphaned_pending(session, make_user, fake_redis):
    """G7: a message pending under the consumer group from a previous
    backend process is claimed and turned into a card on re-registration."""
    u = make_user("a")
    session.add(Run(id="r1", created_by=u.id, mode="ask", stage="investigating"))
    session.commit()
    svc = _svc(fake_redis, _Relay(), _Ctl())
    await fake_redis.xadd("approvals:r1", {
        "approval_id": "ap-orphan", "thread_id": "l1", "kind": "tool",
        "payload": json.dumps({"tool": "file_write"})})
    await fake_redis.xgroup_create("approvals:r1", "approvals", id="0",
                                   mkstream=True)
    # Deliver but never ack — the orphan.
    res = await fake_redis.xreadgroup("approvals", "backend-1",
                                      {"approvals:r1": ">"}, count=1)
    assert res
    svc.run_streams.add("r1")
    # One loop iteration's reclaim phase:
    stream = "approvals:r1"
    claimed = await fake_redis.xautoclaim(stream, "approvals", "backend-1",
                                          min_idle_time=0, start_id="0-0",
                                          count=100)
    for msg_id, fields in claimed[1]:
        await svc._create_card("r1", fields)
        await fake_redis.xack(stream, "approvals", msg_id)
    card = session.get(Approval, "ap-orphan")
    assert card is not None and card.run_id == "r1"
