"""LEGACY tracer bullet — Claude Agent SDK based (SUPERSEDED Aug 5, Round 32/Phase 0).

Replaced by worker/spike/matrix.py, which runs the FULL a–g DECISION_MATRIX
over the OpenAI-compatible gateway using langchain-openai ChatOpenAI + a
LangGraph interrupt/resume graph (check g). Kept for history; do NOT run this
against the open-model route — it speaks the Anthropic protocol the SDK expects,
which is not what the Phase 0 gate validates.

Headless agent, run INSIDE the worker container image on the real workstation,
streaming normalized StepEvents over WS to a static page — no DB, no auth, no
modes, one gateway key. Verifies, through LiteLLM -> Foundry Kimi:

  (a) tool-call fidelity (read/edit/bash round-trips)
  (b) streaming
  (c) token accounting
  (d) structured output fidelity (Plan + Notebook schemas via output_format)
  (e) one REAL 30+ turn Ask-mode task on ServerApp (cumulative drift)
  (f) PROMPT CACHING survival (the deciding number)
  (g) interrupt + inject + resume fidelity (nudge semantics)

Output: results JSON + DECISION_MATRIX.md filled in. Thresholds are registered
in DECISION_MATRIX.md BEFORE running — the matrix decides Kimi vs
Claude-on-Foundry mechanically, no number-and-a-debate.

Usage (inside container, or on host for a pre-Docker smoke):
  python -m spike.tracer ask       --golden /golden/repos --repo ServerApp --branch main
  python -m spike.tracer structured
  python -m spike.tracer soak      --golden /golden/repos
  python -m spike.tracer interrupt --golden /golden/repos
  python -m spike.tracer cache
  python -m spike.tracer all       --golden /golden/repos

Env: ANTHROPIC_BASE_URL (gateway), ANTHROPIC_AUTH_TOKEN (gateway key),
     SPIKE_RESULTS_DIR (default ./spike-results), SPIKE_WS_PORT (default 8765),
     SPIKE_HTTP_PORT (default 8766).
"""

from __future__ import annotations

import argparse
import asyncio
import functools
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    ToolUseBlock,
)
from zagent_contracts import Notebook, Plan, StepEvent, StepKind

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from worker.normalize import Normalizer

RESULTS_DIR = Path(os.environ.get("SPIKE_RESULTS_DIR", "./spike-results"))
WS_PORT = int(os.environ.get("SPIKE_WS_PORT", "8765"))
HTTP_PORT = int(os.environ.get("SPIKE_HTTP_PORT", "8766"))

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


# ---------------------------------------------------------------- WS + static

class Broadcaster:
    def __init__(self) -> None:
        self.clients: set[Any] = set()
        self.loop: asyncio.AbstractEventLoop | None = None

    async def handler(self, ws: Any) -> None:
        self.clients.add(ws)
        try:
            await ws.wait_closed()
        finally:
            self.clients.discard(ws)

    def publish(self, payload: dict[str, Any]) -> None:
        if not self.loop or not self.clients:
            return
        data = json.dumps(payload)

        async def _send() -> None:
            for ws in list(self.clients):
                try:
                    await ws.send(data)
                except Exception:
                    self.clients.discard(ws)

        asyncio.run_coroutine_threadsafe(_send(), self.loop)


BROADCAST = Broadcaster()


def _start_servers() -> None:
    import websockets

    static_dir = Path(__file__).parent / "static"

    def _http() -> None:
        handler = functools.partial(SimpleHTTPRequestHandler, directory=str(static_dir))
        ThreadingHTTPServer(("0.0.0.0", HTTP_PORT), handler).serve_forever()

    threading.Thread(target=_http, daemon=True).start()

    async def _ws() -> None:
        BROADCAST.loop = asyncio.get_running_loop()
        async with websockets.serve(BROADCAST.handler, "0.0.0.0", WS_PORT):
            await asyncio.Future()

    def _ws_thread() -> None:
        asyncio.run(_ws())

    threading.Thread(target=_ws_thread, daemon=True).start()
    time.sleep(0.5)
    print(f"[spike] viewer: http://localhost:{HTTP_PORT}  ws: ws://localhost:{WS_PORT}")


# ---------------------------------------------------------------- workspace

def stamp_workspace(golden: Path, repo: str, branch: str, dest: Path) -> Path:
    """Self-contained clone stamp from golden, checked out at origin/<branch>.
    Golden is fetch-only; the stamp owns its .git (plan §3)."""
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


# ---------------------------------------------------------------- run harness

class RunRecorder:
    def __init__(self, name: str) -> None:
        self.name = name
        self.events: list[dict[str, Any]] = []
        self.tool_calls: list[dict[str, Any]] = []
        self.result: ResultMessage | None = None
        self.started = time.monotonic()
        self.first_delta_at: float | None = None

    def record_events(self, events: list[StepEvent]) -> None:
        for e in events:
            self.events.append(e.model_dump(mode="json"))
            BROADCAST.publish({"type": "step", "event": e.model_dump(mode="json")})
            if e.kind in (StepKind.COMMAND, StepKind.FILE_READ, StepKind.FILE_EDIT, StepKind.MCP_CALL):
                self.tool_calls.append({"title": e.title, "ok": e.detail.get("ok", True)})

    def record_deltas(self, deltas: list[Any]) -> None:
        if deltas and self.first_delta_at is None:
            self.first_delta_at = time.monotonic()
        for d in deltas:
            BROADCAST.publish({"type": "delta", "delta": d.model_dump(mode="json")})

    def finish(self) -> dict[str, Any]:
        ok = sum(1 for t in self.tool_calls if t["ok"])
        return {
            "name": self.name,
            "duration_s": round(time.monotonic() - self.started, 1),
            "first_delta_latency_s": (
                round(self.first_delta_at - self.started, 2) if self.first_delta_at else None
            ),
            "num_events": len(self.events),
            "tool_calls": len(self.tool_calls),
            "tool_calls_ok": ok,
            "tool_call_success_rate": (ok / len(self.tool_calls)) if self.tool_calls else None,
            "num_turns": self.result.num_turns if self.result else None,
            "usage": self.result.usage if self.result else None,
            "is_error": self.result.is_error if self.result else None,
        }


def _options(cwd: Path | None, **kwargs: Any) -> ClaudeAgentOptions:
    env = {
        "ANTHROPIC_BASE_URL": os.environ["ANTHROPIC_BASE_URL"],
        "ANTHROPIC_AUTH_TOKEN": os.environ["ANTHROPIC_AUTH_TOKEN"],
    }
    return ClaudeAgentOptions(
        cwd=str(cwd) if cwd else None,
        permission_mode="default",
        env=env,
        **kwargs,
    )


async def run_agent(
    name: str,
    prompt: str,
    cwd: Path | None,
    normalizer_run: str,
    on_client: Any = None,
    **options_kwargs: Any,
) -> RunRecorder:
    rec = RunRecorder(name)
    norm = Normalizer(run_id=normalizer_run, thread_id="spike-lane")
    async with ClaudeSDKClient(options=_options(cwd, **options_kwargs)) as client:
        if on_client:
            await on_client(client)
        await client.query(prompt)
        async for msg in client.receive_response():
            events, deltas = norm.handle(msg)
            rec.record_deltas(deltas)
            rec.record_events(events)
            if isinstance(msg, ResultMessage):
                rec.result = msg
    return rec


# ---------------------------------------------------------------- checks

async def check_ask(golden: Path, repo: str, branch: str) -> dict[str, Any]:
    ws = stamp_workspace(golden, repo, branch, RESULTS_DIR / "workspaces" / repo)
    rec = await run_agent("ask", ASK_PROMPT, ws, "spike-ask")
    return rec.finish()


async def check_structured() -> dict[str, Any]:
    """(d) structured output fidelity: 5 Plan + 5 Notebook generations, all must
    validate against the Pydantic contracts through gateway translation."""
    plan_ok = notebook_ok = 0
    attempts = 5
    for i in range(attempts):
        rec = RunRecorder(f"structured-plan-{i}")
        async with ClaudeSDKClient(options=_options(
            None,
            output_format={"type": "json_schema", "schema": Plan.model_json_schema()},
        )) as client:
            await client.query(
                "Draft a 3-step implementation plan for adding a health-check "
                "endpoint to a NestJS service. Return ONLY the structured output."
            )
            async for msg in client.receive_response():
                if isinstance(msg, ResultMessage):
                    rec.result = msg
        if rec.result is None:
            # No ResultMessage (failed/empty stream) — score 0 like the other
            # failure modes instead of crashing the whole check.
            print(f"[spike] no ResultMessage on attempt {i} — scored 0")
            continue
        structured = (rec.result.usage or {}) and getattr(rec.result, "structured_output", None)
        try:
            raw = getattr(rec.result, "structured_output", None) or json.loads(rec.result.result or "{}")
            Plan.model_validate(raw)
            plan_ok += 1
        except Exception as exc:  # noqa: BLE001
            print(f"[spike] Plan validation failed (attempt {i}): {exc}")

        rec2 = RunRecorder(f"structured-notebook-{i}")
        async with ClaudeSDKClient(options=_options(
            None,
            output_format={"type": "json_schema", "schema": Notebook.model_json_schema()},
        )) as client:
            await client.query(
                "Report a specialist notebook about a hypothetical dedupe bug: one "
                "finding, one file:line evidence, medium confidence. Structured output only."
            )
            async for msg in client.receive_response():
                if isinstance(msg, ResultMessage):
                    rec2.result = msg
        try:
            raw = getattr(rec2.result, "structured_output", None) or json.loads(rec2.result.result or "{}")
            Notebook.model_validate(raw)
            notebook_ok += 1
        except Exception as exc:  # noqa: BLE001
            print(f"[spike] Notebook validation failed (attempt {i}): {exc}")
        _ = structured

    return {
        "name": "structured",
        "plan_valid": f"{plan_ok}/{attempts}",
        "notebook_valid": f"{notebook_ok}/{attempts}",
        "schema_validity_rate": (plan_ok + notebook_ok) / (2 * attempts),
    }


async def check_soak(golden: Path) -> dict[str, Any]:
    """(e) 30+ turn cumulative drift: one REAL deep Ask task on ServerApp."""
    ws = stamp_workspace(golden, "ServerApp", "main", RESULTS_DIR / "workspaces" / "soak-ServerApp")
    rec = await run_agent("soak", SOAK_PROMPT, ws, "spike-soak", max_turns=80)
    summary = rec.finish()
    summary["soak_turns_met"] = bool(rec.result and rec.result.num_turns >= 30)
    # drift proxy: success rate over the LAST 10 tool calls vs the first 10
    first, last = rec.tool_calls[:10], rec.tool_calls[-10:]
    if first and last:
        summary["tool_ok_first10"] = sum(t["ok"] for t in first) / len(first)
        summary["tool_ok_last10"] = sum(t["ok"] for t in last) / len(last)
    return summary


async def check_interrupt(golden: Path) -> dict[str, Any]:
    """(g) nudge = graceful interrupt() + inject + resume. Verify no state loss and
    that the agent visibly incorporates the steering message."""
    ws = stamp_workspace(golden, "ServerApp", "main", RESULTS_DIR / "workspaces" / "interrupt-ServerApp")
    rec = RunRecorder("interrupt")
    norm = Normalizer(run_id="spike-interrupt", thread_id="spike-lane")
    incorporated = False
    state_lost = False
    async with ClaudeSDKClient(options=_options(ws)) as client:
        await client.query(SOAK_PROMPT)
        nudged = False
        async for msg in client.receive_response():
            events, deltas = norm.handle(msg)
            rec.record_deltas(deltas)
            rec.record_events(events)
            if not nudged and isinstance(msg, AssistantMessage) and any(
                isinstance(b, ToolUseBlock) for b in msg.content
            ):
                nudged = True
                await client.interrupt()
                await client.query(
                    "Steering nudge: STOP the deep dive. In your FINAL answer, include "
                    "the exact word PANGOLIN so we can verify this nudge landed."
                )
            if isinstance(msg, ResultMessage):
                rec.result = msg
    text = json.dumps(rec.events)
    incorporated = "PANGOLIN" in text
    state_lost = rec.result is not None and rec.result.is_error
    summary = rec.finish()
    summary.update({"nudge_incorporated": incorporated, "state_lost": state_lost})
    return summary


async def check_cache() -> dict[str, Any]:
    """(f) THE deciding number: do cache_read_input_tokens survive
    LiteLLM -> Foundry -> Kimi translation? Same big prefix, two runs."""
    big_prefix = Path(__file__).parent / "cache_prefix.txt"
    prefix_text = big_prefix.read_text() if big_prefix.exists() else (
        "You are investigating a NestJS + Postgres monolith. " * 400
    )

    async def _once(tag: str) -> dict[str, Any]:
        rec = RunRecorder(tag)
        async with ClaudeSDKClient(options=_options(None)) as client:
            await client.query(f"{prefix_text}\n\nQuestion: what is 2+2? Answer in one word.")
            async for msg in client.receive_response():
                if isinstance(msg, ResultMessage):
                    rec.result = msg
        return dict(rec.result.usage) if rec.result and rec.result.usage else {}

    run1 = await _once("cache-run1")
    run2 = await _once("cache-run2")
    cache_read_run2 = run2.get("cache_read_input_tokens", 0) or 0
    input_run2 = run2.get("input_tokens", 0) or 0
    total_in = cache_read_run2 + input_run2
    return {
        "name": "cache",
        "run1_usage": run1,
        "run2_usage": run2,
        "cache_read_run2": cache_read_run2,
        "cache_hit_ratio_run2": (cache_read_run2 / total_in) if total_in else 0.0,
        "caching_survives": cache_read_run2 > 0,
    }


# ---------------------------------------------------------------- matrix

def write_matrix(results: dict[str, Any]) -> None:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    (RESULTS_DIR / "results.json").write_text(json.dumps(results, indent=2, default=str))
    template = Path(__file__).parent / "DECISION_MATRIX.md"
    out = RESULTS_DIR / "DECISION_MATRIX.md"
    rendered = template.read_text()
    rendered = rendered.replace("{{GENERATED_AT}}", datetime.now(UTC).isoformat())
    rendered = rendered.replace("{{RESULTS_JSON}}", json.dumps(results, indent=2, default=str))
    out.write_text(rendered)
    print(f"[spike] decision matrix -> {out}")


async def main() -> None:
    parser = argparse.ArgumentParser(prog="tracer")
    parser.add_argument("command", choices=["ask", "structured", "soak", "interrupt", "cache", "all"])
    parser.add_argument("--golden", default=os.environ.get("GOLDEN_DIR", "/golden/repos"))
    parser.add_argument("--repo", default="ServerApp")
    parser.add_argument("--branch", default="main")
    args = parser.parse_args()

    for var in ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN"):
        if not os.environ.get(var):
            raise SystemExit(f"[spike] missing env {var} (gateway endpoint + key)")

    _start_servers()
    golden = Path(args.golden)
    results: dict[str, Any] = {}

    if args.command in ("ask", "all"):
        results["ask"] = await check_ask(golden, args.repo, args.branch)
    if args.command in ("structured", "all"):
        results["structured"] = await check_structured()
    if args.command in ("soak", "all"):
        results["soak"] = await check_soak(golden)
    if args.command in ("interrupt", "all"):
        results["interrupt"] = await check_interrupt(golden)
    if args.command in ("cache", "all"):
        results["cache"] = await check_cache()
    write_matrix(results)


if __name__ == "__main__":
    asyncio.run(main())
