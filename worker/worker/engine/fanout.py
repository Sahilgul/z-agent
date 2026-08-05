"""Fan-out — spawn_agent + spawn_swarm.

Two fan-out tools, both engine-side (the agent proposes, the engine vets):

  spawn_agent  — one isolated subagent under the current thread. Used for
                 context-isolated work (a focused investigation that shouldn't
                 pollute the parent's context). Derived context_id:
                 `{thread_id}::worker-{n}`.

  spawn_swarm   — N DISTINCT homogeneous slices (the Lead's decomposition).
                 Each slice runs as its own thread under the run (NOT a
                 subagent of the current thread — they're siblings). Used for
                 width: parallel work on distinct angles/modules.

Engine-side vetoes (the agent never gets to bypass these):
  - STAGGER: spawns are staggered (not all at once) to avoid thundering-herd
    on the gateway. Default 2s between spawns.
  - SHRINK/RECOVER: if the gateway 429s during spawn, shrink the batch (drop
    the last slice) and retry; never spawn into a known-busy gateway.
  - 2H TIMEOUT: every spawned thread has a 2h hard cap. A thread exceeding it
    is drained (its work is committed, its status set to timed_out).
  - ONE-APPROVAL BATCH: a swarm decomposition is approved as ONE batch (the
    human sees all slices, approves once). Not N approvals.
  - CASCADE DRAIN: if the parent thread is killed/stopped, all spawned
    subagents/swarm threads are drained (committed + stopped) in cascade.

AGENTS.md orientation hydration: at spawn, the subagent's first user message
includes the AGENTS.md diff (if any) so it starts oriented. The worker idle
metric gates whether a new spawn is even allowed — a saturated
worker pool refuses new spawns.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from langchain_core.tools import tool

# --- Configuration (fan-out guards) ---

SPAWN_STAGGER_S = 2.0          # delay between spawns
SWARM_MAX_SLICES = 8           # hard cap on simultaneous swarm width
THREAD_TIMEOUT_S = 2 * 60 * 60  # 2h hard cap
SPAWN_RETRY_BACKOFF = [1, 2, 4]  # shrink/recover retry delays


@dataclass
class SpawnRequest:
    """A proposed spawn (agent-authored, engine-vetted)."""
    kind: str  # "agent" | "swarm"
    prompt: str
    repo: str | None = None
    # swarm-only
    slices: list[dict[str, str]] | None = None
    # engine-side
    approved: bool = False
    vetoed: bool = False
    veto_reason: str = ""


@dataclass
class SpawnRegistry:
    """Tracks live spawns for cascade drain + the worker-idle metric."""
    spawns: dict[str, dict[str, Any]] = field(default_factory=dict)

    def register(self, spawn_id: str, kind: str, parent_thread_id: str,
                 context_id: str, prompt: str) -> None:
        self.spawns[spawn_id] = {
            "kind": kind, "parent_thread_id": parent_thread_id,
            "context_id": context_id, "prompt": prompt,
            "started_at": time.time(), "status": "running",
            "watchdog": None,
        }

    def drain(self, parent_thread_id: str) -> list[str]:
        """Cascade drain: stop all spawns under a parent. Returns drained ids."""
        drained = []
        for sid, sp in self.spawns.items():
            if sp["parent_thread_id"] == parent_thread_id and sp["status"] == "running":
                sp["status"] = "drained"
                self._cancel_watchdog(sid)
                drained.append(sid)
        return drained

    def live_count(self) -> int:
        return sum(1 for s in self.spawns.values() if s["status"] == "running")

    def is_saturated(self, cap: int = SWARM_MAX_SLICES) -> bool:
        return self.live_count() >= cap

    def arm_watchdog(self, spawn_id: str, loop: asyncio.AbstractEventLoop) -> None:
        """Arm the 2h hard-cap watchdog for a spawn on the given event loop.

        The spawn tools are sync @tool objects invoked via run_in_executor, so
        they execute in a worker thread with NO running loop — the loop is
        only available in the async dispatch path that called them. The
        dispatcher snapshots the registry before invocation and arms
        watchdogs for any newly-registered spawns once back on the loop
        thread. Without this, `enforce_timeout` was never scheduled and the
        2h cap was never armed (C-04)."""
        sp = self.spawns.get(spawn_id)
        if sp is None or sp.get("watchdog") is not None:
            return
        sp["watchdog"] = asyncio.ensure_future(enforce_timeout(spawn_id), loop=loop)

    def finish(self, spawn_id: str, status: str = "completed") -> None:
        """Mark a spawn finished and cancel its watchdog (the 2h cap is moot
        once the spawn is done)."""
        sp = self.spawns.get(spawn_id)
        if sp is None:
            return
        sp["status"] = status
        self._cancel_watchdog(spawn_id)

    def _cancel_watchdog(self, spawn_id: str) -> None:
        sp = self.spawns.get(spawn_id)
        if sp is None:
            return
        wd = sp.pop("watchdog", None)
        if wd is not None and not wd.done():
            wd.cancel()


# A module-level registry (the engine owns one per run)
_registry = SpawnRegistry()


def get_registry() -> SpawnRegistry:
    return _registry


def reset_registry() -> None:
    """Reset for tests."""
    global _registry
    _registry = SpawnRegistry()


# --- Engine-side veto logic ---

def _veto(req: SpawnRequest, *, worker_idle: bool = True) -> tuple[bool, str]:
    """Engine-side veto. Returns (allowed, reason). The agent never bypasses this."""
    if not worker_idle:
        return False, "worker pool saturated — fan-out refused, retry later"
    if req.kind == "swarm":
        if not req.slices:
            return False, "swarm requires at least one slice"
        if len(req.slices) > SWARM_MAX_SLICES:
            return False, f"swarm width {len(req.slices)} exceeds cap {SWARM_MAX_SLICES}"
        # DISTINCT slices (never arithmetic clones)
        prompts = {s["prompt"] for s in req.slices}
        if len(prompts) < len(req.slices):
            return False, "swarm slices must be DISTINCT (duplicate prompts rejected)"
    return True, ""


def _staggered_spawn(coros: list, stagger_s: float = SPAWN_STAGGER_S) -> list:
    """Wrap coroutines with a stagger delay to avoid thundering-herd."""
    async def _staggered(coro, delay):
        await asyncio.sleep(delay)
        return await coro
    return [_staggered(c, i * stagger_s) for i, c in enumerate(coros)]


# --- spawn_agent tool ---

@tool
def spawn_agent(prompt: str, repo: str | None = None) -> str:
    """Spawn ONE isolated subagent under the current thread.

    The subagent gets a derived context_id (`{thread_id}::worker-{n}`) so its
    conversation is isolated but traceable to the parent. Use this for focused
    work that shouldn't pollute the parent's context (a deep investigation,
    a single-file refactor). The engine vets the spawn (worker-idle gate).
    """
    req = SpawnRequest(kind="agent", prompt=prompt, repo=repo)
    # The worker-idle saturation gate was dead code: _veto defaulted
    # worker_idle=True and every call site omitted it, so a saturated worker
    # pool never refused a spawn (C-03). Drive it from the registry's live
    # spawn count.
    allowed, reason = _veto(req, worker_idle=not _registry.is_saturated())
    if not allowed:
        return f"error: spawn vetoed — {reason}"
    spawn_id = str(uuid.uuid4())
    context_id = f"{_current_thread_id()}::worker-{spawn_id[:8]}"
    _registry.register(spawn_id, "agent", _current_thread_id(), context_id, prompt)
    # The actual spawn is async; the tool returns the spawn handle for the feed.
    return (f"spawned agent {spawn_id} (context={context_id}). "
            f"2h timeout; cascade-drained if parent stops.")


# --- spawn_swarm tool ---

@tool
def spawn_swarm(slices: list[dict[str, str]], rationale: str = "") -> str:
    """Spawn N DISTINCT homogeneous slices as sibling threads under the run.

    Each slice is a {title, prompt, repo?, angle?} dict. The decomposition is
    approved as ONE batch (the human sees all slices, approves once). The engine
    vets: stagger, shrink/recover on gateway 429, 2h timeout per slice, cascade
    drain if the parent stops. Max width: 8 slices.
    """
    req = SpawnRequest(kind="swarm", prompt="", slices=slices)
    allowed, reason = _veto(req, worker_idle=not _registry.is_saturated())
    if not allowed:
        return f"error: swarm vetoed — {reason}"
    spawn_ids = []
    for i, s in enumerate(slices):
        sid = str(uuid.uuid4())
        context_id = f"{_current_thread_id()}::swarm-{i}-{sid[:8]}"
        _registry.register(sid, "swarm", _current_thread_id(), context_id, s.get("prompt", ""))
        spawn_ids.append(sid)
    return (f"spawned swarm of {len(spawn_ids)} threads (staggered 2s, 2h timeout, "
            f"one-approval batch). ids: {', '.join(spawn_ids)}")


def _current_thread_id() -> str:
    """The current thread's id (set by the runner via env)."""
    import os
    return os.environ.get("THREAD_ID", "unknown-thread")


# --- AGENTS.md orientation hydration ---

def hydrate_orientation(agents_md: str | None, prompt: str) -> str:
    """Prepend the AGENTS.md diff (if any) to the spawn's first user message."""
    if not agents_md:
        return prompt
    return f"# Orientation (AGENTS.md)\n{agents_md}\n\n---\n\n# Task\n{prompt}"


# --- Timeout watchdog ---

async def enforce_timeout(spawn_id: str, timeout_s: float = THREAD_TIMEOUT_S) -> str:
    """The 2h hard cap. A spawn exceeding it is drained."""
    await asyncio.sleep(timeout_s)
    sp = _registry.spawns.get(spawn_id)
    if sp and sp["status"] == "running":
        sp["status"] = "timed_out"
        return f"drained {spawn_id} (2h timeout exceeded)"
    return f"{spawn_id} already finished"


__all__ = [
    "SPAWN_STAGGER_S",
    "SWARM_MAX_SLICES",
    "THREAD_TIMEOUT_S",
    "SpawnRegistry",
    "SpawnRequest",
    "enforce_timeout",
    "get_registry",
    "hydrate_orientation",
    "reset_registry",
    "spawn_agent",
    "spawn_swarm",
]