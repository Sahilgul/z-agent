"""SDK messages -> canonical StepEvent, at the worker edge.

One Normalizer instance per thread. A StepEvent is emitted ONCE, COMPLETE, at step
end: tool_use blocks are held until their tool_result arrives, then one event
carrying input+output is produced. Streaming fragments ride TypingDelta only.
"""

from __future__ import annotations

from typing import Any

# The CAS SDK is an OPTIONAL extra (worker[cas]). The new LangGraph engine does
# not use this Normalizer (it has its own event emitter). Import the message
# types lazily so this module imports cleanly without the cas extra installed.
try:
    from claude_agent_sdk import (
        AssistantMessage,
        ResultMessage,
        SystemMessage,
        TextBlock,
        ThinkingBlock,
        ToolResultBlock,
        ToolUseBlock,
        UserMessage,
    )
except ImportError:  # pragma: no cover
    AssistantMessage = ResultMessage = SystemMessage = None  # type: ignore[assignment,misc]
    TextBlock = ThinkingBlock = ToolResultBlock = None  # type: ignore[assignment,misc]
    ToolUseBlock = UserMessage = None  # type: ignore[assignment,misc]

import uuid as _uuid

from collegium_contracts import StepEvent, StepKind, TypingDelta

# W-H12: the flag-gated SDK path must redact EXACTLY like the custom engine
# (D5 parity) — events carry only redacted text, whatever produced them.
from worker.engine.security import redact, redact_dict

TOOL_KIND_MAP = {
    "Bash": StepKind.COMMAND,
    "Read": StepKind.FILE_READ,
    "Edit": StepKind.FILE_EDIT,
    "Write": StepKind.FILE_EDIT,
    "Grep": StepKind.COMMAND,
    "Glob": StepKind.COMMAND,
}

# M-21: MCP test-runner tools (mcp__pytest__run, mcp__vitest__run, …) used to
# all classify as MCP_CALL, so the UI never rendered them as test runs. Match
# these tokens in the mcp__ tool name and classify as TEST_RUN. Avoid bare
# "test" (matches "latest", "contest", …).
_MCP_TEST_TOKENS = (
    "pytest", "vitest", "jest", "test_runner", "run_tests", "test_run", "run_test",
)

# System subtypes that fire once per turn and carry no investigative value —
# they're plumbing, not progress, so they never become transcript lines.
_NOISY_SYSTEM_SUBTYPES = {"init", "thinking_tokens"}


def _tool_kind(name: str) -> StepKind:
    if name.startswith("mcp__"):
        # M-21: classify MCP test-runner tools as TEST_RUN (was MCP_CALL).
        lower = name.lower()
        if any(tok in lower for tok in _MCP_TEST_TOKENS):
            return StepKind.TEST_RUN
        return StepKind.MCP_CALL
    return TOOL_KIND_MAP.get(name, StepKind.COMMAND)


def _tool_title(name: str, tool_input: dict[str, Any]) -> str:
    if name == "Bash":
        return f"$ {tool_input.get('command', '')[:120]}"
    if name in ("Grep", "Glob"):
        return f"{name.lower()} {tool_input.get('pattern', '')[:100]}"
    if name in ("Read", "Edit", "Write"):
        return f"{name} {tool_input.get('file_path', '')}"
    if name.startswith("mcp__"):
        return name.replace("mcp__", "").replace("__", " / ")
    return name


class Normalizer:
    """Per-thread stateful normalizer: allocates seq, pairs tool_use with tool_result."""

    def __init__(self, run_id: str, thread_id: str) -> None:
        self.run_id = run_id
        self.thread_id = thread_id
        self._seq = 0
        self._pending_tools: dict[str, dict[str, Any]] = {}

    def _next(self, kind: StepKind, title: str, detail: dict[str, Any], uuid: str | None) -> StepEvent:
        event = StepEvent(
            run_id=self.run_id,
            thread_id=self.thread_id,
            seq=self._seq,
            kind=kind,
            title=title,
            detail=detail,
            sdk_message_uuid=uuid,
            # W-B6: same emission-uid contract as the custom engine's emitter.
            event_uid=str(_uuid.uuid4()),
        )
        self._seq += 1
        return event

    def handle(self, msg: Any) -> tuple[list[StepEvent], list[TypingDelta]]:
        """Returns (complete events, transient deltas) for one SDK message."""
        events: list[StepEvent] = []
        deltas: list[TypingDelta] = []
        uuid = getattr(msg, "uuid", None)

        if isinstance(msg, AssistantMessage):
            for block in msg.content:
                if isinstance(block, ThinkingBlock):
                    thinking = redact(str(block.thinking))
                    deltas.append(self._delta(StepKind.THINKING, thinking))
                    events.append(self._next(
                        StepKind.THINKING, "thinking…",
                        {"text": thinking}, uuid,
                    ))
                elif isinstance(block, TextBlock):
                    text = redact(str(block.text))
                    deltas.append(self._delta(StepKind.MESSAGE, text))
                    events.append(self._next(
                        StepKind.MESSAGE, text.splitlines()[0][:120] if text else "message",
                        {"text": text}, uuid,
                    ))
                elif isinstance(block, ToolUseBlock):
                    self._pending_tools[block.id] = {
                        "name": block.name,
                        "input": redact_dict(block.input or {}),
                        "uuid": uuid,
                    }
                    deltas.append(self._delta(_tool_kind(block.name), _tool_title(block.name, block.input or {})))

        elif isinstance(msg, UserMessage):
            if isinstance(msg.content, str):
                # A plain-string user message (the common user-prompt shape)
                # must reach the transcript — `else []` silently dropped it.
                text = redact(msg.content)
                if text.strip():
                    events.append(self._next(
                        StepKind.MESSAGE, text.splitlines()[0][:120],
                        {"text": text}, uuid,
                    ))
                content = []
            else:
                content = msg.content
            for block in content:
                if isinstance(block, ToolResultBlock):
                    events.append(self._complete_tool(block, uuid))

        elif isinstance(msg, SystemMessage):
            # Per-turn housekeeping (init, thinking_tokens) fires on EVERY turn,
            # including injected nudge turns on the same live session — storing
            # it makes a healthy persistent conversation READ like a restart.
            # Only genuinely informative subtypes become transcript lines.
            if msg.subtype not in _NOISY_SYSTEM_SUBTYPES:
                events.append(self._next(
                    StepKind.STATUS, f"session {msg.subtype}",
                    {"subtype": msg.subtype, "data": msg.data}, uuid,
                ))

        elif isinstance(msg, ResultMessage):
            events.append(self._next(
                StepKind.STATUS, "turn complete",
                {
                    "num_turns": msg.num_turns,
                    "duration_ms": msg.duration_ms,
                    "is_error": msg.is_error,
                    "session_id": msg.session_id,
                    # Reported usage — gateway metering remains the cost source of
                    # truth; SDK USD is wrong for Kimi and is NOT stored.
                    "usage": msg.usage,
                },
                uuid,
            ))

        return events, deltas

    def _complete_tool(self, block: ToolResultBlock, uuid: str | None) -> StepEvent:
        pending = self._pending_tools.pop(block.tool_use_id, None)
        if pending is None:
            return self._next(
                StepKind.STATUS, "tool result (unpaired)",
                {"content": redact(_result_text(block)), "is_error": bool(block.is_error)}, uuid,
            )
        kind = _tool_kind(pending["name"])
        title = _tool_title(pending["name"], pending["input"])
        if kind == StepKind.TEST_RUN or (pending["name"] == "Bash" and _looks_like_test(pending["input"])):
            kind = StepKind.TEST_RUN
        return self._next(
            kind, title,
            {
                "tool": pending["name"],
                "input": pending["input"],
                "output": redact(_result_text(block)),
                "ok": not bool(block.is_error),
            },
            pending["uuid"] or uuid,
        )

    def _delta(self, kind: StepKind, text: str) -> TypingDelta:
        return TypingDelta(run_id=self.run_id, thread_id=self.thread_id, kind=kind, text=text)


def _result_text(block: ToolResultBlock) -> str:
    content = block.content
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(item.get("text", ""))
            else:
                parts.append(str(item))
        return "\n".join(parts)
    return "" if content is None else str(content)


def _looks_like_test(tool_input: dict[str, Any]) -> bool:
    cmd = str(tool_input.get("command", ""))
    return any(tok in cmd for tok in ("pytest", "npm test", "vitest", "jest", "pnpm test"))
