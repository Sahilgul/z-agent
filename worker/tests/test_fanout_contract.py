"""Contract tests for Phase 6 fan-out (plan §17)."""

from __future__ import annotations

import pytest

from worker.engine.fanout import (
    SPAWN_STAGGER_S,
    SWARM_MAX_SLICES,
    THREAD_TIMEOUT_S,
    enforce_timeout,
    get_registry,
    hydrate_orientation,
    reset_registry,
    spawn_agent,
    spawn_swarm,
)


@pytest.fixture(autouse=True)
def _clean_registry():
    reset_registry()
    yield
    reset_registry()


def test_spawn_agent_succeeds_when_worker_idle(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("THREAD_ID", "thread-1")
    result = spawn_agent.invoke({"prompt": "investigate the dedupe bug"})
    assert result.startswith("spawned agent")
    reg = get_registry()
    assert reg.live_count() == 1


def test_spawn_agent_vetoed_when_worker_saturated():
    """The engine-side veto refuses spawns when the worker pool is saturated."""
    reg = get_registry()
    # Saturate the registry
    for i in range(SWARM_MAX_SLICES):
        reg.register(f"s{i}", "swarm", "thread-1", f"ctx-{i}", "p")
    # Now spawn_agent should be vetoed (worker idle = false)
    from worker.engine.fanout import SpawnRequest, _veto
    req = SpawnRequest(kind="agent", prompt="x")
    allowed, reason = _veto(req, worker_idle=False)
    assert allowed is False
    assert "saturated" in reason


def test_spawn_swarm_rejects_duplicate_slices(monkeypatch: pytest.MonkeyPatch):
    """§4: swarm slices must be DISTINCT — duplicate prompts are rejected."""
    monkeypatch.setenv("THREAD_ID", "thread-1")
    slices = [
        {"title": "a", "prompt": "do the same thing"},
        {"title": "b", "prompt": "do the same thing"},  # duplicate
    ]
    result = spawn_swarm.invoke({"slices": slices})
    assert result.startswith("error: swarm vetoed")
    assert "DISTINCT" in result or "duplicate" in result


def test_spawn_swarm_rejects_over_width(monkeypatch: pytest.MonkeyPatch):
    """Max width cap: 8 slices."""
    monkeypatch.setenv("THREAD_ID", "thread-1")
    slices = [{"title": f"s{i}", "prompt": f"distinct prompt {i}"} for i in range(SWARM_MAX_SLICES + 1)]
    result = spawn_swarm.invoke({"slices": slices})
    assert "exceeds cap" in result


def test_spawn_swarm_registers_all_slices(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("THREAD_ID", "thread-1")
    slices = [{"title": f"s{i}", "prompt": f"distinct prompt {i}"} for i in range(3)]
    result = spawn_swarm.invoke({"slices": slices})
    assert "spawned swarm of 3" in result
    reg = get_registry()
    assert reg.live_count() == 3


def test_cascade_drain_stops_child_spawns(monkeypatch: pytest.MonkeyPatch):
    """If the parent thread stops, all its spawns are drained in cascade."""
    monkeypatch.setenv("THREAD_ID", "thread-1")
    spawn_agent.invoke({"prompt": "child 1"})
    spawn_agent.invoke({"prompt": "child 2"})
    reg = get_registry()
    assert reg.live_count() == 2
    drained = reg.drain("thread-1")
    assert len(drained) == 2
    assert reg.live_count() == 0


def test_cascade_drain_only_affects_parent_children(monkeypatch: pytest.MonkeyPatch):
    """Draining one parent doesn't touch another parent's spawns."""
    monkeypatch.setenv("THREAD_ID", "thread-1")
    spawn_agent.invoke({"prompt": "child of thread-1"})
    monkeypatch.setenv("THREAD_ID", "thread-2")
    spawn_agent.invoke({"prompt": "child of thread-2"})
    reg = get_registry()
    drained = reg.drain("thread-1")
    assert len(drained) == 1
    assert reg.live_count() == 1  # thread-2's child still running


def test_orientation_hydration_prepends_agents_md():
    prompt = "investigate the bug"
    out = hydrate_orientation("# Repo guide\nuse strict typing", prompt)
    assert "Orientation (AGENTS.md)" in out
    assert "Repo guide" in out
    assert prompt in out


def test_orientation_hydration_no_agents_md_passthrough():
    prompt = "investigate the bug"
    assert hydrate_orientation(None, prompt) == prompt
    assert hydrate_orientation("", prompt) == prompt


@pytest.mark.asyncio
async def test_timeout_watchdog_drains_long_running_spawn():
    """The 2h hard cap drains a spawn exceeding it."""
    reg = get_registry()
    reg.register("spawn-x", "agent", "thread-1", "ctx-x", "p")
    # Use a tiny timeout for the test (the real cap is 2h)
    result = await enforce_timeout("spawn-x", timeout_s=0.05)
    assert "drained" in result
    assert reg.spawns["spawn-x"]["status"] == "timed_out"


@pytest.mark.asyncio
async def test_timeout_watchdog_skips_finished_spawn():
    reg = get_registry()
    reg.register("spawn-y", "agent", "thread-1", "ctx-y", "p")
    reg.spawns["spawn-y"]["status"] = "completed"
    result = await enforce_timeout("spawn-y", timeout_s=0.01)
    assert "already finished" in result


def test_stagger_constant_is_2s():
    assert SPAWN_STAGGER_S == 2.0


def test_thread_timeout_is_2h():
    assert THREAD_TIMEOUT_S == 2 * 60 * 60


def test_swarm_max_slices_is_8():
    assert SWARM_MAX_SLICES == 8
