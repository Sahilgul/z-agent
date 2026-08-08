"""Contract tests for fan-out."""

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
    # C1: the tool records a vetted REQUEST; the backend spawns the thread.
    assert result.startswith("spawn requested: agent")
    reg = get_registry()
    assert reg.live_count() == 1


def test_spawn_agent_vetoed_when_worker_saturated(monkeypatch: pytest.MonkeyPatch):
    """The engine-side veto refuses spawns when the worker pool is saturated.

    Driven END-TO-END through the tool (not _veto in isolation): the old test
    called `_veto(req, worker_idle=False)` directly, which codified the C-03
    bypass — the call sites defaulted worker_idle=True and never consulted the
    registry, so a saturated pool still accepted spawns. The tool itself must
    refuse when the registry is saturated."""
    monkeypatch.setenv("THREAD_ID", "thread-1")
    reg = get_registry()
    # Saturate the registry
    for i in range(SWARM_MAX_SLICES):
        reg.register(f"s{i}", "swarm", "thread-1", f"ctx-{i}", "p")
    assert reg.is_saturated()
    # Now spawn_agent itself must be vetoed (worker_idle derived from the registry).
    result = spawn_agent.invoke({"prompt": "should be refused"})
    assert result.startswith("error: spawn vetoed")
    assert "saturated" in result
    # No new spawn registered — the gate refused it.
    assert reg.live_count() == SWARM_MAX_SLICES


def test_spawn_swarm_vetoed_when_worker_saturated(monkeypatch: pytest.MonkeyPatch):
    """A saturated worker pool refuses swarm spawns too (same gate as spawn_agent)."""
    monkeypatch.setenv("THREAD_ID", "thread-1")
    reg = get_registry()
    for i in range(SWARM_MAX_SLICES):
        reg.register(f"s{i}", "swarm", "thread-1", f"ctx-{i}", "p")
    slices = [{"title": "a", "prompt": "distinct prompt"}]
    result = spawn_swarm.invoke({"slices": slices})
    assert result.startswith("error: swarm vetoed")
    assert "saturated" in result
    assert reg.live_count() == SWARM_MAX_SLICES


def test_spawn_swarm_rejects_duplicate_slices(monkeypatch: pytest.MonkeyPatch):
    """Swarm slices must be DISTINCT — duplicate prompts are rejected."""
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
    assert "swarm of 3 threads requested" in result
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


@pytest.fixture
def fake_spawn_redis(monkeypatch: pytest.MonkeyPatch):
    """C1: stand in for the spawn-request stream so dispatch tests don't need
    a live Redis. Verifies the request payload the SpawnBridge consumes."""
    import fakeredis.aioredis

    from worker.engine import fanout
    client = fakeredis.aioredis.FakeRedis()
    monkeypatch.setattr(fanout, "_spawn_redis", client)
    monkeypatch.setenv("RUN_ID", "run-1")
    yield client
    monkeypatch.setattr(fanout, "_spawn_redis", None)


@pytest.mark.asyncio
async def test_spawn_arms_2h_watchdog_via_dispatch(monkeypatch: pytest.MonkeyPatch,
                                                   fake_spawn_redis):
    """C-04: a successful spawn must arm the 2h hard-cap watchdog. The sync
    spawn tool runs in an executor thread with no running loop, so the watchdog
    is armed by the async dispatch path (call_tool_direct -> _call_extra_tool)
    once back on the loop thread. Before this fix `enforce_timeout` was never
    scheduled and the 2h cap was never armed."""
    monkeypatch.setenv("THREAD_ID", "thread-1")
    from worker.engine.tools import call_tool_direct

    result = await call_tool_direct("spawn_agent", {"prompt": "investigate the dedupe bug"})
    assert result["ok"] is True
    reg = get_registry()
    live = [sid for sid, sp in reg.spawns.items() if sp["status"] == "running"]
    assert len(live) == 1
    sp = reg.spawns[live[0]]
    assert sp["watchdog"] is not None, "2h watchdog was not armed"
    assert not sp["watchdog"].done()
    # Cancel the real 2h watchdog so it doesn't linger past the test.
    sp["watchdog"].cancel()


@pytest.mark.asyncio
async def test_spawn_publishes_request_to_backend(monkeypatch: pytest.MonkeyPatch,
                                                  fake_spawn_redis):
    """C1: a dispatched spawn becomes a spawn_requests stream entry the
    backend SpawnBridge turns into a REAL thread (no phantom spawns)."""
    import json

    monkeypatch.setenv("THREAD_ID", "thread-1")
    from worker.engine.tools import call_tool_direct
    result = await call_tool_direct("spawn_agent",
                                    {"prompt": "map the auth flow", "repo": "web"})
    assert result["ok"] is True
    entries = await fake_spawn_redis.xrange("spawn_requests:run-1")
    assert len(entries) == 1
    fields = {k.decode() if isinstance(k, bytes) else k:
              v.decode() if isinstance(v, bytes) else v
              for k, v in entries[0][1].items()}
    payload = json.loads(fields["payload"])
    assert payload["run_id"] == "run-1"
    assert payload["parent_thread_id"] == "thread-1"
    assert payload["kind"] == "agent"
    assert payload["prompt"] == "map the auth flow"
    assert payload["repo"] == "web"
    assert payload["spawn_id"] in get_registry().spawns


@pytest.mark.asyncio
async def test_cascade_drain_cancels_watchdog(monkeypatch: pytest.MonkeyPatch,
                                              fake_spawn_redis):
    """C-04: cascade drain must cancel the watchdog of every drained spawn
    (the 2h cap is moot once the parent stops the spawn)."""
    monkeypatch.setenv("THREAD_ID", "thread-1")
    from worker.engine.tools import call_tool_direct

    await call_tool_direct("spawn_agent", {"prompt": "child 1"})
    await call_tool_direct("spawn_agent", {"prompt": "child 2"})
    reg = get_registry()
    live = [sid for sid, sp in reg.spawns.items() if sp["status"] == "running"]
    assert all(reg.spawns[sid]["watchdog"] is not None for sid in live)
    drained = reg.drain("thread-1")
    assert len(drained) == 2
    assert reg.live_count() == 0
    for sid in drained:
        # watchdog cancelled (popped) on drain
        assert "watchdog" not in reg.spawns[sid]


def test_stagger_constant_is_2s():
    assert SPAWN_STAGGER_S == 2.0


def test_thread_timeout_is_2h():
    assert THREAD_TIMEOUT_S == 2 * 60 * 60


def test_swarm_max_slices_is_8():
    assert SWARM_MAX_SLICES == 8
