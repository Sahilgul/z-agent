"""Phase 0 spike — the seven DECISION_MATRIX checks (a–g).

Each check returns a dict that the matrix runner aggregates per model and
renders into DECISION_MATRIX.md. Thresholds live in DECISION_MATRIX.md (fixed
pre-run, "no number-and-a-debate").
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from zagent_contracts import Notebook, Plan

from spike.agent_loop import MAX_TURNS_DEFAULT, AgentRecorder, make_llm, run_agent_loop

ASK_PROMPT = (
    "How does the scribe flow dedupe questions? Find the code, read it, and answer "
    "with file:line citations. Read-only investigation — do not modify anything."
)
SOAK_PROMPT = (
    "Do a deep read-only investigation of this repo: map how a Socket.io scribe "
    "event flows from the client through the server to persistence. Trace every "
    "hop with file:line evidence, check the DB schema, the socket handlers, and "
    "any queue or service-bus handoffs. Keep going until the full chain is proven "
    "— this should take many steps. Do not modify anything."
)
NUDGE_CANARY = "PANGOLIN"
NUDGE_TEXT = (
    "Steering nudge: STOP the deep dive. In your FINAL answer, include the exact "
    f"word {NUDGE_CANARY} so we can verify this nudge landed."
)


def stamp_workspace(golden: Path, repo: str, branch: str, dest: Path) -> Path:
    """Self-contained clone stamp from golden, checked out at origin/<branch>."""
    src = golden / repo
    if not src.is_dir():
        raise SystemExit(f"[spike] golden repo not found: {src}")
    if dest.exists():
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "clone", "--quiet", str(src), str(dest)], check=True)
    subprocess.run(["git", "-C", str(dest), "fetch", "--quiet", "origin"], check=True)
    subprocess.run(
        ["git", "-C", str(dest), "checkout", "--quiet", "-B", "spike", f"origin/{branch}"],
        check=True,
    )
    print(f"[spike] stamped {repo} @ origin/{branch} -> {dest}")
    return dest


# ----------------------------------------------------------- (a/b/c) ask

async def check_ask(golden: Path, repo: str, branch: str, model: str, results_dir: Path) -> dict[str, Any]:
    """(a) tool-call fidelity + (b) streaming + (c) token accounting — one ask task."""
    ws = stamp_workspace(golden, repo, branch, results_dir / "workspaces" / f"{model}-{repo}")
    llm = make_llm(model, streaming=True)
    rec = AgentRecorder("ask", model)
    await run_agent_loop(llm, ASK_PROMPT, str(ws), rec, max_turns=40)
    summary = rec.finish()
    summary["check"] = "ask"
    return summary


# ----------------------------------------------------------- (d) structured

async def check_structured(model: str) -> dict[str, Any]:
    """(d) structured output fidelity: 5 Plan + 5 Notebook generations via json_schema."""
    plan_ok = 0
    notebook_ok = 0
    attempts = 5

    for i in range(attempts):
        llm_p = make_llm(model, streaming=False, structured=Plan)
        try:
            msg = await llm_p.ainvoke(
                [
                    SystemMessage(content="Return ONLY structured output matching the schema."),
                    HumanMessage(
                        content="Draft a 3-step implementation plan for adding a health-check "
                        "endpoint to a NestJS service."
                    ),
                ]
            )
            Plan.model_validate(_parse_structured(msg))
            plan_ok += 1
        except Exception as exc:  # noqa: BLE001
            print(f"[spike] Plan validation failed ({model} attempt {i}): {exc}")

        llm_n = make_llm(model, streaming=False, structured=Notebook)
        try:
            msg = await llm_n.ainvoke(
                [
                    SystemMessage(content="Return ONLY structured output matching the schema."),
                    HumanMessage(
                        content="Report a specialist notebook about a hypothetical dedupe bug: "
                        "one finding, one file:line evidence, medium confidence."
                    ),
                ]
            )
            Notebook.model_validate(_parse_structured(msg))
            notebook_ok += 1
        except Exception as exc:  # noqa: BLE001
            print(f"[spike] Notebook validation failed ({model} attempt {i}): {exc}")

    return {
        "name": "structured",
        "model": model,
        "check": "structured",
        "plan_valid": f"{plan_ok}/{attempts}",
        "notebook_valid": f"{notebook_ok}/{attempts}",
        "schema_validity_rate": (plan_ok + notebook_ok) / (2 * attempts),
    }


def _parse_structured(msg: Any) -> dict[str, Any]:
    content = msg.content if hasattr(msg, "content") else str(msg)
    if isinstance(content, dict):
        return content
    if isinstance(content, list):
        text = "".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in content)
        return json.loads(text)
    if isinstance(content, str):
        return json.loads(content)
    return {}


# ----------------------------------------------------------- (e) soak

async def check_soak(golden: Path, model: str, results_dir: Path) -> dict[str, Any]:
    """(e) 30+ turn cumulative drift: one REAL deep Ask task on ServerApp."""
    ws = stamp_workspace(golden, "ServerApp", "main", results_dir / "workspaces" / f"{model}-soak")
    llm = make_llm(model, streaming=True)
    rec = AgentRecorder("soak", model)
    await run_agent_loop(llm, SOAK_PROMPT, str(ws), rec, max_turns=MAX_TURNS_DEFAULT)
    summary = rec.finish()
    summary["check"] = "soak"
    summary["soak_turns_met"] = rec.turn_count >= 30
    first, last = rec.tool_calls[:10], rec.tool_calls[-10:]
    if first and last:
        summary["tool_ok_first10"] = sum(t["ok"] for t in first) / len(first)
        summary["tool_ok_last10"] = sum(t["ok"] for t in last) / len(last)
    return summary


# ----------------------------------------------------------- (f) cache

async def check_cache(model: str) -> dict[str, Any]:
    """(f) THE deciding number: do cache_read_input_tokens survive gateway translation?

    Same big prefix, two runs. cache_read_input_tokens > 0 on run 2 = pass.
    """
    big_prefix = "You are investigating a NestJS + Postgres monolith. " * 400
    question = "\n\nQuestion: what is 2+2? Answer in one word."

    async def _once() -> dict[str, Any]:
        llm = make_llm(model, streaming=False)
        msg = await llm.ainvoke([HumanMessage(content=big_prefix + question)])
        return _extract_usage(msg) or {}

    run1 = await _once()
    run2 = await _once()
    cache_read_run2 = run2.get("cache_read_input_tokens", 0) or 0
    input_run2 = run2.get("input_tokens", 0) or 0
    total_in = cache_read_run2 + input_run2
    return {
        "name": "cache",
        "model": model,
        "check": "cache",
        "run1_usage": run1,
        "run2_usage": run2,
        "cache_read_run2": cache_read_run2,
        "cache_hit_ratio_run2": (cache_read_run2 / total_in) if total_in else 0.0,
        "caching_survives": cache_read_run2 > 0,
    }


def _extract_usage(msg: Any) -> dict[str, Any] | None:
    meta = getattr(msg, "response_metadata", {}) or {}
    token_usage = meta.get("token_usage") or meta.get("usage") or {}
    if not token_usage:
        return None
    cached = 0
    ptd = token_usage.get("prompt_tokens_details")
    if isinstance(ptd, dict):
        cached = ptd.get("cached_tokens", 0)
    else:
        cached = token_usage.get("cache_read_input_tokens", 0)
    return {
        "input_tokens": token_usage.get("prompt_tokens", token_usage.get("input_tokens", 0)),
        "output_tokens": token_usage.get("completion_tokens", token_usage.get("output_tokens", 0)),
        "cache_read_input_tokens": cached,
        "total_tokens": token_usage.get("total_tokens", 0),
    }


__all__ = [
    "ASK_PROMPT",
    "NUDGE_CANARY",
    "NUDGE_TEXT",
    "SOAK_PROMPT",
    "check_ask",
    "check_cache",
    "check_soak",
    "check_structured",
    "stamp_workspace",
]
