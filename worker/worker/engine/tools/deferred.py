"""RD deferred tools (Phase 10, R34): web_search, file_delete, terminal_await,
playbook_load, mode_request. All DEFERRED tier — never bound by default,
loaded via tool_search. (tool_search itself lives in discovery.py and is
Tier 0.)
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

from langchain_core.tools import tool

from worker.engine.security import wrap_untrusted
from worker.engine.tools.readonly import _resolve as _resolve_path

AWAIT_TIMEOUT_DEFAULT_S = 120.0
# R24#1 odd-interval poll rules: irregular cadence, never a fixed tight loop.
POLL_CADENCE_S = (1.0, 2.0, 3.0, 5.0, 7.0, 9.0, 10.0)


@tool
def web_search(query: str, max_results: int = 5) -> str:
    """Search the web via the gateway search provider. Results pass through
    security quarantine like web_fetch (data, never instructions).

    Args:
        query: the search query.
        max_results: cap on returned results (default 5).
    """
    return "ok: dispatched by the engine (async shim below)"


@tool
def file_delete(file_path: str) -> str:
    """Delete a file in the workspace. DESTRUCTIVE — requires verbatim human
    approval every time (same posture as destructive terminal_exec).

    Args:
        file_path: path to delete, relative to the workspace root.
    """
    try:
        p = _resolve_path(file_path)
    except ValueError as exc:
        return str(exc)
    if not p.exists():
        return f"error: no such file: {file_path}"
    if p.is_dir():
        return f"error: file_delete only removes files, not directories: {file_path}"
    try:
        p.unlink()
    except OSError as exc:
        return f"error: delete failed: {exc}"
    return f"deleted {file_path}"


@tool
async def terminal_await(job_id: str, pattern: str | None = None,
                         timeout_s: float = AWAIT_TIMEOUT_DEFAULT_S) -> str:
    """Block on a terminal_exec background job until it exits OR its output
    matches a regex pattern. Polls on an irregular cadence (R24#1).

    Args:
        job_id: the background job id from terminal_exec.
        pattern: optional regex — return as soon as the output matches.
        timeout_s: give up after this many seconds (default 120) and report
            the job is still running (it is NOT killed).
    """
    import re as _re

    from worker.engine.tools.background import terminal_manager
    mgr = terminal_manager()
    job = mgr.jobs.get(job_id)
    if job is None:
        return f"error: unknown job id {job_id}"
    rx = _re.compile(pattern) if pattern else None
    deadline = asyncio.get_running_loop().time() + timeout_s
    i = 0
    while asyncio.get_running_loop().time() < deadline:
        if not job.running:
            return mgr.render(job_id)
        if rx:
            haystack = "\n".join(job.ring)
            if rx.search(haystack):
                return f"[pattern matched: {pattern}]\n" + mgr.render(job_id, tail=100)
        await asyncio.sleep(POLL_CADENCE_S[min(i, len(POLL_CADENCE_S) - 1)])
        i += 1
    return f"[still running after {timeout_s:.0f}s — job NOT killed]\n" + mgr.render(job_id)


@tool
def playbook_load(name: str) -> str:
    """Load a T5 procedural playbook into context by name. Playbooks are
    versioned team procedures (§9 T5 precedence/routing rules apply).

    Args:
        name: the playbook name (without .md).
    """
    pdir = Path(__file__).resolve().parent.parent / "prompts" / "playbooks"
    if not re_fullmatch_safe(name):
        return "error: playbook name must be alphanumeric/dash/underscore"
    path = pdir / f"{name}.md"
    if not path.exists():
        available = sorted(p.stem for p in pdir.glob("*.md")) if pdir.exists() else []
        return (f"error: unknown playbook {name!r}. available: {available or '(none)'}")
    content = path.read_text(encoding="utf-8")
    return f'<playbook name="{name}">\n{content}\n</playbook>'


def re_fullmatch_safe(name: str) -> bool:
    import re as _re
    return bool(_re.fullmatch(r"[A-Za-z0-9_\-]+", name or ""))


@tool
def mode_request(target_mode: str, reason: str) -> str:
    """REQUEST a mode transition (never a silent self-switch). Routed through
    the audited §8 transition path: team policy + authorizer identity apply;
    the request is approval-routed — a human approves before the mode changes.

    Args:
        target_mode: ask | plan | development | debug | goal.
        reason: why the transition is needed (shown on the approval card).
    """
    return "ok: dispatched by the engine (the gate owns the transition)"


# --- async-aware dispatch shim (extended.py parity) ---

DEFERRED_TOOLS: list[Any] = [web_search, file_delete, terminal_await,
                             playbook_load, mode_request]
DEFERRED_TOOL_BY_NAME: dict[str, Any] = {t.name: t for t in DEFERRED_TOOLS}


async def call_deferred_tool(name: str, args: dict[str, Any]) -> dict[str, Any]:
    t = DEFERRED_TOOL_BY_NAME.get(name)
    if t is None:
        return {"kind": "error", "ok": False, "output": f"unknown tool: {name}"}
    try:
        if name == "web_search":
            return await _web_search(args)
        if getattr(t, "coroutine", None) is not None:
            result = await t.ainvoke(args)
        else:
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(None, lambda: t.invoke(args))
        output = str(result)
        is_error = output.startswith("error:")
        return {"kind": "error" if is_error else "success", "ok": not is_error,
                "output": output, "tool": name, "args": args}
    except Exception as exc:  # noqa: BLE001
        return {"kind": "error", "ok": False, "output": f"error: {exc}",
                "tool": name, "args": args}


async def _web_search(args: dict[str, Any]) -> dict[str, Any]:
    """Search provider via the gateway (R34): POST ZAGENT_SEARCH_ENDPOINT.
    Unconfigured endpoint is a typed error, never a crash."""
    endpoint = os.environ.get("ZAGENT_SEARCH_ENDPOINT", "").strip()
    if not endpoint:
        return {"kind": "error", "ok": False,
                "output": "error: web_search provider not configured "
                          "(set ZAGENT_SEARCH_ENDPOINT on the gateway)",
                "tool": "web_search", "args": args}
    query = str(args.get("query", "")).strip()
    if not query:
        return {"kind": "error", "ok": False, "output": "error: empty query",
                "tool": "web_search", "args": args}
    import httpx
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(endpoint, json={
                "query": query, "max_results": int(args.get("max_results", 5))})
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:  # noqa: BLE001
        return {"kind": "error", "ok": False, "output": f"error: web_search failed: {exc}",
                "tool": "web_search", "args": args}
    results = data.get("results", []) if isinstance(data, dict) else []
    lines = [f"- {r.get('title', '?')} — {r.get('url', '')}\n  {r.get('snippet', '')[:200]}"
             for r in results[: int(args.get("max_results", 5))]]
    body = "\n".join(lines) if lines else "(no results)"
    return {"kind": "success", "ok": True,
            "output": wrap_untrusted(body, source="web_search"),
            "tool": "web_search", "args": args}


__all__ = ["DEFERRED_TOOLS", "DEFERRED_TOOL_BY_NAME", "POLL_CADENCE_S",
           "call_deferred_tool", "file_delete", "mode_request", "playbook_load",
           "terminal_await", "web_search"]
