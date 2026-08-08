"""Wave 4 Stream A regression tests: event integrity.

D1 unique (run_id, thread_id, seq) + idempotent consumer insert.
D3 dead-letter sweep alerts on growth.
D4 shutdown drain re-processes orphaned pending entries.
D6 relay-before-ack ordering.
D7 tenant-scoped publish_global.
"""


import pytest
import sqlalchemy as sa

from app.db.models.event import Event
from app.db.models.run import Run
from app.db.models.thread import Thread
from app.events import bus as bus_mod
from app.events import relay as relay_mod
from tests.test_events_bus import _consumer, _step


def _mk(session, make_user):
    u = make_user("a")
    session.add(Run(id="r1", created_by=u.id, mode="ask", stage="investigating"))
    session.add(Thread(id="l1", run_id="r1", persona="explorer", status="running"))
    session.commit()


# ------------------------------------------------------------------- D1

async def test_duplicate_event_redelivery_is_acked_not_duplicated(
        session, make_user, fake_redis):
    """D1: the same (run_id, thread_id, seq) arriving twice (at-least-once
    redelivery) stores ONE row and acks both times."""
    _mk(session, make_user)
    c = _consumer(fake_redis)
    ev = _step(run_id="r1", thread_id="l1", seq=7)
    payload = {"payload": ev.model_dump_json()}
    await c._process("events:r1", "1-0", payload, "r1")
    await c._process("events:r1", "2-0", payload, "r1")  # redelivery
    rows = session.query(Event).filter_by(run_id="r1", thread_id="l1").all()
    assert len(rows) == 1
    # Legacy frames (no event_uid) keep the worker's seq as the dedupe key —
    # the unique constraint is what makes this redelivery idempotent (D1).
    assert rows[0].seq == 7
    assert fake_redis.acked["events:r1"] == {"1-0", "2-0"}  # no pending buildup


def test_events_table_has_unique_run_thread_seq():
    cols = [c.name for c in Event.__table__.constraints
            if isinstance(c, sa.UniqueConstraint)]
    assert "uq_events_run_thread_seq" in cols


# ------------------------------------------------------------------- D3

async def test_deadletter_growth_alerts_and_notes_the_run(
        session, make_user, fake_redis):
    """D3: dead letters were write-only. On growth the sweep logs and pushes
    a note to the run owner; steady depth stays quiet."""
    _mk(session, make_user)

    class R:
        def __init__(self):
            self.notes = []

        async def publish_note(self, run_id, text):
            self.notes.append((run_id, text))

    relay = R()
    c = _consumer(fake_redis, relay)
    c.run_streams.add("events:r1")
    await fake_redis.xadd("events:r1:deadletter", {"error": "poison"})
    await c._scan_deadletters()
    assert relay.notes and "dead-lettered" in relay.notes[0][1]
    relay.notes.clear()
    await c._scan_deadletters()  # same depth -> quiet
    assert relay.notes == []
    await fake_redis.xadd("events:r1:deadletter", {"error": "another"})
    await c._scan_deadletters()
    assert len(relay.notes) == 1  # growth re-alerts


# ------------------------------------------------------------------- D4

async def test_unregister_drains_pending_entries(session, make_user, fake_redis):
    """D4: a message delivered-but-unacked at shutdown is claimed and
    processed by the drain instead of going dark with the process."""
    _mk(session, make_user)
    c = _consumer(fake_redis)
    ev = _step(run_id="r1", thread_id="l1", seq=3)
    await fake_redis.xadd("events:r1",
                          {"payload": ev.model_dump_json()})
    await fake_redis.xgroup_create("events:r1", bus_mod.GROUP, id="0",
                                   mkstream=True)
    # Read WITHOUT ack -> pending entry owned by the consumer.
    res = await fake_redis.xreadgroup(bus_mod.GROUP, bus_mod.CONSUMER,
                                      {"events:r1": ">"}, count=1)
    assert res
    await c._drain_run("events:r1")
    rows = session.query(Event).filter_by(run_id="r1", thread_id="l1").all()
    assert [r.seq for r in rows] == [3]  # legacy frame (no uid): worker seq kept


async def test_forwarder_caps_stream_length():
    """D4: worker xadd carries a MAXLEN cap — an unconsumed run can't grow
    Redis without bound. (Worker-side source probe; the behavioral half lives
    in worker/tests/test_event_integrity.py.)"""
    from pathlib import Path
    src = (Path(__file__).resolve().parents[2]
           / "worker" / "worker" / "forwarder.py").read_text()
    assert "maxlen=100_000" in src


# ------------------------------------------------------------------- D6

async def test_relay_failure_leaves_message_unacked(session, make_user, fake_redis):
    """D6: relay BEFORE ack — a WS-fanout crash must not ack the stream
    entry, or live delivery would be silently lost."""
    _mk(session, make_user)

    class BoomRelay:
        async def publish_step(self, run_id, event):
            raise RuntimeError("ws fanout exploded")

    c = _consumer(fake_redis, BoomRelay())
    ev = _step(run_id="r1", thread_id="l1", seq=1)
    with pytest.raises(RuntimeError, match="ws fanout exploded"):
        await c._process("events:r1", "1-0", {"payload": ev.model_dump_json()}, "r1")
    assert "1-0" not in fake_redis.acked.get("events:r1", set())
    # ...but the row IS durable — replay/redelivery converges.
    assert session.query(Event).filter_by(run_id="r1").count() == 1


# ------------------------------------------------------------------- D7

def test_publish_global_scoped_to_owner(fake_redis):
    r = relay_mod.Relay.__new__(relay_mod.Relay)
    r.redis = fake_redis
    r.subscribers = {}
    r._delta_tasks = {}
    r._queue_owner = {}
    import asyncio
    q_alice = asyncio.Queue()
    q_bob = asyncio.Queue()
    r.subscribers["run-a"] = {q_alice}
    r.subscribers["run-b"] = {q_bob}
    r._queue_owner[q_alice] = 1
    r._queue_owner[q_bob] = 2

    async def go():
        await r.publish_global({"type": "repo_added", "repo": "web"}, user_id=1)
        assert not q_alice.empty()
        assert q_bob.empty()  # no cross-tenant leak
        await r.publish_global({"type": "fleet_notice"})  # true broadcast
        assert not q_bob.empty()

    asyncio.run(go())


async def test_ws_subscribe_passes_user(session, make_user):
    """The WS endpoint is the only subscriber path; it must hand the
    authenticated user's id to the relay or D7 scoping is blind."""
    import inspect

    from app.ws import events as ws_mod
    src = inspect.getsource(ws_mod.run_events_ws)
    assert "user_id=user.id" in src
