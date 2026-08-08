"""Event emitter — engine state -> canonical StepEvents.

The single bridge from the LangGraph loop to the StepEvent contract.
Every tool call, assistant message, and turn boundary becomes exactly one
StepEvent. Tool outputs are redacted HERE (not inside tools) so the agent
keeps raw outputs for reasoning while events carry only redacted text.

This is the read-only emitter; the approval-card event
type and the two-phase verbatim approval flow extend it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from collegium_contracts import StepEvent, StepKind
from langchain_core.messages import AIMessage, ToolMessage

from worker.engine.security import redact, redact_dict


class EventEmitter:
    """Allocates seq, pairs tool_use with tool_result, emits StepEvents.

    One emitter per thread (per task, really — re-instantiated per turn so seq
    is monotonic across the thread's lifetime via the state's compacted_event_ids).
    """

    def __init__(self, run_id: str, thread_id: str, context_id: str | None = None,
                 seq_store: Path | None = None) -> None:
        self.run_id = run_id
        self.thread_id = thread_id
        self.context_id = context_id or thread_id
        # D2: seq is single-sourced PER THREAD and must survive container
        # replacement — otherwise the replacement restarts at seq=0 and the
        # backend's unique (run_id, thread_id, seq) constraint dedupes the
        # whole replayed prefix into oblivion. The store lives on the durable
        # session volume (CHECKPOINT_MIRROR_DIR).
        self._seq_store = seq_store
        self._seq = self._load_seq()
        self._pending_tools: dict[str, dict[str, Any]] = {}

    def _load_seq(self) -> int:
        if self._seq_store is None:
            return 0
        try:
            return int(self._seq_store.read_text().strip())
        except (OSError, ValueError):
            return 0

    def _persist_seq(self) -> None:
        if self._seq_store is None:
            return
        import os
        tmp = self._seq_store.with_suffix(".seq.tmp")
        try:
            tmp.write_text(str(self._seq))
            os.replace(tmp, self._seq_store)
        except OSError:
            pass  # a persist miss degrades to a redelivery the DB dedupes

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
        self._persist_seq()
        return event

    # --- Dedicated approval StepKind + action_id pairing ---

    def approval_card(self, payload: dict[str, Any], task_id: str | None) -> StepEvent:
        """The approval-card StepEvent — kind=APPROVAL (replaces the old
        STATUS/seq=0 card). action_id pairs the card with its decision event."""
        approval_id = str(payload.get("approval_id", ""))
        return self._next(
            StepKind.APPROVAL, f"approval: {payload.get('tool', '?')}",
            {
                "kind": "approval_card",
                "action_id": approval_id,
                "approval_id": approval_id,
                "tool": payload.get("tool"),
                "args": payload.get("args"),
                "preview": payload.get("preview"),
                "destructive": payload.get("destructive", False),
                "always_allowable": payload.get("always_allowable", False),
            },
            task_id, None,
        )

    def approval_decision(self, approval_id: str, decision: dict[str, Any],
                          task_id: str | None) -> StepEvent:
        """The paired decision event — same action_id as the card."""
        verdict = decision.get("decision", "deny")
        return self._next(
            StepKind.APPROVAL, f"approval {verdict}: {decision.get('tool', '')}".rstrip(": "),
            {
                "kind": "approval_decision",
                "action_id": approval_id,
                "approval_id": approval_id,
                "decision": verdict,
                "reason": decision.get("reason"),
                "edited": "edited_args" in decision,
            },
            task_id, None,
        )

    def from_assistant(self, msg: AIMessage, task_id: str | None) -> list[StepEvent]:
        """Turn an AIMessage into StepEvents (thinking/text + pending tool uses)."""
        events: list[StepEvent] = []
        sdk_uuid = getattr(msg, "id", None) or (getattr(msg, "usage_metadata", None) and None)
        content = msg.content if isinstance(msg.content, list) else [msg.content] if msg.content else []

        # OpenAI-compatible reasoning models (Kimi-K2, DeepSeek-R1, …) return
        # their chain-of-thought in the non-standard ``reasoning_content`` field,
        # which ChatOpenAIReasoning preserves into additional_kwargs. Surface it
        # as a THINKING event BEFORE the message text so the transcript reads in
        # the order the model produced it (think, then answer).
        reasoning = (msg.additional_kwargs or {}).get("reasoning_content")
        if reasoning and str(reasoning).strip():
            events.append(self._next(
                StepKind.THINKING, "thinking…",
                {"text": redact(str(reasoning))}, task_id, sdk_uuid,
            ))

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
