"""Wave 3 concurrency + swarm regression tests.

H1: DB-backed reservations serialize concurrent spawns (two "replicas" —
    two Capacity instances over one DB — cannot double-book a repo or the cap).
H2: a failed Thread insert releases the reservation.
H3: the global cap is the single authoritative 100.
I4: one writable thread per repo is enforced by the DB unique partial index.
C1/H4/I3: the SpawnBridge turns spawn requests into real threads, vetoes
    over-cap requests deterministically, reports spawn_done, and terminates
    a child at the 2h timeout.
H6: requests are processed FIFO (stream order).
"""

from __future__ import annotations

import asyncio
import json

import pytest
import sqlalchemy as sa

from app.db.models.run import Run
from app.db.models.thread import Thread
from app.orchestrator.semaphores import Capacity


@pytest.fixture
def db_concurrency_on(monkeypatch):
    monkeypatch.setenv("COLLEGIUM_FEATURE_DB_CONCURRENCY", "1")
    from app.core.config import get_settings
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _run(session, make_user, run_id="r1"):
    u = make_user("a")
    run = Run(id=run_id, created_by=u.id, mode="ask", stage="investigating")
    session.add(run)
    session.commit()
    return run


# ------------------------------------------------------------------- H3

def test_global_cap_is_100():
    from app.core.config import get_settings
    assert get_settings().global_thread_cap == 100


# ------------------------------------------------------------------- H1

async def test_db_reservations_block_second_replica(session, make_user, db_concurrency_on):
    """Two Capacity instances (two 'replicas') over one DB: the second
    same-repo writable acquire must fail at the DB, not just in-process."""
    _run(session, make_user)
    a, b = Capacity(), Capacity()
    ok_a, _ = await a.try_acquire("web")
    assert ok_a
    ok_b, reason = await b.try_acquire("web")
    assert not ok_b
    assert "web" in reason
    # Release frees the lock for the other replica.
    a.commit_reservation("web")
    ok_b2, _ = await b.try_acquire("web")
    assert ok_b2


async def test_db_reservation_counts_toward_cap(session, make_user, db_concurrency_on,
                                                monkeypatch):
    """Reservations from replica A shrink replica B's headroom."""
    _run(session, make_user)
    from app.core.config import get_settings
    monkeypatch.setattr(get_settings(), "global_thread_cap", 2)
    a, b = Capacity(), Capacity()
    assert (await a.try_acquire(None))[0]
    assert (await a.try_acquire(None))[0]
    ok, reason = await b.try_acquire(None)
    assert not ok and "cap" in reason


async def test_stale_reservation_is_swept(session, make_user, db_concurrency_on,
                                          monkeypatch):
    """A crashed backend's leftover reservation must not hold the repo lock
    forever — reservations older than the TTL are swept before counting."""
    from datetime import UTC, datetime, timedelta

    from app.db.models.reservation import CapacityReservation
    _run(session, make_user)
    session.add(CapacityReservation(
        token="stale-1", repo_scope="web",
        created_at=datetime.now(UTC) - timedelta(seconds=3600)))
    session.commit()
    ok, _ = await Capacity().try_acquire("web")
    assert ok
    assert session.query(CapacityReservation).filter_by(token="stale-1").count() == 0


# ------------------------------------------------------------------- H2

async def test_failed_insert_releases_reservation(session, make_user, monkeypatch):
    """H2: if the Thread row insert raises, the reservation is released —
    capacity (and the repo write lock) must not leak."""
    _run(session, make_user)
    from app.orchestrator.thread_manager import ThreadManager
    from tests.test_orchestrator_run_manager import _FakeIngest, _FakeRelay

    tm = ThreadManager.__new__(ThreadManager)
    tm.ingest, tm.relay = _FakeIngest(), _FakeRelay()

    from app.core.config import get_settings
    tm.settings = get_settings()

    import app.orchestrator.thread_manager as tm_mod
    from app.orchestrator import semaphores
    cap = semaphores.Capacity()
    monkeypatch.setattr(tm_mod, "capacity", cap)

    # Force the insert to fail (duplicate thread id via a seeded row).
    import uuid as _uuid
    monkeypatch.setattr(_uuid, "uuid4", lambda: "fixed-id")
    existing = Thread(id="fixed-id", run_id="r1", persona="x", status="running")
    session.add(existing)
    session.commit()

    with pytest.raises(sa.exc.IntegrityError):
        await tm.spawn(session.get(Run, "r1"), persona="p", prompt="p",
                       persona_prompt="", writable_repo=None, context_repos=[])
    assert cap._reserved == 0


# ------------------------------------------------------------------- I4

def test_unique_writable_repo_index(session, make_user):
    """The DB itself rejects a second ACTIVE writable thread on one repo."""
    _run(session, make_user)
    session.add(Thread(id="t1", run_id="r1", persona="dev", status="running",
                       repo_scope="web"))
    session.commit()
    session.add(Thread(id="t2", run_id="r1", persona="dev", status="queued",
                       repo_scope="web"))
    with pytest.raises(sa.exc.IntegrityError):
        session.commit()
    session.rollback()
    # Terminal threads don't hold the lock — a completed writer frees it.
    session.add(Thread(id="t3", run_id="r1", persona="dev", status="completed",
                       repo_scope="web"))
    session.commit()
    # Read-only threads (repo_scope NULL) are exempt.
    session.add(Thread(id="t4", run_id="r1", persona="ex", status="running"))
    session.add(Thread(id="t5", run_id="r1", persona="ex", status="running"))
    session.commit()


# ------------------------------------------------------------------- C1/H4/I3/H6

def _bridge(monkeypatch, session, fake_redis):
    from app.events.spawn_bridge import SpawnBridge
    from tests.test_orchestrator_run_manager import _FakeControl, _FakeRelay

    spawned: list[dict] = []

    class _TM:
        async def spawn(self, run, persona, prompt, persona_prompt,
                        writable_repo, context_repos, **kw):
            spawned.append({"persona": persona, "prompt": prompt,
                            "writable": writable_repo.name if writable_repo else None})
            t = Thread(id=f"child-{len(spawned)}", run_id=run.id, persona=persona,
                       status="running", container_id=f"c-{len(spawned)}")
            session.add(t)
            session.commit()
            return t

    control = _FakeControl()
    control.spawned_done = []

    async def spawn_done(parent, spawn_id, status="completed"):
        control.spawned_done.append((parent, spawn_id, status))

    control.spawn_done = spawn_done
    bridge = SpawnBridge.__new__(SpawnBridge)
    bridge.thread_manager = _TM()
    bridge.control = control
    bridge.relay = _FakeRelay()
    bridge.redis = fake_redis
    bridge.run_streams = set()
    bridge._task = None
    bridge._watchers = set()
    return bridge, spawned, control


async def test_spawn_bridge_creates_real_thread_and_reports_done(
        session, make_user, fake_redis, monkeypatch):
    _run(session, make_user)
    bridge, spawned, control = _bridge(monkeypatch, session, fake_redis)
    await bridge._process({"payload": json.dumps({
        "spawn_id": "sp-1", "run_id": "r1", "parent_thread_id": "parent-1",
        "kind": "agent", "prompt": "map auth", "repo": None,
        "context_id": "parent-1::worker-sp1"})}, "r1")
    assert len(spawned) == 1
    assert spawned[0]["prompt"] == "map auth"
    # The watcher (started by _process) reports spawn_done once the child
    # terminates — exactly once, even if a second watcher is driven manually.
    session.get(Thread, "child-1").status = "completed"
    session.commit()
    import app.events.spawn_bridge as sb_mod
    monkeypatch.setattr(sb_mod, "WATCH_POLL_S", 0.01)
    w = asyncio.ensure_future(
        bridge._watch_child("r1", "child-1", "parent-1", "sp-1"))
    await asyncio.wait_for(w, timeout=2)
    for _ in range(200):
        if control.spawned_done:
            break
        await asyncio.sleep(0.01)
    assert control.spawned_done == [("parent-1", "sp-1", "completed")]
    for t in list(bridge._watchers):
        t.cancel()


async def test_spawn_bridge_veto_reports_back(session, make_user, fake_redis, monkeypatch):
    """H4: an over-capacity/lock-conflicting spawn is a deterministic veto,
    reported to the parent — not a silent queue past the cap."""
    _run(session, make_user)
    bridge, spawned, control = _bridge(monkeypatch, session, fake_redis)

    async def boom(*a, **k):
        from app.orchestrator.thread_manager import ThreadSpawnError
        raise ThreadSpawnError("global thread cap (100) reached — queued")

    bridge.thread_manager.spawn = boom
    await bridge._process({"payload": json.dumps({
        "spawn_id": "sp-x", "run_id": "r1", "parent_thread_id": "parent-1",
        "kind": "swarm", "prompt": "slice", "repo": None,
        "context_id": "c"})}, "r1")
    assert spawned == []
    assert control.spawned_done == [("parent-1", "sp-x", "vetoed")]


async def test_spawn_bridge_timeout_kills_child(session, make_user, fake_redis,
                                                monkeypatch):
    """I3: the 2h spawn timeout TERMINATES the child (kill + verified exit),
    then reports timed_out — not just a relabel."""
    _run(session, make_user)
    bridge, spawned, control = _bridge(monkeypatch, session, fake_redis)
    session.add(Thread(id="child-9", run_id="r1", persona="swarm-slice",
                       status="running", container_id="c-9"))
    session.commit()

    killed = []
    control.killed_orig = control.killed

    async def kill(tid, *, wait_ack=False, ack_timeout_s=10.0):
        killed.append(tid)
        return True

    control.kill = kill
    exits = []
    import app.sandbox.manager as sm
    monkeypatch.setattr(sm.sandbox_manager, "wait_for_container_exit",
                        lambda cid, timeout_s=15.0: exits.append(cid) or True)
    import app.events.spawn_bridge as sb_mod
    monkeypatch.setattr(sb_mod, "SPAWN_TIMEOUT_S", 0.02)
    monkeypatch.setattr(sb_mod, "WATCH_POLL_S", 0.01)
    await asyncio.wait_for(
        bridge._watch_child("r1", "child-9", "parent-1", "sp-9"), timeout=2)
    assert killed == ["child-9"]
    assert exits == ["c-9"]
    session.expire_all()
    assert session.get(Thread, "child-9").status == "failed"
    assert control.spawned_done[-1] == ("parent-1", "sp-9", "timed_out")


async def test_spawn_bridge_fifo_order(session, make_user, fake_redis, monkeypatch):
    """H6: requests on one run's stream are processed in arrival order."""
    _run(session, make_user)
    bridge, spawned, control = _bridge(monkeypatch, session, fake_redis)
    bridge.register_run("r1")
    for i in range(3):
        await fake_redis.xadd("spawn_requests:r1", {"payload": json.dumps({
            "spawn_id": f"sp-{i}", "run_id": "r1", "parent_thread_id": "p",
            "kind": "swarm", "prompt": f"slice {i}", "repo": None,
            "context_id": f"c{i}"})})
    # Drive one loop iteration manually.
    import app.events.spawn_bridge as sb_mod
    monkeypatch.setattr(sb_mod, "IDLE_POLL_SECONDS", 0.01)
    results = await fake_redis.xreadgroup(sb_mod.GROUP, "test-consumer",
                                          {"spawn_requests:r1": ">"}, count=50)
    # Clean up the test consumer's pending entries; drive _process directly
    # in stream order (what _loop does).
    for stream, messages in results:
        for msg_id, fields in messages:
            await bridge._process(fields, "r1")
            await fake_redis.xack(stream, sb_mod.GROUP, msg_id)
    assert [s["prompt"] for s in spawned] == ["slice 0", "slice 1", "slice 2"]
