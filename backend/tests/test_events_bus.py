import asyncio
import json
import uuid

import pytest
from zagent_contracts import StepEvent, StepKind

import app.db.base as db_base
from app.db.models.event import Event
from app.db.models.lane import Lane
from app.db.models.run import Run
from app.events import bus as bus_mod


def _consumer(fake_redis, relay=None):
    from tests.conftest import FakeRelay
    c = bus_mod.IngestConsumer.__new__(bus_mod.IngestConsumer)
    c.settings = bus_mod.get_settings()
    c.redis = fake_redis
    c.relay = relay or FakeRelay()
    c.run_streams = set()
    c._task = None
    return c


def _step(run_id="r1", lane_id="l1", seq=0, kind=StepKind.MESSAGE, uuid_=None):
    return StepEvent(run_id=run_id, lane_id=lane_id, seq=seq, kind=kind,
                      title="t", detail={"x": 1}, sdk_message_uuid=uuid_)


async def test_process_persists_event_and_acks(session, make_user, fake_redis):
    u = make_user("a")
    run = Run(id="r1", created_by=u.id, mode="ask", stage="investigating")
    lane = Lane(id="l1", run_id="r1", persona="researcher", status="running", next_seq=0)
    session.add_all([run, lane])
    session.commit()
    relay = _consumer(fake_redis).relay
    c = _consumer(fake_redis, relay)
    ev = _step(seq=5, uuid_="sdk-1")
    await c._process("events:r1", "1-0", {"payload": ev.model_dump_json()}, "r1")
    row = session.query(Event).one()
    assert row.seq == 5
    assert row.type == "message"
    assert row.sdk_message_uuid == "sdk-1"
    assert row.payload == {"x": 1}
    session.expire_all()
    lane_row = session.get(Lane, "l1")
    assert lane_row.next_seq == 6
    run_row = session.get(Run, "r1")
    assert run_row.last_active_at is not None
    assert any(m[0] == "r1" and m[1].get("type") == "step" for m in relay.published)


async def test_process_advances_next_seq_only_when_higher(session, make_user, fake_redis):
    u = make_user("a")
    run = Run(id="r1", created_by=u.id, mode="ask")
    lane = Lane(id="l1", run_id="r1", persona="researcher", status="running", next_seq=10)
    session.add_all([run, lane])
    session.commit()
    c = _consumer(fake_redis)
    ev = _step(seq=3)
    await c._process("events:r1", "1-0", {"payload": ev.model_dump_json()}, "r1")
    session.expire_all()
    assert session.get(Lane, "l1").next_seq == 10  # 3 < 10, no advance


async def test_process_captures_session_id_from_turn_complete(session, make_user, fake_redis):
    u = make_user("a")
    run = Run(id="r1", created_by=u.id, mode="ask", stage="investigating")
    lane = Lane(id="l1", run_id="r1", persona="researcher", status="running", next_seq=0)
    session.add_all([run, lane])
    session.commit()
    c = _consumer(fake_redis)
    ev = StepEvent(
        run_id="r1", lane_id="l1", seq=0, kind=StepKind.STATUS, title="turn complete",
        detail={"num_turns": 1, "duration_ms": 200, "is_error": False,
                "session_id": "sess-abc-123", "usage": {}},
        sdk_message_uuid="sdk-1",
    )
    await c._process("events:r1", "1-0", {"payload": ev.model_dump_json()}, "r1")
    session.expire_all()
    assert session.get(Lane, "l1").session_id == "sess-abc-123"


async def test_process_ignores_session_id_on_non_turn_complete_events(session, make_user, fake_redis):
    u = make_user("a")
    run = Run(id="r1", created_by=u.id, mode="ask", stage="investigating")
    lane = Lane(id="l1", run_id="r1", persona="researcher", status="running", next_seq=0)
    session.add_all([run, lane])
    session.commit()
    c = _consumer(fake_redis)
    # A status event that isn't "turn complete" must not write session_id even
    # if it happens to carry one — only the SDK's ResultMessage marks a turn
    # boundary, and that is the only session_id worth persisting.
    ev = StepEvent(
        run_id="r1", lane_id="l1", seq=0, kind=StepKind.STATUS, title="session init",
        detail={"session_id": "should-not-stick"},
    )
    await c._process("events:r1", "1-0", {"payload": ev.model_dump_json()}, "r1")
    session.expire_all()
    assert session.get(Lane, "l1").session_id is None


async def test_process_deadletters_malformed_payload(session, make_user, fake_redis):
    u = make_user("a")
    run = Run(id="r1", created_by=u.id, mode="ask")
    session.add(run)
    session.commit()
    c = _consumer(fake_redis)
    await c._process("events:r1", "1-0", {"payload": "{not json"}, "r1")
    assert "events:r1:deadletter" in fake_redis.streams
    dl = fake_redis.streams["events:r1:deadletter"]
    assert dl[0][1]["original_id"] == "1-0"
    assert "error" in dl[0][1]
    assert session.query(Event).count() == 0


async def test_process_deadletters_invalid_event(session, make_user, fake_redis):
    u = make_user("a")
    run = Run(id="r1", created_by=u.id, mode="ask")
    session.add(run)
    session.commit()
    c = _consumer(fake_redis)
    await c._process("events:r1", "1-0", {"payload": json.dumps({"run_id": "r1"})}, "r1")
    assert "events:r1:deadletter" in fake_redis.streams


async def test_process_missing_payload_key_deadletters(session, make_user, fake_redis):
    u = make_user("a")
    run = Run(id="r1", created_by=u.id, mode="ask")
    session.add(run)
    session.commit()
    c = _consumer(fake_redis)
    await c._process("events:r1", "1-0", {}, "r1")
    assert "events:r1:deadletter" in fake_redis.streams


def test_register_unregister_run(fake_redis):
    c = _consumer(fake_redis)
    c.register_run("r1")  # bare id normalizes to the real stream key
    assert "events:r1" in c.run_streams
    c.unregister_run("r1")
    assert "events:r1" not in c.run_streams


async def test_start_stop_lifecycle(fake_redis):
    c = _consumer(fake_redis)
    c._task = None
    await c.start()
    assert c._task is not None
    c._task.cancel()
    await c.stop()


async def test_ensure_group_creates_then_busygroup(fake_redis):
    c = _consumer(fake_redis)
    await c._ensure_group("events:r1")
    assert "events:r1" in fake_redis.streams
    await c._ensure_group("events:r1")  # idempotent


async def test_loop_processes_registered_stream(session, make_user, fake_redis):
    u = make_user("a")
    run = Run(id="r1", created_by=u.id, mode="ask")
    lane = Lane(id="l1", run_id="r1", persona="researcher", status="running")
    session.add_all([run, lane])
    session.commit()
    c = _consumer(fake_redis)
    c.register_run("events:r1")
    ev = _step(seq=1, uuid_="u1")
    await fake_redis.xadd("events:r1", {"payload": ev.model_dump_json()})
    task = asyncio.create_task(c._loop())
    for _ in range(200):  # poll until the step lands (≤1s), instead of parking 0.3s
        await asyncio.sleep(0.005)
        if any(m[1].get("type") == "step" for m in c.relay.published):
            break
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception as exc:
        raise AssertionError(f"loop raised: {exc!r}") from exc
    assert any(m[1].get("type") == "step" for m in c.relay.published)
    fresh = db_base.SessionLocal()
    try:
        assert fresh.query(Event).count() == 1
    finally:
        fresh.close()


async def test_loop_sleeps_when_no_streams(fake_redis, monkeypatch):
    c = _consumer(fake_redis)
    slept = {"n": 0}
    real_sleep = asyncio.sleep

    async def fake_sleep(t):
        slept["n"] += 1
        await real_sleep(0)
    monkeypatch.setattr(bus_mod.asyncio, "sleep", fake_sleep)
    task = asyncio.create_task(c._loop())
    await real_sleep(0)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert slept["n"] >= 1


async def test_init_constructs_redis_client():
    """Cover IngestConsumer.__init__ — redis.from_url builds a client object
    without opening a connection, so this is network-free."""
    from tests.conftest import FakeRelay
    c = bus_mod.IngestConsumer(FakeRelay())
    try:
        assert c.relay is not None
        assert c.run_streams == set()
        assert c._task is None
        assert c.redis is not None
    finally:
        await c.redis.aclose()


async def test_ensure_group_reraises_non_busygroup_error(fake_redis):
    c = _consumer(fake_redis)

    class _Err(Exception):
        pass

    async def boom(stream, group, id="0", mkstream=False):
        raise _Err("something else")
    fake_redis.xgroup_create = boom
    with pytest.raises(_Err):
        await c._ensure_group("events:r1")


async def test_loop_handles_xreadgroup_response_error(fake_redis, monkeypatch):
    c = _consumer(fake_redis)
    c.register_run("events:r1")
    real_sleep = asyncio.sleep

    async def fake_sleep(t):
        await real_sleep(0)

    async def boom(*a, **k):
        raise bus_mod.redis.ResponseError("transient")
    fake_redis.xreadgroup = boom
    monkeypatch.setattr(bus_mod.asyncio, "sleep", fake_sleep)
    task = asyncio.create_task(c._loop())
    await real_sleep(0)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
