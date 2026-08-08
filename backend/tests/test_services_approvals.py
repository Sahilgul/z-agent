import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from app.db.models.approval import Approval
from app.db.models.run import Run
from app.services.approvals import ApprovalService


def _svc(fake_redis, relay=None, control=None):
    from tests.conftest import FakeControl, FakeRelay
    svc = ApprovalService.__new__(ApprovalService)
    svc.relay = relay or FakeRelay()
    svc.control = control or FakeControl()
    svc.redis = fake_redis
    svc.run_streams = set()
    svc._task = None
    svc._last_sweep = datetime.now(UTC)
    svc._claimed = set()
    return svc


def test_register_unregister_run(fake_redis):
    svc = _svc(fake_redis)
    svc.register_run("r1")
    assert "r1" in svc.run_streams
    svc.unregister_run("r1")
    assert "r1" not in svc.run_streams


async def test_create_card_persists_and_fans_out(session, make_user, fake_redis):
    u = make_user("a")
    run = Run(id="r-c", created_by=u.id, mode="ask", stage="awaiting_user")
    session.add(run)
    session.commit()
    svc = _svc(fake_redis)
    await svc._create_card("r-c", {
        "approval_id": "ap-1", "thread_id": "l-1", "kind": "tool",
        "payload": '{"x": 1}',
    })
    row = session.query(Approval).one()
    assert row.id == "ap-1"
    assert row.kind == "tool"
    assert row.payload == {"x": 1}
    assert row.thread_id == "l-1"
    assert any(m[0] == "r-c" and m[1].get("type") == "run_stage" for m in svc.relay.published)
    assert any(m[1].get("type") == "approval_card" for m in svc.relay.published)


async def test_create_card_defaults_kind_and_payload(session, make_user, fake_redis):
    u = make_user("a")
    run = Run(id="r-c2", created_by=u.id, mode="ask")
    session.add(run)
    session.commit()
    svc = _svc(fake_redis)
    await svc._create_card("r-c2", {})
    row = session.query(Approval).one()
    assert row.kind == "tool"
    assert row.payload == {}


async def test_decide_records_and_publishes(session, make_user, fake_redis):
    u = make_user("a")
    run = Run(id="r-d", created_by=u.id, mode="ask")
    session.add(run)
    session.commit()
    ap = Approval(id="ap-d", run_id="r-d", kind="plan")
    session.add(ap)
    session.commit()
    svc = _svc(fake_redis)
    out = await svc.decide("ap-d", "approved", u.id, "ok")
    assert out.decision == "approved"
    assert out.decided_by == u.id
    assert out.decided_at is not None
    assert any(c[0] == "approval:ap-d:decision" for c in svc.control.calls)


async def test_decide_double_decide_raises(session, make_user, fake_redis):
    u = make_user("a")
    run = Run(id="r-d2", created_by=u.id, mode="ask")
    session.add(run)
    session.commit()
    ap = Approval(id="ap-d2", run_id="r-d2", kind="plan", decision="approved")
    session.add(ap)
    session.commit()
    svc = _svc(fake_redis)
    with pytest.raises(ValueError):
        await svc.decide("ap-d2", "denied", u.id)


async def test_decide_missing_raises(session, fake_redis):
    svc = _svc(fake_redis)
    with pytest.raises(ValueError):
        await svc.decide("ghost", "approved", 1)


async def test_start_stop_lifecycle(fake_redis):
    svc = _svc(fake_redis)
    svc._task = None
    await svc.start()
    assert svc._task is not None
    svc._task.cancel()
    await svc.stop()


async def test_init_constructs_redis_client():
    """Cover ApprovalService.__init__ — redis.from_url builds a client without
    opening a connection, so this is network-free."""
    from tests.conftest import FakeControl, FakeRelay
    svc = ApprovalService(FakeRelay(), FakeControl())
    try:
        assert svc.relay is not None
        assert svc.control is not None
        assert svc.run_streams == set()
        assert svc._task is None
        assert svc.redis is not None
    finally:
        await svc.redis.aclose()


async def test_loop_processes_approval_card(session, make_user, fake_redis):
    u = make_user("a")
    run = Run(id="r-lp", created_by=u.id, mode="ask", stage="investigating")
    session.add(run)
    session.commit()
    svc = _svc(fake_redis)
    svc.register_run("r-lp")
    await fake_redis.xadd("approvals:r-lp", {
        "approval_id": "ap-lp", "thread_id": "l-1", "kind": "tool",
        "payload": '{"k": 1}',
    })
    task = asyncio.create_task(svc._loop())
    for _ in range(200):  # poll until the card lands (≤1s), instead of parking 0.3s
        await asyncio.sleep(0.005)
        if any(m[1].get("type") == "approval_card" for m in svc.relay.published):
            break
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    row = session.query(Approval).filter_by(id="ap-lp").one()
    assert row.kind == "tool"
    assert row.payload == {"k": 1}
    assert any(m[1].get("type") == "approval_card" for m in svc.relay.published)


async def test_create_card_stamps_expiry(session, make_user, fake_redis):
    u = make_user("a")
    session.add(Run(id="r-exp", created_by=u.id, mode="ask"))
    session.commit()
    svc = _svc(fake_redis)
    await svc._create_card("r-exp", {"approval_id": "ap-exp"})
    row = session.query(Approval).filter_by(id="ap-exp").one()
    assert row.expires_at is not None


async def test_expire_stale_times_out_and_fans_out(session, make_user, fake_redis):
    """The worker's BLPOP has already denied these; the card must not linger."""
    u = make_user("a")
    session.add(Run(id="r-st", created_by=u.id, mode="ask"))
    session.commit()
    past = datetime.now(UTC) - timedelta(minutes=5)
    session.add(Approval(id="ap-old", run_id="r-st", kind="tool", expires_at=past))
    session.add(Approval(id="ap-live", run_id="r-st", kind="tool",
                         expires_at=datetime.now(UTC) + timedelta(minutes=5)))
    session.commit()

    svc = _svc(fake_redis)
    svc._last_sweep = datetime.now(UTC) - timedelta(hours=1)
    await svc._expire_stale()

    session.expire_all()
    assert session.query(Approval).filter_by(id="ap-old").one().decision == "timeout"
    assert session.query(Approval).filter_by(id="ap-live").one().decision is None
    assert any(m[1].get("type") == "approval_resolved" for m in svc.relay.published)


async def test_expire_stale_throttles_between_sweeps(session, make_user, fake_redis):
    u = make_user("a")
    session.add(Run(id="r-th", created_by=u.id, mode="ask"))
    session.commit()
    session.add(Approval(id="ap-th", run_id="r-th", kind="tool",
                         expires_at=datetime.now(UTC) - timedelta(minutes=5)))
    session.commit()
    svc = _svc(fake_redis)  # _last_sweep = now, so this tick is a no-op
    await svc._expire_stale()
    session.expire_all()
    assert session.query(Approval).filter_by(id="ap-th").one().decision is None


async def test_expire_stale_swallows_db_error(fake_redis, monkeypatch):
    """G-17: a SQLAlchemyError during the stale sweep must not kill the
    consumer — it's logged and swallowed (a sweep hiccup is transient;
    the next tick retries). Covers the except-SQLAlchemyError branch."""
    from sqlalchemy.exc import SQLAlchemyError

    import app.services.approvals as approvals_mod

    class _BoomSession:
        def query(self, *a, **k):
            raise SQLAlchemyError("simulated sweep failure")
        def close(self):
            pass

    monkeypatch.setattr(approvals_mod, "get_session", lambda: _BoomSession())
    svc = _svc(fake_redis)
    svc._last_sweep = datetime.now(UTC) - timedelta(hours=1)
    # Must not raise — the SQLAlchemyError is swallowed (logged + return).
    await svc._expire_stale()


async def test_loop_sleeps_when_no_streams(fake_redis, monkeypatch):
    svc = _svc(fake_redis)
    real_sleep = asyncio.sleep

    async def fake_sleep(t):
        await real_sleep(0)
    monkeypatch.setattr("app.services.approvals.asyncio.sleep", fake_sleep)
    task = asyncio.create_task(svc._loop())
    await real_sleep(0)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


async def test_loop_busygroup_is_ignored(session, make_user, fake_redis, monkeypatch):
    import redis.asyncio as redis
    u = make_user("a")
    run = Run(id="r-bg", created_by=u.id, mode="ask")
    session.add(run); session.commit()
    svc = _svc(fake_redis)
    svc.register_run("r-bg")

    call_count = {"n": 0}
    real_xgroup = fake_redis.xgroup_create

    async def flaky_xgroup(stream, group, id="0", mkstream=False):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise redis.ResponseError("BUSYGROUP Group already exists")
        return await real_xgroup(stream, group, id=id, mkstream=mkstream)

    fake_redis.xgroup_create = flaky_xgroup
    real_sleep = asyncio.sleep

    async def fake_sleep(t):
        await real_sleep(0)
    monkeypatch.setattr("app.services.approvals.asyncio.sleep", fake_sleep)
    task = asyncio.create_task(svc._loop())
    await real_sleep(0)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
