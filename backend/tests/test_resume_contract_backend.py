"""Wave 2 resume-contract regression tests (backend side).

B1: the bus captures thread.session_id from the engine-identity event (no
    title coupling).
B2/C3/C6/C8: thread_env wires the custom-engine contract — per-run MODE
    (validated), RESUME_CONTEXT_ID, CHECKPOINT_MIRROR_DIR, timeout clocks,
    env-size ceiling.
B3: the custom engine's session volume mounts at /session (not /root/.claude).
C9: the boot watchdog marks a pre-heartbeat crash failed promptly.
K19: kill/replace propagates preserve_workspace from the spawn context.
"""

from __future__ import annotations

import pytest

from app.db.models.run import Run
from app.db.models.thread import Thread
from app.orchestrator import run_manager as run_manager_mod
from app.sandbox import manager as sb
from tests.test_orchestrator_run_manager import (
    _FakeControl,
    _FakeIngest,
    _FakeLaneManager,
    _FakeRelay,
)


def _make_run_thread(session, make_user, mode="ask", session_id=None):
    u = make_user("a")
    run = Run(id="r1", created_by=u.id, mode=mode, stage="investigating")
    thread = Thread(id="l1", run_id="r1", persona="researcher", status="running",
                    session_id=session_id)
    existing = session.get(Run, "r1")
    if existing is None:
        session.add(run)
    if session.get(Thread, "l1") is None:
        session.add(thread)
    session.commit()
    return run, thread


# ------------------------------------------------------------------- B2/C3/C6/C8

def test_thread_env_passes_run_mode_and_engine_contract(session, make_user):
    run, thread = _make_run_thread(session, make_user, mode="plan",
                                   session_id="ctx-7")
    env = sb.SandboxManager().thread_env(run, thread, "task", "persona",
                                         "default", writable=False)
    assert env["MODE"] == "plan"           # C3: per-run mode, not the default
    assert env["RESUME_CONTEXT_ID"] == "ctx-7"  # B2
    assert env["CHECKPOINT_MIRROR_DIR"] == "/session"  # B3
    assert env["APPROVAL_TIMEOUT_S"] == str(sb.get_settings().approval_timeout_seconds)
    assert env["IDLE_TTL_SECONDS"] == str(sb.get_settings().idle_ttl_seconds)


def test_thread_env_rejects_unknown_mode(session, make_user):
    run, thread = _make_run_thread(session, make_user, mode="invented-mode")
    with pytest.raises(sb.InvalidModeError):
        sb.SandboxManager().thread_env(run, thread, "task", "persona",
                                       "default", writable=False)


def test_thread_env_size_ceiling_fails_loudly(session, make_user, monkeypatch):
    run, thread = _make_run_thread(session, make_user)
    monkeypatch.setattr(sb.get_settings(), "max_env_payload_bytes", 1024)
    with pytest.raises(ValueError, match="env payload"):
        sb.SandboxManager().thread_env(run, thread, "x" * 5000, "persona",
                                       "default", writable=False)


def test_session_volume_mounts_at_session_for_custom_engine(session, make_user, monkeypatch):
    run, thread = _make_run_thread(session, make_user)
    captured = {}

    class _FakeContainer:
        id = "cid-1"
        short_id = "cid-1"

    class _FakeContainers:
        def run(self, image, environment=None, volumes=None, **kw):
            captured["volumes"] = volumes
            captured["env"] = environment
            return _FakeContainer()

    class _FakeClient:
        containers = _FakeContainers()

    monkeypatch.setattr(sb, "_docker", lambda: _FakeClient())
    sb.SandboxManager().run_thread_container(run, thread, "task", "persona",
                                             "default", None, [])
    binds = {v["bind"] for v in captured["volumes"].values()}
    assert "/session" in binds
    assert "/root/.claude" not in binds  # B3: no SDK leftover pretend-durability
    assert captured["env"]["CHECKPOINT_MIRROR_DIR"] == "/session"


# ------------------------------------------------------------------- C9

async def test_boot_watchdog_marks_preheartbeat_crash_failed(session, make_user, monkeypatch):
    from datetime import UTC, datetime, timedelta

    from app.orchestrator.thread_manager import ThreadManager
    tm = ThreadManager.__new__(ThreadManager)
    tm._boot_tasks = set()
    thread = Thread(id="l1", run_id="r1", persona="dev", status="running",
                    container_id="c-dead",
                    heartbeat_at=datetime.now(UTC) - timedelta(minutes=5))
    session.add(thread)
    session.commit()

    monkeypatch.setattr(sb.sandbox_manager, "container_running", lambda cid: False)
    relay = _FakeRelay()
    tm.relay = relay
    tm.gateway = None
    await tm._boot_watchdog("l1", "c-dead", grace_s=0.01)
    session.expire_all()
    assert session.get(Thread, "l1").status == "failed"
    assert relay.threads[-1] == ("r1", "l1", "failed")


async def test_boot_watchdog_leaves_live_container_alone(session, make_user, monkeypatch):
    from app.orchestrator.thread_manager import ThreadManager
    tm = ThreadManager.__new__(ThreadManager)
    tm._boot_tasks = set()
    thread = Thread(id="l1", run_id="r1", persona="dev", status="running",
                    container_id="c-live")
    session.add(thread)
    session.commit()
    monkeypatch.setattr(sb.sandbox_manager, "container_running", lambda cid: True)
    tm.relay = _FakeRelay()
    await tm._boot_watchdog("l1", "c-live", grace_s=0.01)
    session.expire_all()
    assert session.get(Thread, "l1").status == "running"


# ------------------------------------------------------------------- K19

async def test_kill_replace_propagates_preserve_workspace(session, make_user, monkeypatch):
    u = make_user("a")
    rm = run_manager_mod.RunManager(_FakeIngest(), _FakeRelay(),
                                    _FakeLaneManager(), _FakeControl())
    run = Run(id="r1", created_by=u.id, mode="ask", stage="investigating")
    thread = Thread(id="l1", run_id="r1", persona="dev", status="running",
                    container_id="c1",
                    spawn_context={"prompt": "p", "persona_prompt": "pp",
                                   "preserve_workspace": True})
    session.add_all([run, thread])
    session.commit()

    monkeypatch.setattr(run_manager_mod.sandbox_manager,
                        "wait_for_container_exit", lambda cid, timeout_s=15.0: True)
    captured = {}

    class _R:
        id = "thread-new"

    async def fake_spawn(run, persona, prompt, persona_prompt, writable_repo,
                         context_repos, resume_session=False,
                         resume_from_thread_id=None, preserve_workspace=False,
                         budget_usd=None, model=None, reasoning=None, images=None, image_notes=None):
        captured["preserve_workspace"] = preserve_workspace
        return _R()

    rm.thread_manager.spawn = fake_spawn
    await rm.kill_replace_thread("r1", "l1")
    assert captured["preserve_workspace"] is True


# ------------------------------------------------------------------- B1

async def test_bus_captures_engine_identity_without_title_coupling(session, make_user, fake_redis):
    from collegium_contracts import StepEvent, StepKind

    from tests.test_events_bus import _consumer
    _make_run_thread(session, make_user)
    c = _consumer(fake_redis)
    ev = StepEvent(
        run_id="r1", thread_id="l1", seq=0, kind=StepKind.STATUS,
        title="engine identity",
        detail={"kind": "engine_identity", "session_id": "ctx-42",
                "engine": "custom"},
    )
    await c._process("events:r1", "1-0", {"payload": ev.model_dump_json()}, "r1")
    session.expire_all()
    assert session.get(Thread, "l1").session_id == "ctx-42"


async def test_bus_ignores_unrelated_status_with_session_field(session, make_user, fake_redis):
    """Only identity-bearing events set session_id — a random STATUS event
    carrying a session_id-shaped detail must not clobber it."""
    from collegium_contracts import StepEvent, StepKind

    from tests.test_events_bus import _consumer
    _make_run_thread(session, make_user, session_id="ctx-orig")
    c = _consumer(fake_redis)
    ev = StepEvent(
        run_id="r1", thread_id="l1", seq=1, kind=StepKind.STATUS,
        title="some other status",
        detail={"session_id": "ctx-wrong"},
    )
    await c._process("events:r1", "1-0", {"payload": ev.model_dump_json()}, "r1")
    session.expire_all()
    assert session.get(Thread, "l1").session_id == "ctx-orig"
