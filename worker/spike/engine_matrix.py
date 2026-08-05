"""RG — the DECISION_MATRIX a–g gate driven on the REAL engine (worker/engine).

The original spike (spike/matrix.py + spike/agent_loop.py) validated the
gateway with a hand-rolled loop. RG (plan §23) re-runs the same a–g checks
through the assembled LangGraph spine — agent/approval_gate/tools/compaction
— so the gate certifies the thing that actually ships.

Model-level checks (d structured output, f prompt caching) are gateway/model
capabilities and are shared with the spike. Engine-level checks (a/b/c ask,
e soak, g interrupt+inject+resume) drive build_graph() in-process with the
production node wiring; approvals are serviced exactly like the runner
(interrupt -> card -> Command(resume=decision)) with an auto-allow posture
except where the check needs the interrupt itself.

Usage:
  python -m spike.engine_matrix all --golden /golden/repos \
      --models kimi-foundry,qwen-foundry

Env: LITELLM_BASE_URL, LITELLM_API_KEY (gateway); DATABASE_URL optional
(Postgres checkpointer — set it: the gate must certify the production saver).
SPIKE_RESULTS_DIR default ./spike-results.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from langchain_core.messages import HumanMessage
from langgraph.types import Command

from spike.checks import (
    ASK_PROMPT,
    NUDGE_CANARY,
    NUDGE_TEXT,
    SOAK_PROMPT,
    check_cache,
    check_structured,
    stamp_workspace,
)
from spike.matrix import evaluate_gate, render_matrix
from worker.engine.approvals import ApprovalBroker  # noqa: F401  (contract reference)
from worker.engine.checkpointer import open_checkpointer
from worker.engine.compaction import Compactor, SelfTuningLimit
from worker.engine.events import EventEmitter
from worker.engine.graph import build_graph
from worker.engine.state import Autonomy, Mode, tag_message

RESULTS_DIR = Path(os.environ.get("SPIKE_RESULTS_DIR", "./spike-results"))
G_PROMPT = (
    "Append the single line `# engine gate probe` to the end of README.md in "
    "the workspace using the file_write or file_edit tool, then stop."
)


# ------------------------------------------------------------- recording


class EngineRecorder:
    """Scores a real-engine run from its StepEvents + TypingDelta timings."""

    def __init__(self, name: str, model: str) -> None:
        self.name = name
        self.model = model
        self.events: list[Any] = []
        self.tool_calls: list[dict[str, Any]] = []
        self.usage: dict[str, Any] = {}
        self.first_delta_at: float | None = None
        self.started = time.monotonic()
        self.is_error = False
        self.turn_count = 0

    async def event_sink(self, events: list) -> None:
        self.events.extend(events)
        for event in events:
            detail = event.detail or {}
            if "tool" in detail and "ok" in detail:
                self.tool_calls.append({"tool": detail["tool"], "ok": bool(detail["ok"])})
            if detail.get("usage"):
                self.usage = detail["usage"]
            if detail.get("is_error"):
                self.is_error = True
            if detail.get("num_turns"):
                self.turn_count = max(self.turn_count, int(detail["num_turns"]))

    async def delta_sink(self, _delta: Any) -> None:
        if self.first_delta_at is None:
            self.first_delta_at = time.monotonic()

    def finish(self) -> dict[str, Any]:
        ok = sum(1 for t in self.tool_calls if t["ok"])
        total = len(self.tool_calls)
        return {
            "name": self.name,
            "model": self.model,
            "duration_s": round(time.monotonic() - self.started, 1),
            "first_delta_latency_s": (
                round(self.first_delta_at - self.started, 2)
                if self.first_delta_at is not None else None
            ),
            "tool_call_success_rate": (ok / total) if total else None,
            "tool_calls_total": total,
            "turn_count": self.turn_count,
            "is_error": self.is_error,
            "usage": self.usage or None,
        }


class AutoAllowBroker:
    """Graph-side approval broker: the interrupt still fires (that's the point
    of g); the runner-side loop resumes it with allow."""

    def card_payload(self, name: str, args: dict[str, Any], tc_id: str) -> dict[str, Any]:
        return {
            "type": "approval_request", "approval_id": f"ap-{tc_id}",
            "tool": name, "args": args, "tool_call_id": tc_id,
            "preview": f"{name}({json.dumps(args)[:80]})", "destructive": False,
            "always_allowable": False,
        }

    async def is_always_allowed(self, name: str, args: dict[str, Any]) -> bool:
        return False

    async def persist_always_allow(self, name: str) -> None:
        return None


# ------------------------------------------------------------- engine driver


def _initial_state(prompt: str, mode: Mode, autonomy: Autonomy, workspace: Path) -> dict[str, Any]:
    return {
        "run_id": f"rg-{uuid.uuid4().hex[:8]}",
        "thread_id": f"rg-thread-{uuid.uuid4().hex[:8]}",
        "context_id": f"rg-ctx-{uuid.uuid4().hex[:8]}",
        "task_id": str(uuid.uuid4()),
        "mode": mode,
        "autonomy": autonomy,
        "budget": {"used": 0.0, "cap": 50.0},
        "messages": [tag_message(
            HumanMessage(content=f"Workspace root: {workspace}\n\n{prompt}"), "user",
        )],
        "done": False,
        "error": None,
        "approved_calls": {},
        "denial_streak": 0,
        "tool_streak": {},
        "turn_count": 0,
        "compaction_count": 0,
        "compaction_retries": 0,
    }


def _config(rec: EngineRecorder, workspace: Path, model: str,
            emitter: EventEmitter, thread_id: str) -> dict[str, Any]:
    return {
        "configurable": {
            "thread_id": thread_id,
            "model": model,
            "emitter": emitter,
            "approval_broker": AutoAllowBroker(),
            "compactor": Compactor(),
            "tuning": SelfTuningLimit(),
            "workspace": str(workspace),
            "event_sink": rec.event_sink,
            "delta_sink": rec.delta_sink,
        },
        "recursion_limit": 120,
    }


async def _invoke_servicing_approvals(graph: Any, config: dict[str, Any],
                                      input_or_none: Any,
                                      resumes: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """The runner's interrupt pump with an auto-allow (or scripted) posture."""
    scripted = list(resumes or [])
    interrupt_count = 0
    result = await graph.ainvoke(input_or_none, config)
    while True:
        snap = await graph.aget_state(config)
        interrupts = [i for task in snap.tasks for i in task.interrupts]
        if not interrupts:
            return result or {}, interrupt_count
        interrupt_count += len(interrupts)
        decision = scripted.pop(0) if scripted else {"decision": "allow"}
        result = await graph.ainvoke(Command(resume=decision), config)


async def run_engine_turn(prompt: str, mode: Mode, autonomy: Autonomy,
                          model: str, workspace: Path, rec: EngineRecorder,
                          *, saver: Any | None = None) -> tuple[dict[str, Any], Any, dict[str, Any]]:
    """One engine thread on the real graph. Returns (result, graph, config)."""
    emitter = EventEmitter(f"rg-{model}", rec.name, rec.name)
    thread_id = f"rg-{rec.name}-{model}-{uuid.uuid4().hex[:6]}"
    config = _config(rec, workspace, model, emitter, thread_id)
    # Production contract: built tools resolve paths from WORKSPACE_DIR (the
    # sandbox manager exports it in containers; the harness sets it here).
    os.environ["WORKSPACE_DIR"] = str(workspace)
    graph = build_graph(checkpointer=saver)
    result, interrupts = await _invoke_servicing_approvals(
        graph, config, _initial_state(prompt, mode, autonomy, workspace))
    result["_interrupt_count"] = interrupts
    if not rec.usage and result.get("last_usage"):
        rec.usage = result["last_usage"]
    rec.turn_count = max(rec.turn_count, int(result.get("turn_count") or 0))
    if result.get("error"):
        rec.is_error = True
    return result, graph, config


# ------------------------------------------------------------- checks


async def engine_check_ask(golden: Path, repo: str, branch: str, model: str,
                           results_dir: Path, saver: Any | None) -> dict[str, Any]:
    """(a) tool-call fidelity + (b) streaming + (c) token accounting — ask mode
    on the REAL graph (agent -> tools loop, read-only binding)."""
    ws = stamp_workspace(golden, repo, branch, results_dir / "workspaces" / f"{model}-{repo}-engine")
    rec = EngineRecorder("ask", model)
    result, _graph, _config = await run_engine_turn(
        ASK_PROMPT, Mode.ASK, Autonomy.SUPERVISED, model, ws, rec, saver=saver)
    summary = rec.finish()
    summary["check"] = "ask"
    summary["engine_error"] = result.get("error")
    return summary


async def engine_check_soak(golden: Path, model: str, results_dir: Path,
                            saver: Any | None) -> dict[str, Any]:
    """(e) 30+ turn cumulative drift on the REAL graph."""
    ws = stamp_workspace(golden, "ServerApp", "main", results_dir / "workspaces" / f"{model}-engine-soak")
    rec = EngineRecorder("soak", model)
    result, _graph, _config = await run_engine_turn(
        SOAK_PROMPT, Mode.ASK, Autonomy.SUPERVISED, model, ws, rec, saver=saver)
    summary = rec.finish()
    summary["check"] = "soak"
    turns = result.get("turn_count", rec.turn_count)
    summary["turn_count"] = turns
    summary["soak_turns_met"] = turns >= 30 and not result.get("error")
    first, last = rec.tool_calls[:10], rec.tool_calls[-10:]
    if first and last:
        summary["tool_ok_first10"] = sum(t["ok"] for t in first) / len(first)
        summary["tool_ok_last10"] = sum(t["ok"] for t in last) / len(last)
    summary["engine_error"] = result.get("error")
    return summary


async def engine_check_interrupt(golden: Path, model: str, results_dir: Path,
                                 saver: Any | None) -> dict[str, Any]:
    """(g) interrupt + inject + resume ON THE REAL SPINE.

    Turn 1 (development, supervised): the model edits README -> the approval
    gate interrupt()s -> resume allow -> the edit executes.
    Turn 2: a NUDGE-tagged canary message is injected as a state delta (the
    runner's turn-boundary nudge path) -> the final answer must carry the
    canary. State loss is checked across the checkpoint boundary.
    """
    ws = stamp_workspace(golden, "ServerApp", "main", results_dir / "workspaces" / f"{model}-engine-interrupt")
    rec = EngineRecorder("interrupt", model)
    result, graph, config = await run_engine_turn(
        G_PROMPT, Mode.DEVELOPMENT, Autonomy.SUPERVISED, model, ws, rec, saver=saver)

    gate_fired = result.get("_interrupt_count", 0) > 0
    snap = await graph.aget_state(config)
    messages_before = len(snap.values.get("messages", []))

    # Turn 2: the canary nudge rides the runner's state-delta path.
    messages = list(snap.values.get("messages", []))
    messages.append(tag_message(HumanMessage(content=NUDGE_TEXT), "nudge"))
    result2, _ = await _invoke_servicing_approvals(graph, config, {
        "messages": messages, "done": False, "error": None, "needs_compaction": False,
    })

    snap_after = await graph.aget_state(config)
    final_text = ""
    for msg in reversed(snap_after.values.get("messages", [])):
        content = getattr(msg, "content", None)
        if isinstance(content, str) and content.strip() and msg.type == "ai":
            final_text = content
            break
    all_text = json.dumps(
        [(e.detail or {}).get("text", "") for e in rec.events], default=str) + final_text
    state_lost = (
        len(snap_after.values.get("messages", [])) < messages_before
        or result.get("error") is not None
        or result2.get("error") is not None
    )
    return {
        "name": "interrupt",
        "model": model,
        "check": "interrupt",
        "approval_gate_fired": gate_fired,
        "nudge_incorporated": NUDGE_CANARY in all_text,
        "state_lost": bool(state_lost),
        "interrupt_resume_works": bool(NUDGE_CANARY in all_text and not state_lost),
        "engine_error": result.get("error") or result2.get("error"),
    }


# ------------------------------------------------------------- runner


async def run_engine_matrix(command: str, golden: Path, repo: str, branch: str,
                            models: list[str]) -> dict[str, Any]:
    results: dict[str, Any] = {}
    async with open_checkpointer() as saver:
        for model in models:
            results[model] = {}
            print(f"[rg] === model: {model} (real engine) ===")
            try:
                if command in ("ask", "all"):
                    results[model]["ask"] = await engine_check_ask(golden, repo, branch, model, RESULTS_DIR, saver)
                if command in ("structured", "all"):
                    results[model]["structured"] = await check_structured(model)
                if command in ("soak", "all"):
                    results[model]["soak"] = await engine_check_soak(golden, model, RESULTS_DIR, saver)
                if command in ("interrupt", "all"):
                    results[model]["interrupt"] = await engine_check_interrupt(golden, model, RESULTS_DIR, saver)
                if command in ("cache", "all"):
                    results[model]["cache"] = await check_cache(model)
            except Exception as exc:  # noqa: BLE001
                results[model]["error"] = str(exc)
                print(f"[rg] model {model} failed: {exc}")
    return results


async def main() -> int:
    parser = argparse.ArgumentParser(prog="engine_matrix")
    parser.add_argument("command", choices=["ask", "structured", "soak", "interrupt", "cache", "all"])
    parser.add_argument("--golden", default=os.environ.get("GOLDEN_DIR", "/golden/repos"))
    parser.add_argument("--repo", default="ServerApp")
    parser.add_argument("--branch", default="main")
    parser.add_argument("--models", default=os.environ.get("SPIKE_MODELS", "kimi-foundry"))
    args = parser.parse_args()

    for var in ("LITELLM_BASE_URL", "LITELLM_API_KEY"):
        if not os.environ.get(var):
            print(f"[rg] missing env {var} (gateway OpenAI-compatible endpoint + key)", file=sys.stderr)
            return 2

    models = [m.strip() for m in args.models.split(",") if m.strip()]
    all_results = await run_engine_matrix(args.command, Path(args.golden), args.repo, args.branch, models)
    gate = evaluate_gate(all_results) if args.command == "all" else {
        "model_verdicts": {}, "passing_models": [], "gate_passed": False,
        "note": "partial run — gate only evaluated on 'all'",
    }
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    (RESULTS_DIR / "engine-results.json").write_text(
        json.dumps({"results": all_results, "gate": gate}, indent=2, default=str))
    out = RESULTS_DIR / "DECISION_MATRIX.engine.md"
    out.write_text(render_matrix(all_results, gate))
    print(f"[rg] decision matrix -> {out}")
    print(f"[rg] gate passed: {gate['gate_passed']} "
          f"(passing models: {', '.join(gate['passing_models']) or 'none'})")
    return 0 if (args.command != "all" or gate["gate_passed"]) else 1


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    sys.exit(asyncio.run(main()))
