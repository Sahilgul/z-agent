"""can_use_tool -> approval service bridge.

The SDK calls this and BLOCKS until the human (or thread policy) resolves. The
request is published to the backend on approvals:{run_id}; the decision comes back
on approval:{approval_id}:decision via BLPOP.

Supervised auto-allow floor (anti-approval-fatigue): read/grep/glob always pass
without a card; only writes, bash, git, and MCP mutations reach the human.
Timeout behavior is deterministic: timeout = DENY (+ notify), EXCEPT
Autonomous where nothing is bridged at all (bypassPermissions).
"""

from __future__ import annotations

import json
import time
import uuid

import redis.asyncio as redis

AUTO_ALLOW_TOOLS = frozenset({"Read", "Grep", "Glob", "LS", "WebSearch", "WebFetch", "TodoWrite"})
DENY_ON_TIMEOUT = True  # Supervised/Gated; Autonomous never reaches this bridge


class ApprovalBridge:
    def __init__(self, redis_url: str, run_id: str, thread_id: str, timeout_seconds: int = 900) -> None:
        self.redis = redis.from_url(redis_url, decode_responses=True)
        self.run_id = run_id
        self.thread_id = thread_id
        self.timeout_seconds = timeout_seconds
        self.always_allowed: set[str] = set()  # "Always Allow" persists the tool class for the run

    async def ask(self, tool_name: str, tool_input: dict, context) -> dict:
        """Signature matches the SDK can_use_tool callback; returns a PermissionResult."""
        from claude_agent_sdk import (
            PermissionResultAllow,
            PermissionResultDeny,
            ToolPermissionContext,
        )
        # L-10: the old `isinstance(context, ToolPermissionContext) or
        # context is not None` was tautological — `context is not None`
        # short-circircuited to True for any non-None context, so the
        # isinstance check was dead and None was the only thing that could
        # fail it. Assert the type directly.
        assert isinstance(context, ToolPermissionContext)

        if tool_name in AUTO_ALLOW_TOOLS or tool_name in self.always_allowed:
            return PermissionResultAllow(updated_input=tool_input)

        approval_id = str(uuid.uuid4())
        await self.redis.xadd(f"approvals:{self.run_id}", {
            "approval_id": approval_id,
            "thread_id": self.thread_id,
            "kind": "tool",
            "payload": json.dumps({"tool": tool_name, "input": tool_input}),
            "requested_at": str(time.time()),
        })
        decision_key = f"approval:{approval_id}:decision"
        try:
            result = await self.redis.blpop(decision_key, timeout=self.timeout_seconds)
        except redis.RedisError:
            # A dropped connection must not crash the can_use_tool callback
            # into the SDK — deterministic deny, same contract as timeout.
            return PermissionResultDeny(message="approval channel error — denied deterministically")
        if result is None:
            if DENY_ON_TIMEOUT:
                return PermissionResultDeny(message="approval timed out — denied deterministically")
            return PermissionResultAllow(updated_input=tool_input)

        _, raw = result
        try:
            decision = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return PermissionResultDeny(message="malformed decision payload — denied")
        if not isinstance(decision, dict):
            return PermissionResultDeny(message="malformed decision payload — denied")
        if decision.get("decision") == "always_allow":
            self.always_allowed.add(tool_name)
            return PermissionResultAllow(updated_input=tool_input)
        if decision.get("decision") in ("allow", "allow_once"):
            return PermissionResultAllow(updated_input=tool_input)
        return PermissionResultDeny(message=decision.get("reason", "denied by user"))

    async def close(self) -> None:
        await self.redis.aclose()
