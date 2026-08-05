"""Tools package — capability registry + idempotency keys (plan §6, §15 plumbing).

The capability registry is the single source of truth for what each tool can
do (read-only vs mutating, needs-approval, mcp-sourced). Phase 3 adds the
mutating tools with the two-phase verbatim approval contract.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from worker.engine.tools.mutating import (
    MUTATING_TOOL_BY_NAME,
    MUTATING_TOOLS,
    call_mutating_tool,
    content_hash,
    is_destructive_command,
)
from worker.engine.tools.readonly import READONLY_TOOLS, TOOL_BY_NAME, call_tool


class Capability(str, Enum):
    """What a tool is allowed to do — drives the approval gate."""

    READONLY = "readonly"          # always allowed (no approval)
    MUTATING = "mutating"          # needs approval (Phase 3)
    DESTRUCTIVE = "destructive"    # needs verbatim approval every time (Phase 3)
    MCP = "mcp"                    # external; capability per-tool


# Tool capability map. terminal_exec is MUTATING (it can run any command);
# a specific call is DESTRUCTIVE if is_destructive_command(command) is true.
_TOOL_CAPABILITIES: dict[str, Capability] = {
    "file_read": Capability.READONLY,
    "file_search": Capability.READONLY,
    "file_glob": Capability.READONLY,
    "file_edit": Capability.MUTATING,
    "file_write": Capability.MUTATING,
    "terminal_exec": Capability.MUTATING,
    "memory_search": Capability.READONLY,
    "ask_user": Capability.READONLY,
    "spawn_agent": Capability.MUTATING,
    "spawn_swarm": Capability.MUTATING,
    # RC extended set (§7). knowledge_draft is MUTATING (repo|global scopes are
    # human-gated); the gate auto-allows scope=user per the T4 contract.
    "web_fetch": Capability.READONLY,
    "git_snapshot": Capability.READONLY,
    "update_tasks": Capability.READONLY,
    "compact": Capability.READONLY,
    "knowledge_draft": Capability.MUTATING,
    # RD deferred set (Phase 10, R34). file_delete is DESTRUCTIVE: verbatim
    # approval every time. mode_request is MUTATING (approval-routed §8 path).
    "tool_search": Capability.READONLY,
    "web_search": Capability.READONLY,
    "file_delete": Capability.DESTRUCTIVE,
    "terminal_await": Capability.READONLY,
    "playbook_load": Capability.READONLY,
    "mode_request": Capability.MUTATING,
}


def capability_of(tool_name: str) -> Capability:
    return _TOOL_CAPABILITIES.get(tool_name, Capability.READONLY)


def is_mutable_capability(tool_name: str, args: dict[str, Any] | None = None) -> bool:
    """True if the call needs the approval gate. terminal_exec is destructive
    when its command matches the destructive regex."""
    cap = capability_of(tool_name)
    if cap == Capability.READONLY:
        return False
    if cap == Capability.MCP:
        return True
    if tool_name == "terminal_exec" and args:
        return True  # mutating; destructiveness checked at the gate
    return cap in (Capability.MUTATING, Capability.DESTRUCTIVE)


def needs_approval(tool_name: str, autonomy: str,
                   args: dict[str, Any] | None = None,
                   ruleset: list[dict[str, Any]] | None = None) -> bool:
    """Whether a tool call needs human approval given the autonomy level.

    SUPERVISED/GATED: mutating/destructive need approval; readonly never does.
    AUTONOMOUS: nothing is bridged (bypassPermissions) — the gateway per-key
    budget is the only backstop.

    RC: glob rulesets (permissions.py, findLast precedence) refine the
    capability default per-call — an explicit allow rule bypasses the card,
    an ask rule forces one, deny never reaches this function (the gate
    rejects it first).
    """
    if autonomy == "autonomous":
        return False
    cap_default = capability_of(tool_name) in (Capability.MUTATING, Capability.DESTRUCTIVE)
    if ruleset:
        from worker.engine.permissions import Effect, decision_for_call
        _effect, needs = decision_for_call(tool_name, args, ruleset,
                                           capability_default_needs_approval=cap_default)
        if _effect is Effect.DENY:
            return False  # denied outright — the gate short-circuits before approval
        return needs
    return cap_default


# --- The unified tool registry (read-only + mutating) ---

ALL_TOOLS = READONLY_TOOLS + MUTATING_TOOLS
ALL_TOOL_BY_NAME: dict[str, Any] = {t.name: t for t in ALL_TOOLS}


def _extra_tools() -> dict[str, Any]:
    """Tools built outside tools/ (memory, goal, fan-out, extended RC set) —
    imported lazily to avoid a circular import."""
    from worker.engine.fanout import spawn_agent, spawn_swarm
    from worker.engine.goal_mode import ask_user
    from worker.engine.memory import memory_search
    from worker.engine.tools.deferred import DEFERRED_TOOLS
    from worker.engine.tools.discovery import tool_search
    from worker.engine.tools.extended import EXTENDED_TOOLS
    return {t.name: t for t in (memory_search, ask_user, spawn_agent, spawn_swarm,
                                tool_search, *EXTENDED_TOOLS, *DEFERRED_TOOLS)}


# Every built tool, unique by name (the mutating terminal_exec wins over the
# read-only variant). ALL_TOOLS keeps its Phase-3 shape for the §17 contract
# tests; ALL_BUILT_TOOLS is the RA interim registry (bound mode-aware).
def _dedupe_by_name(tools: list[Any]) -> list[Any]:
    seen: dict[str, Any] = {}
    for t in tools:
        seen[t.name] = t  # later entries win (mutating terminal_exec > read-only)
    return list(seen.values())


ALL_BUILT_TOOLS: list[Any] = _dedupe_by_name(ALL_TOOLS + list(_extra_tools().values()))
ALL_BUILT_TOOL_BY_NAME: dict[str, Any] = {t.name: t for t in ALL_BUILT_TOOLS}

# §7/RD Tier-0 name parity: the ripgrep search tool is named `code_search` in
# the plan contracts; the langchain tool object is `file_search`. Alias at the
# registry level so either name resolves (RD's DEFAULT_TOOLS uses code_search).
_NAME_ALIASES = {"code_search": "file_search"}


def resolve_tool_name(name: str) -> str:
    return _NAME_ALIASES.get(name, name)


# --- R34 two-tier surface ---

# Tier 0 — bound every turn (mode-intersected). Contract names; code_search
# resolves to the file_search tool object.
DEFAULT_TOOLS: list[str] = [
    "file_read", "file_edit", "file_write", "terminal_exec",
    "code_search", "file_glob", "update_tasks", "tool_search",
]

# §8 mode tool_filter (fail-closed: denied tools are absent from binding AND
# from the tool_search index + D6 roster). "mcp__*" gates the MCP fold-in.
_READ_SET = {
    "file_read", "file_search", "file_glob", "terminal_exec", "update_tasks",
    "compact", "web_fetch", "web_search", "git_snapshot", "memory_search",
    "tool_search", "playbook_load", "mode_request", "mcp__*",
}
_DEV_SET = _READ_SET | {
    "file_edit", "file_write", "file_delete", "terminal_await",
    "knowledge_draft", "spawn_agent", "spawn_swarm",
}
MODE_ALLOWED: dict[str, set[str]] = {
    "ask": set(_READ_SET),
    "plan": set(_READ_SET),
    "development": set(_DEV_SET),
    "debug": set(_DEV_SET),
    "goal": _DEV_SET | {"ask_user"},
}


def mode_allowed(name: str, mode: Any) -> bool:
    """§8 tool_filter — the single fail-closed check used by binding, the
    discovery index, and the roster."""
    mode_val = mode.value if hasattr(mode, "value") else str(mode)
    allowed = MODE_ALLOWED.get(mode_val, MODE_ALLOWED["ask"])
    if name.startswith("mcp__"):
        return "mcp__*" in allowed
    return resolve_tool_name(name) in allowed


# Mode-gated default additions (§8): fan-out in development/goal. ask_user is
# DEFERRED (discoverable) so the goal default bind stays <=10 schemas (P10).
_MODE_DEFAULT_ADDITIONS: dict[str, list[str]] = {
    "development": ["spawn_agent", "spawn_swarm"],
    "goal": ["spawn_agent", "spawn_swarm"],
}


def default_tool_names(mode: Any) -> list[str]:
    """Tier-0 names for a mode: DEFAULT_TOOLS ∩ mode-allowed + additions."""
    mode_val = mode.value if hasattr(mode, "value") else str(mode)
    names = [n for n in DEFAULT_TOOLS if mode_allowed(n, mode)]
    names += _MODE_DEFAULT_ADDITIONS.get(mode_val, [])
    return names


def tools_for_mode(mode: Any) -> list[Any]:
    """R34 Tier-0 binding: DEFAULT_TOOLS(mode) + mode-gated additions — NEVER
    the full registry. The agent node adds state.discovered_tools on top."""
    tools: list[Any] = []
    for name in default_tool_names(mode):
        t = ALL_BUILT_TOOL_BY_NAME.get(resolve_tool_name(name))
        if t is not None and t not in tools:
            tools.append(t)
    return tools


async def call_any_tool(name: str, args: dict[str, Any],
                        *, approval_gate: Any = None, autonomy: str = "supervised") -> dict[str, Any]:
    """Dispatch any tool. Mutating calls go through the approval gate first
    (§3 two-phase verbatim). The gate returns the verbatim args to execute
    with; we call the tool with THOSE args, not the agent's original."""
    if name in TOOL_BY_NAME:
        return await call_tool(name, args)
    if name in MUTATING_TOOL_BY_NAME:
        if approval_gate is not None and needs_approval(name, autonomy):
            decision = await approval_gate.request(name, args)
            if not decision["approved"]:
                return {"kind": "error", "ok": False, "output": f"error: {decision.get('reason', 'denied')}",
                        "tool": name, "args": args}
            # §3: execute with the VERBATIM args from the gate
            return await call_mutating_tool(name, decision["args"])
        return await call_mutating_tool(name, args)
    extra = _extra_tools()
    if name in extra:
        return await _call_extra_tool(extra[name], name, args)
    return {"kind": "error", "ok": False, "output": f"unknown tool: {name}"}


async def call_tool_direct(name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Dispatch WITHOUT the approval gate — the graph's gate node has already
    decided (RA). Mutating tools execute with the gate's verbatim args.
    mcp__* routes to the MCP manager (R24#7 per-server isolation); the RC
    extended set routes through its async-aware shim (web_fetch is a coroutine)."""
    name = resolve_tool_name(name)
    if name.startswith("mcp__"):
        from worker.engine.mcp import mcp_manager
        return await mcp_manager().call(name, args)
    if name == "tool_search":
        from worker.engine.tools.discovery import tool_search_async
        return await tool_search_async(args, mode="development",
                                       bound=default_tool_names("development"))
    from worker.engine.tools.extended import EXTENDED_TOOL_BY_NAME, call_extended_tool
    if name in EXTENDED_TOOL_BY_NAME:
        return await call_extended_tool(name, args)
    from worker.engine.tools.deferred import DEFERRED_TOOL_BY_NAME, call_deferred_tool
    if name in DEFERRED_TOOL_BY_NAME:
        return await call_deferred_tool(name, args)
    if name in TOOL_BY_NAME:
        return await call_tool(name, args)
    if name in MUTATING_TOOL_BY_NAME:
        return await call_mutating_tool(name, args)
    extra = _extra_tools()
    if name in extra:
        return await _call_extra_tool(extra[name], name, args)
    return {"kind": "error", "ok": False, "output": f"unknown tool: {name}"}


async def _call_extra_tool(t: Any, name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Invoke a langchain @tool with the error-prefix result convention."""
    import asyncio
    try:
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(None, lambda: t.invoke(args))
        output = str(result)
        is_error = output.startswith("error:")
        return {
            "kind": "error" if is_error else "success",
            "ok": not is_error,
            "output": output,
            "tool": name,
            "args": args,
        }
    except Exception as exc:  # noqa: BLE001
        return {"kind": "error", "ok": False, "output": f"error: {exc}", "tool": name, "args": args}


# --- Idempotency keys (plan §15) ---

def idempotency_key(run_id: str, thread_id: str, task_id: str, tool_name: str, args: dict[str, Any]) -> str:
    """Stable idempotency key so a retried tool call doesn't double-execute."""
    import hashlib
    import json
    args_blob = json.dumps(args, sort_keys=True, default=str)
    h = hashlib.sha256(f"{run_id}|{thread_id}|{task_id}|{tool_name}|{args_blob}".encode()).hexdigest()[:16]
    return f"idem-{h}"


__all__ = [
    "ALL_BUILT_TOOLS",
    "ALL_BUILT_TOOL_BY_NAME",
    "ALL_TOOLS",
    "ALL_TOOL_BY_NAME",
    "DEFAULT_TOOLS",
    "MODE_ALLOWED",
    "MUTATING_TOOLS",
    "MUTATING_TOOL_BY_NAME",
    "READONLY_TOOLS",
    "TOOL_BY_NAME",
    "Capability",
    "call_any_tool",
    "call_mutating_tool",
    "call_tool",
    "call_tool_direct",
    "capability_of",
    "content_hash",
    "default_tool_names",
    "idempotency_key",
    "is_destructive_command",
    "is_mutable_capability",
    "mode_allowed",
    "needs_approval",
    "resolve_tool_name",
    "tools_for_mode",
]
