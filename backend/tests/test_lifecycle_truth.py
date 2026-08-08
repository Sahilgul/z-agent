"""Wave 1 lifecycle-truth regression tests (backend side).

A2: kill/replace fails when the old container survives kill+force-stop.
A3: resume_run is guarded (active-run resume is an idempotent no-op) and
    kills/waits a still-live prior container before re-executing.
E1: reconcile never sweeps a run with a fresh heartbeat or a live container.
E3: the heartbeat reaper marks a stale-heartbeat thread failed ONLY when its
    container is confirmed gone.
E6: RunManager.shutdown drains tracked blueprint tasks.
K14: acked control delivery waits for the worker's ack key when the feature
    flag is on.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from app.db.models.approval import Approval
from app.db.models.run import Run
from app.db.models.thread import Thread
from app.orchestrator import run_manager as run_manager_mod
from app.orchestrator.run_manager import RunManager
from tests.test_orchestrator_run_manager import (
    _FakeControl,
    _FakeIngest,
    _FakeLaneManager,
    _FakeRelay,
)


def _make_manager():
    ingest, relay, control = _FakeIngest(), _FakeRelay(), _FakeControl()
    rm = RunManager(ingest, relay, _FakeLaneManager(), control)
    return rm, ingest, relay, control


# ------------------------------------------------------------------- A2

async def test_kill_replace_aborts_when_container_survives(session, make_user, monkeypatch):
    """A2: when the old container survives kill + force-stop, the replace
    must FAIL — spawning would double-mount the session volume."""
    u = make_user("a")
    rm, _, _, _ = _make_manager()
    run = Run(id="r1", created_by=u.id, mode="ask", stage="investigating")
    thread = Thread(id="l1", run_id="r1", persona="explorer", status="running",
                    container_id="c-old",
                    spawn_context={"prompt": "p", "persona_prompt": "pp"})
    session.add_all([run, thread])
    session.commit()

    spawned = []

    async def fake_spawn(*a, **k):
        spawned.append(1)
    rm.thread_manager.spawn = fake_spawn
    monkeypatch.setattr(run_manager_mod.sandbox_manager,
                        "wait_for_container_exit", lambda cid, timeout_s=15.0: False)

    # W-H8: the abort is a ValueError now (the intent API maps it to a 422
    # instead of a 500), and the thread must NOT be stamped "replaced" — the
    # stamp only lands after the old container's exit is verified.
    with pytest.raises(ValueError, match="survived kill"):
        await rm.kill_replace_thread("r1", "l1")
    assert spawned == []  # no replacement on a live old container
    session.expire_all()
    # The kill acked but the container won't die — "running" would show a
    # live tile for a thread that can never heartbeat again. Failed is honest.
    assert session.get(Thread, "l1").status == "failed"


# ------------------------------------------------------------------- W-H5

async def test_stop_run_stamps_pending_approvals_and_fans_out(session, make_user):
    """W-H5: stopping a run strands its pending approval cards — the worker's
    BLPOP is dead. The rows must be stamped (audit trail keeps 'stopped')
    and every open console gets approval_resolved so the zombie card drops."""
    u = make_user("a")
    rm, _, relay, _ = _make_manager()
    run = Run(id="r1", created_by=u.id, mode="ask", stage="investigating",
              available_actions=[])
    thread = Thread(id="l1", run_id="r1", persona="lead", status="running")
    session.add_all([
        run, thread,
        Approval(id="ap-1", run_id="r1", thread_id="l1", kind="tool", payload={}),
        # An already-decided card must NOT be re-stamped.
        Approval(id="ap-2", run_id="r1", thread_id="l1", kind="tool", payload={},
                 decision="deny", decided_at=datetime.now(UTC)),
        # Another run's card is out of scope.
        Approval(id="ap-3", run_id="r-other", thread_id="l9", kind="tool", payload={}),
    ])
    session.commit()

    await rm.stop_run("r1")

    session.expire_all()
    assert session.get(Approval, "ap-1").decision == "stopped"
    assert session.get(Approval, "ap-2").decision == "deny"  # untouched
    assert session.get(Approval, "ap-3").decision is None    # untouched
    resolved = [m for _, m in relay.fanouts if m.get("type") == "approval_resolved"]
    assert resolved == [{"type": "approval_resolved", "approval_id": "ap-1",
                         "decision": "stopped"}]


# ------------------------------------------------------------------- A3

async def test_resume_active_run_is_idempotent_noop(session, make_user):
    """A3: resuming an IN-FLIGHT run must not double-execute the blueprint."""
    u = make_user("a")
    rm, _, _, _ = _make_manager()
    run = Run(id="r1", created_by=u.id, mode="ask", stage="investigating")
    session.add(run)
    session.commit()
    result = await rm.resume_run("r1", u.id)
    assert result is not None and result.id == "r1"
    assert "r1" not in rm._tasks  # no second execution task
    session.expire_all()
    assert session.get(Run, "r1").stage == "investigating"  # untouched


async def test_resume_kills_live_prior_container(session, make_user, monkeypatch):
    """A3: a terminal run whose last thread still has a live container gets
    a kill + verified exit before the replacement executes."""
    u = make_user("a")
    rm, _, _, control = _make_manager()
    run = Run(id="r1", created_by=u.id, mode="ask", stage="interrupted",
              title="t")
    thread = Thread(id="l1", run_id="r1", persona="lead", status="running",
                    container_id="c-live")
    session.add_all([run, thread])
    session.commit()

    exits = []
    monkeypatch.setattr(run_manager_mod.sandbox_manager,
                        "wait_for_container_exit",
                        lambda cid, timeout_s=15.0: exits.append(cid) or True)
    executed = []
    monkeypatch.setattr(rm, "_execute",
                        lambda *a, **k: executed.append(a) or asyncio.sleep(0))

    result = await rm.resume_run("r1", u.id)
    assert result is not None
    assert "l1" in control.killed
    assert exits == ["c-live"]
    session.expire_all()
    assert session.get(Thread, "l1").status == "stopped"
    await asyncio.sleep(0)
    assert executed  # the resume re-executes the run


# ------------------------------------------------------------------- E1/E2

async def test_reconcile_preserves_healthy_run(session, make_user, monkeypatch):
    """E1: a run with a fresh heartbeat survives a backend restart."""
    u = make_user("a")
    rm, _, _, _ = _make_manager()
    run = Run(id="r1", created_by=u.id, mode="ask", stage="developing")
    thread = Thread(id="l1", run_id="r1", persona="dev", status="running",
                    heartbeat_at=datetime.now(UTC))
    session.add_all([run, thread])
    session.commit()
    count = await rm.reconcile_on_boot()
    assert count == 0
    session.expire_all()
    assert session.get(Run, "r1").stage == "developing"
    assert session.get(Thread, "l1").status == "running"


async def test_reconcile_preserves_live_container(session, make_user, monkeypatch):
    """E1: no fresh heartbeat but the container still runs -> not a zombie."""
    u = make_user("a")
    rm, _, _, _ = _make_manager()
    run = Run(id="r1", created_by=u.id, mode="ask", stage="developing")
    thread = Thread(id="l1", run_id="r1", persona="dev", status="running",
                    container_id="c1",
                    heartbeat_at=datetime.now(UTC) - timedelta(hours=1))
    session.add_all([run, thread])
    session.commit()
    monkeypatch.setattr(run_manager_mod.sandbox_manager,
                        "container_running", lambda cid: True)
    count = await rm.reconcile_on_boot()
    assert count == 0
    session.expire_all()
    assert session.get(Run, "r1").stage == "developing"


async def test_reconcile_sweeps_dead_run_and_stops_containers(session, make_user, monkeypatch):
    """A run with no heartbeat and no live container is swept — and its
    leftover containers are stopped for real (not DB-only)."""
    u = make_user("a")
    rm, _, _, _ = _make_manager()
    run = Run(id="r1", created_by=u.id, mode="ask", stage="developing")
    thread = Thread(id="l1", run_id="r1", persona="dev", status="running",
                    container_id="c1",
                    heartbeat_at=datetime.now(UTC) - timedelta(hours=1))
    session.add_all([run, thread])
    session.commit()
    exits = []
    monkeypatch.setattr(run_manager_mod.sandbox_manager,
                        "container_running", lambda cid: False)
    monkeypatch.setattr(run_manager_mod.sandbox_manager,
                        "wait_for_container_exit",
                        lambda cid, timeout_s=15.0: exits.append(cid) or True)
    count = await rm.reconcile_on_boot()
    assert count == 1
    assert exits == ["c1"]
    session.expire_all()
    assert session.get(Run, "r1").stage == "interrupted"
    assert session.get(Thread, "l1").status == "stopped"


# ------------------------------------------------------------------- E3

async def test_reaper_fails_thread_with_dead_container(session, make_user, monkeypatch):
    from app.services import heartbeats as hb_mod
    session.add(Thread(id="l1", run_id="r1", persona="dev", status="running",
                       container_id="c1",
                       heartbeat_at=datetime.now(UTC) - timedelta(minutes=10)))
    session.commit()
    # sandbox_manager is imported inside _reap_once; patch at the source.
    from app.sandbox.manager import sandbox_manager
    monkeypatch.setattr(sandbox_manager, "container_running", lambda cid: False)
    p = hb_mod.HeartbeatPersister.__new__(hb_mod.HeartbeatPersister)
    await p._reap_once()
    session.expire_all()
    t = session.get(Thread, "l1")
    assert t.status == "failed"
    assert t.finished_at is not None


async def test_reaper_leaves_thread_with_live_container(session, make_user, monkeypatch):
    from app.sandbox.manager import sandbox_manager
    from app.services import heartbeats as hb_mod
    session.add(Thread(id="l1", run_id="r1", persona="dev", status="running",
                       container_id="c1",
                       heartbeat_at=datetime.now(UTC) - timedelta(minutes=10)))
    session.commit()
    monkeypatch.setattr(sandbox_manager, "container_running", lambda cid: True)
    p = hb_mod.HeartbeatPersister.__new__(hb_mod.HeartbeatPersister)
    await p._reap_once()
    session.expire_all()
    assert session.get(Thread, "l1").status == "running"


# ------------------------------------------------------------------- E6

async def test_shutdown_drains_tracked_tasks():
    rm, _, _, _ = _make_manager()
    rm._tasks["r1"] = asyncio.create_task(asyncio.sleep(100))
    rm._tasks["r2"] = asyncio.create_task(asyncio.sleep(100))
    await rm.shutdown()
    for t in rm._tasks.values():
        assert t.done()


# ------------------------------------------------------------------- K14

async def test_control_ack_wait_roundtrip(fake_redis, monkeypatch):
    """With the flag on, interrupt waits for and finds the worker's ack key."""
    monkeypatch.setenv("COLLEGIUM_FEATURE_CONTROL_ACKS", "1")
    from app.core.config import get_settings
    get_settings.cache_clear()
    try:
        import json as _json

        from app.events import control as control_mod
        c = control_mod.LaneControl.__new__(control_mod.LaneControl)
        c.redis = fake_redis

        async def fake_worker():
            # Simulate the worker: read the published message, ack its id.
            await asyncio.sleep(0.05)
            _, payload = fake_redis.published[-1]
            msg_id = _json.loads(payload)["id"]
            await fake_redis.set(f"thread:t1:ack:{msg_id}", "interrupt")

        task = asyncio.create_task(fake_worker())
        monkeypatch.setattr(control_mod, "ACK_POLL_S", 0.01)
        acked = await c.interrupt("t1", wait_ack=True, ack_timeout_s=2.0)
        await task
        assert acked is True
    finally:
        get_settings.cache_clear()


async def test_control_ack_timeout_returns_false(fake_redis, monkeypatch):
    monkeypatch.setenv("COLLEGIUM_FEATURE_CONTROL_ACKS", "1")
    from app.core.config import get_settings
    get_settings.cache_clear()
    try:
        from app.events import control as control_mod
        c = control_mod.LaneControl.__new__(control_mod.LaneControl)
        c.redis = fake_redis
        monkeypatch.setattr(control_mod, "ACK_POLL_S", 0.01)
        acked = await c.interrupt("t1", wait_ack=True, ack_timeout_s=0.05)
        assert acked is False
    finally:
        get_settings.cache_clear()
