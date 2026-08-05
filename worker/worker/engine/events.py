"""Event emitter — engine state -> canonical StepEvents (plan §6 events.py).

The single bridge from the LangGraph loop to the StepEvent contract (Phase 1).
Every tool call, assistant message, and turn boundary becomes exactly one
StepEvent. Tool outputs are redacted HERE (not inside tools) so the agent
keeps raw outputs for reasoning while events carry only redacted text.

This is the Phase 2 read-only emitter; Phase 3 adds the approval-card event
type and the two-phase verbatim approval flow.
"""

from __future__ import annotations

from typing import Any

from langchain_core.messages import AIMessage, ToolMessage
from zagent_contracts import StepEvent, StepKind

from worker.engine.security import redact, redact_dict


class EventEmitter:
    """Allocates seq, pairs tool_use with tool_result, emits StepEvents.

    One emitter per thread (per task, really — re-instantiated per turn so seq
    is monotonic across the thread's lifetime via the state's compacted_event_ids).
    """

    def __init__(self, run_id: str, thread_id: str, context_id: str | None = None) -> None:
        self.run_id = run_id
        self.thread_id = thread_id
        self.context_id = context_id or thread_id
        self._seq = 0
        self._pending_tools: dict[str, dict[str, Any]] = {}

    def _next(self, kind: StepKind, title: str, detail: dict[str, Any],
              task_id: str | None, sdk_uuid: str | None) -> StepEvent:
        event = StepEvent(
            run_id=self.run_id,
            thread_id=self.thread_id,
            context_id=self.context_id,
            task_id=task_id,
            seq=self._seq,
            kind=kind,
            title=title,
            detail=detail,
            sdk_message_uuid=sdk_uuid,
        )
        self._seq += 1
        return event

    def from_assistant(self, msg: AIMessage, task_id: str | None) -> list[StepEvent]:
        """Turn an AIMessage into StepEvents (thinking/text + pending tool uses)."""
        events: list[StepEvent] = []
        sdk_uuid = getattr(msg, "id", None) or getattr(msg, "usage_metadata", None) and None
        content = msg.content if isinstance(msg.content, list) else [msg.content] if msg.content else []

        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "thinking":
                    events.append(self._next(
                        StepKind.THINKING, "thinking…",
                        {"text": redact(str(block.get("thinking", "")))}, task_id, sdk_uuid,
                    ))
                elif block.get("type") == "text":
                    text = redact(str(block.get("text", "")))
                    events.append(self._next(
                        StepKind.MESSAGE, text.splitlines()[0][:120] if text else "message",
                        {"text": text}, task_id, sdk_uuid,
                    ))
            elif isinstance(block, str) and block.strip():
                text = redact(block)
                events.append(self._next(
                    StepKind.MESSAGE, text.splitlines()[0][:120], {"text": text}, task_id, sdk_uuid,
                ))

        for tc in (msg.tool_calls or []):
            name = tc.get("name", "")
            args = tc.get("args", {}) or {}
            self._pending_tools[tc.get("id", "")] = {"name": name, "args": args, "uuid": sdk_uuid}
            events.append(self._next(
                _tool_kind(name), _tool_title(name, args),
                {"tool": name, "input": redact_dict(args)}, task_id, sdk_uuid,
            ))

        return events

    def from_tool_result(self, msg: ToolMessage, task_id: str | None) -> StepEvent:
        """Pair a ToolMessage with its pending tool_use -> one complete event."""
        tc_id = msg.tool_call_id
        pending = self._pending_tools.pop(tc_id, None)
        output = redact(str(msg.content))
        if pending is None:
            return self._next(
                StepKind.STATUS, "tool result (unpaired)",
                {"output": output}, task_id, None,
            )
        name = pending["name"]
        kind = _tool_kind(name)
        if name == "terminal_exec" and _looks_like_test(pending["args"]):
            kind = StepKind.TEST_RUN
        return self._next(
            kind, _tool_title(name, pending["args"]),
            {
                "tool": name,
                "input": redact_dict(pending["args"]),
                "output": output,
                "ok": not _is_error(msg),
            },
            task_id, pending["uuid"],
        )

    def turn_boundary(self, task_id: str, *, num_turns: int, duration_ms: int,
                      is_error: bool, usage: dict[str, Any] | None) -> StepEvent:
        return self._next(
            StepKind.STATUS, "turn complete",
            {
                "num_turns": num_turns, "duration_ms": duration_ms,
                "is_error": is_error, "usage": usage,
            },
            task_id, None,
        )


def _tool_kind(name: str) -> StepKind:
    if name.startswith("mcp__"):
        return StepKind.MCP_CALL
    return {
        "file_read": StepKind.FILE_READ,
        "file_search": StepKind.COMMAND,
        "file_glob": StepKind.COMMAND,
        "terminal_exec": StepKind.COMMAND,
    }.get(name, StepKind.COMMAND)


def _tool_title(name: str, args: dict[str, Any]) -> str:
    if name == "terminal_exec":
        return f"$ {str(args.get('command', ''))[:120]}"
    if name in ("file_read", "file_search", "file_glob"):
        return f"{name} {args.get('file_path') or args.get('pattern') or ''}"
    if name.startswith("mcp__"):
        return name.replace("mcp__", "").replace("__", " / ")
    return name


def _looks_like_test(args: dict[str, Any]) -> bool:
    cmd = str(args.get("command", ""))
    return any(t in cmd for t in ("pytest", "npm test", "vitest", "jest", "pnpm test"))


def _is_error(msg: ToolMessage) -> bool:
    return bool(getattr(msg, "status", None) == "error")


__all__ = ["EventEmitter"]
