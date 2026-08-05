"""Engine-side approval gate — the two-phase verbatim contract.

In SUPERVISED/GATED autonomy, a mutating tool call is NOT executed on the first
request. It is intercepted here, an approval-card StepEvent carrying the
VERBATIM command/edit is emitted, and the thread BLOCKS until the human
decides (Redis BLPOP). On approval, the tool executes with the verbatim args
from phase 1 (NOT any args the agent mutated meanwhile — the verbatim contract).

Verbatim contract rules enforced here:
  - always_allow persists the tool CLASS (file_edit, terminal_exec), never a
    specific file/command. A re-used always-allow on a new target is rejected.
  - DESTRUCTIVE commands never get always_allow — verbatim every time.
  - AUTONOMOUS: nothing is bridged (bypassPermissions).

The gate is per-run state (the always-allow set is per run, shared across the
run's threads via Redis). Timeout = DENY deterministically.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import redis.asyncio as redis

from worker.engine.tools.mutating import is_destructive_command

# Tool classes eligible for always_allow (NOT destructive — those never are).
_ALWAYS_ALLOWABLE_CLASSES = frozenset({"file_edit", "file_write", "terminal_exec"})
DENY_ON_TIMEOUT = True
_DEFAULT_TIMEOUT_S = 900  # 15 min


class ApprovalGate:
    """The two-phase verbatim gate. One per thread (the always-allow set is
    per-run, loaded from Redis on init)."""

    def __init__(self, redis_url: str, run_id: str, thread_id: str,
                 *, timeout_s: int = _DEFAULT_TIMEOUT_S) -> None:
        self.redis = redis.from_url(redis_url, decode_responses=True)
        self.run_id = run_id
        self.thread_id = thread_id
        self.timeout_s = timeout_s
        self._always_allowed: set[str] = set()
        self._event_sink: Any = None  # async callable, set by the runner

    def set_event_sink(self, sink: Any) -> None:
        self._event_sink = sink

    async def _load_always_allowed(self) -> None:
        """Load the per-run always-allow set from Redis (shared across threads)."""
        members = await self.redis.smembers(f"always_allow:{self.run_id}")
        self._always_allowed = set(members)

    async def _persist_always_allowed(self, tool_class: str) -> None:
        await self.redis.sadd(f"always_allow:{self.run_id}", tool_class)

    async def request(self, tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
        """The two-phase gate. Returns the verbatim args to execute with, or
        a denial dict. Call this BEFORE executing a mutating tool."""
        await self._load_always_allowed()

        # Destructive commands never get always_allow
        is_destructive = tool_name == "terminal_exec" and is_destructive_command(args.get("command", ""))
        if tool_name in self._always_allowed and not is_destructive:
            return {"approved": True, "args": args, "verbatim": True, "via": "always_allow"}

        # Phase 1: emit the verbatim approval card and block for the decision.
        approval_id = str(uuid.uuid4())
        verbatim_preview = _verbatim_preview(tool_name, args)
        await self._emit_approval_card(approval_id, tool_name, args, verbatim_preview, is_destructive)

        decision_key = f"approval:{approval_id}:decision"
        try:
            result = await self.redis.blpop(decision_key, timeout=self.timeout_s)
        except redis.RedisError as exc:
            # A dropped connection during the wait must not kill the run —
            # deterministic deny, same contract as a timeout.
            return {"approved": False, "args": args,
                    "reason": f"approval channel error — denied ({type(exc).__name__})"}
        if result is None:
            # Timeout = DENY deterministically
            if DENY_ON_TIMEOUT:
                return {"approved": False, "args": args, "reason": "approval timed out — denied"}
            return {"approved": True, "args": args, "verbatim": True, "via": "timeout"}

        _, raw = result
        try:
            decision = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return {"approved": False, "args": args, "reason": "malformed decision payload — denied"}
        if not isinstance(decision, dict):
            return {"approved": False, "args": args, "reason": "malformed decision payload — denied"}
        dec = decision.get("decision")
        if dec == "always_allow":
            # Persist the tool CLASS, not the specific target — but only for
            # allowable, non-destructive tools. For a non-allowable (or
            # destructive) tool the user STILL meant ALLOW; the old code
            # fell through to the DENY return, silently inverting intent (H-09).
            # Honor the allow for this call without persisting an unsafe
            # always-allow.
            if tool_name in _ALWAYS_ALLOWABLE_CLASSES and not is_destructive:
                self._always_allowed.add(tool_name)
                await self._persist_always_allowed(tool_name)
                return {"approved": True, "args": args, "verbatim": True, "via": "always_allow"}
            return {"approved": True, "args": args, "verbatim": True,
                    "via": "always_allow_unpersisted"}
        if dec in ("allow", "allow_once"):
            return {"approved": True, "args": args, "verbatim": True, "via": dec}
        return {"approved": False, "args": args, "reason": decision.get("reason", "denied by user")}

    async def _emit_approval_card(self, approval_id: str, tool_name: str,
                                  args: dict[str, Any], preview: str,
                                  is_destructive: bool) -> None:
        """Emit the approval-card StepEvent (Phase 1 preview)."""
        if self._event_sink is None:
            return
        from zagent_contracts import StepEvent, StepKind
        event = StepEvent(
            run_id=self.run_id, thread_id=self.thread_id, context_id=self.thread_id,
            seq=0,  # the runner's emitter allocates the real seq; this is a signal
            kind=StepKind.STATUS, title=f"approval: {tool_name}",
            detail={
                "approval_id": approval_id,
                "tool": tool_name,
                "args": args,
                "preview": preview,
                "destructive": is_destructive,
                "kind": "approval_card",
            },
        )
        await self._event_sink([event])

    async def close(self) -> None:
        await self.redis.aclose()


class ApprovalBroker:
    """Runner-side broker for the INTERRUPT-DRIVEN approval flow.

    The graph's gate node calls interrupt(card_payload) — LangGraph persists
    the checkpoint and halts. The RUNNER (the only place allowed to block)
    emits the card, waits for the human's decision on Redis, and resumes the
    graph with Command(resume=decision). Because the decision crosses the
    checkpoint boundary, approvals survive container replacement (the
    Redis driver: interrupt -> publish -> await -> resume).

    Decision contract (the resume value):
      {"decision": "allow" | "always_allow" | "deny", "reason": str?}
    Timeout = DENY deterministically; the runner synthesizes the
    deny decision and resumes with it — the gate node treats it like any denial.
    """

    def __init__(self, redis_url: str, run_id: str, thread_id: str,
                 *, timeout_s: int = _DEFAULT_TIMEOUT_S) -> None:
        self.redis = redis.from_url(redis_url, decode_responses=True)
        self.run_id = run_id
        self.thread_id = thread_id
        self.timeout_s = timeout_s

    def card_payload(self, tool_name: str, args: dict[str, Any], tool_call_id: str) -> dict[str, Any]:
        """The interrupt payload — everything the runner needs to render the card."""
        is_destructive = tool_name == "terminal_exec" and is_destructive_command(args.get("command", ""))
        return {
            "type": "approval_request",
            "approval_id": str(uuid.uuid4()),
            "tool_call_id": tool_call_id,
            "tool": tool_name,
            "args": args,
            "preview": _verbatim_preview(tool_name, args),
            "destructive": is_destructive,
            "always_allowable": tool_name in _ALWAYS_ALLOWABLE_CLASSES and not is_destructive,
        }

    async def is_always_allowed(self, tool_name: str, args: dict[str, Any]) -> bool:
        """The always-allow set is per-run, class-scoped; destructive never."""
        is_destructive = tool_name == "terminal_exec" and is_destructive_command(args.get("command", ""))
        if is_destructive:
            return False
        members = await self.redis.smembers(f"always_allow:{self.run_id}")
        return tool_name in set(members)

    async def persist_always_allow(self, tool_name: str) -> None:
        await self.redis.sadd(f"always_allow:{self.run_id}", tool_name)

    async def wait_decision(self, approval_id: str) -> dict[str, Any]:
        """Block for the human's decision. Timeout = DENY deterministically.

        A Redis connection error during the (potentially 15-min) BLPOP is a
        transient failure, not a run-killer: deny deterministically, matching
        the timeout contract, instead of propagating and failing the run."""
        try:
            result = await self.redis.blpop(f"approval:{approval_id}:decision", timeout=self.timeout_s)
        except redis.RedisError as exc:
            return {"decision": "deny",
                    "reason": f"approval channel error — denied ({type(exc).__name__})"}
        if result is None:
            return {"decision": "deny", "reason": "approval timed out — denied"}
        _, raw = result
        try:
            decision = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return {"decision": "deny", "reason": "malformed decision payload — denied"}
        if decision.get("decision") not in ("allow", "allow_once", "always_allow",
                                            "edited_allow", "deny"):
            return {"decision": "deny", "reason": "unknown decision — denied"}
        return decision

    async def close(self) -> None:
        await self.redis.aclose()


def _verbatim_preview(tool_name: str, args: dict[str, Any]) -> str:
    """The verbatim text the human sees — the EXACT thing that will execute."""
    if tool_name == "terminal_exec":
        return f"$ {args.get('command', '')}"
    if tool_name == "file_edit":
        return (f"edit {args.get('file_path', '')}\n"
                f"--- old ---\n{args.get('old_string', '')[:2000]}\n"
                f"--- new ---\n{args.get('new_string', '')[:2000]}")
    if tool_name == "file_write":
        return f"write {args.get('file_path', '')}\n--- content ---\n{args.get('content', '')[:4000]}"
    return f"{tool_name} {json.dumps(args, default=str)[:2000]}"


__all__ = ["ApprovalBroker", "ApprovalGate"]
