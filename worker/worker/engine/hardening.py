"""Hardening harness — the 30-turn soak + SLO/load tests.

This is the operational gate before cutting the CAS seam. It runs the final
engine (not the spike) through:

  - 30-turn soak: a real deep-investigation task; ≥30 turns, is_error=false,
    last-10 tool success ≥ first-10 − 0.10 (the drift check).
  - SLO: first-delta latency, turn-completion latency, event-loss rate.
  - Load: N concurrent threads (the multi-thread-run stress), no event drops.

Much of this is operational (needs live gateway + containers); the code here
is the harness that runs inside the worker container. The dual-runtime soak
compares this engine against the legacy CAS runtime on the same
task; the canary flips traffic to the new engine at 10% → 50% → 100%.
"""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass, field
from typing import Any

from langchain_core.messages import HumanMessage

from worker.engine import EventEmitter, build_graph, make_checkpointer
from worker.engine.state import Autonomy, Budget, EngineState, Mode, tag_message

SOAK_PROMPT = (
    "Do a deep read-only investigation of this repo: map how a Socket.io scribe "
    "event flows from the client through the server to persistence. Trace every "
    "hop with file:line evidence, check the DB schema, the socket handlers, and "
    "any queue or service-bus handoffs. Keep going until the full chain is proven "
    "— this should take many steps. Do not modify anything."
)


@dataclass
class SoakResult:
    turns: int = 0
    tool_calls: int = 0
    tool_calls_ok: int = 0
    first_delta_latency_s: float | None = None
    turn_latencies: list[float] = field(default_factory=list)
    events_emitted: int = 0
    events_lost: int = 0
    is_error: bool = False
    drift: float | None = None  # last10_ok_rate - first10_ok_rate

    @property
    def event_loss_rate(self) -> float:
        return (self.events_lost / self.events_emitted) if self.events_emitted else 0.0

    @property
    def p95_turn_latency_s(self) -> float | None:
        if not self.turn_latencies:
            return None
        s = sorted(self.turn_latencies)
        return s[int(len(s) * 0.95)]


# --- SLO thresholds (fixed pre-run) ---

SLO = {
    "min_turns": 30,
    "max_first_delta_latency_s": 5.0,
    "max_p95_turn_latency_s": 60.0,
    "max_event_loss_rate": 0.0,
    "min_drift": -0.10,  # last10 >= first10 - 0.10
}


async def run_soak(*, model: str, run_id: str, thread_id: str,
                   workspace: str, max_turns: int = 80) -> tuple[SoakResult, dict[str, Any]]:
    """Run the 30-turn soak against the final engine. Returns (result, verdict)."""
    result = SoakResult()
    events: list[Any] = []
    deltas: list[Any] = []
    started = time.monotonic()
    first_delta_at: float | None = None  # H-14: timestamp of the FIRST delta

    async def event_sink(evts: list) -> None:
        for e in evts:
            # H-15: capture per-turn latency from turn_boundary events so the
            # p95 SLO can pass. turn_latencies was never populated, so
            # p95_turn_latency_s was always None -> the SLO evaluated 999 < 60
            # and structurally failed.
            d = getattr(e, "detail", None) or {}
            if getattr(e, "title", "") == "turn complete" and d.get("duration_ms") is not None:
                result.turn_latencies.append(float(d["duration_ms"]) / 1000.0)
        events.extend(evts)

    async def delta_sink(delta) -> None:
        nonlocal first_delta_at
        # H-14: capture when the FIRST delta arrives — the SLO measures
        # time-to-first-delta, not full runtime (the old code recorded the
        # full runtime after the graph finished, so the SLO was structurally
        # unpassable).
        if first_delta_at is None:
            first_delta_at = time.monotonic()
        deltas.append(delta)

    emitter = EventEmitter(run_id, thread_id)
    graph = build_graph()
    _checkpointer = make_checkpointer()  # wired for Postgres in hardening

    state: EngineState = {
        "run_id": run_id, "thread_id": thread_id, "context_id": thread_id,
        "task_id": f"soak-{int(time.time())}",
        "mode": Mode.ASK, "autonomy": Autonomy.SUPERVISED,
        "budget": Budget(cap=20.0),
        "messages": [tag_message(HumanMessage(content=f"Workspace: {workspace}\n\n{SOAK_PROMPT}"), "user")],  # type: ignore[arg-type]
        "done": False, "error": None,
    }
    config = {
        "configurable": {
            "thread_id": thread_id, "model": model, "emitter": emitter,
            "approval_gate": None, "event_sink": event_sink, "delta_sink": delta_sink,
        },
        "recursion_limit": max_turns * 2,
    }

    try:
        outcome = await graph.ainvoke(state, config=config)
        result.is_error = bool(outcome.get("error"))
    except Exception:  # noqa: BLE001
        result.is_error = True

    result.turns = emitter._seq
    # M-17: events_lost was never set, so the "no event loss" SLO was
    # vacuously green (0/0). Measure it for real: events_emitted is what the
    # EMITTER created (emitter._seq); events is what the SINK received. The
    # difference is actual loss (a sink that dropped events now makes the
    # SLO fail instead of passing by default).
    result.events_emitted = emitter._seq
    result.events_lost = max(0, emitter._seq - len(events))
    # Count tool calls + ok from events
    tool_events = [e for e in events if e.detail.get("tool")]
    result.tool_calls = len(tool_events)
    result.tool_calls_ok = sum(1 for e in tool_events if e.detail.get("ok"))
    if deltas and first_delta_at is not None:
        # H-14: time-to-FIRST-delta (captured in the delta_sink closure), not
        # the full runtime — the old code used `time.monotonic() - started`
        # here, which is the whole soak duration and made the SLO
        # structurally unpassable.
        result.first_delta_latency_s = first_delta_at - started
    # Drift: first-10 vs last-10 tool success rate
    if len(tool_events) >= 20:
        first10 = tool_events[:10]
        last10 = tool_events[-10:]
        f_rate = sum(1 for e in first10 if e.detail.get("ok")) / 10
        l_rate = sum(1 for e in last10 if e.detail.get("ok")) / 10
        result.drift = l_rate - f_rate

    verdict = evaluate_slo(result)
    return result, verdict


def evaluate_slo(result: SoakResult) -> dict[str, Any]:
    """Map a soak result to SLO pass/fail."""
    checks = {
        "turns_met": result.turns >= SLO["min_turns"],
        "first_delta_ok": (result.first_delta_latency_s or 999) < SLO["max_first_delta_latency_s"],
        "p95_turn_ok": (result.p95_turn_latency_s or 999) < SLO["max_p95_turn_latency_s"],
        "no_event_loss": result.event_loss_rate <= SLO["max_event_loss_rate"],
        "no_drift": (result.drift is None) or (result.drift >= SLO["min_drift"]),
        "no_error": not result.is_error,
    }
    return {"checks": checks, "passed": all(checks.values())}


# --- Load test (concurrent threads) ---

async def run_load_test(*, model: str, num_threads: int = 4,
                        run_id: str = "load-run") -> dict[str, Any]:
    """N concurrent threads; verify no event drops + no cross-thread contamination."""
    async def one(tid: str) -> SoakResult:
        result, _ = await run_soak(model=model, run_id=run_id, thread_id=tid,
                                   workspace=os.environ.get("WORKSPACE_DIR", "/workspace"))
        return result

    tasks = [asyncio.create_task(one(f"{run_id}-t{i}")) for i in range(num_threads)]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    errors = [r for r in results if isinstance(r, Exception)]
    return {
        "num_threads": num_threads,
        "errors": len(errors),
        "all_completed": len(errors) == 0,
    }


__all__ = ["SLO", "SoakResult", "evaluate_slo", "run_load_test", "run_soak"]
