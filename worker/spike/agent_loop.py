"""Phase 0 spike — the hand-rolled ReAct agent loop over the LiteLLM gateway.

This is deliberately NOT the full LangGraph StateGraph (that's Phase 2). It
validates that the gateway can sustain a multi-turn tool-calling conversation
on open models. The interrupt+inject+resume check (g) uses LangGraph's
interrupt()/Command(resume=) in a separate graph (interrupt_graph.py) because
that's the mechanism Phase 3 approvals depend on.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_openai import ChatOpenAI

from spike.spike_tools import SPIKE_TOOLS, call_tool, looks_like_test

MAX_TURNS_DEFAULT = 80

SYSTEM_PROMPT = (
    "You are a code investigator. Use the file_read and bash tools to explore the "
    "workspace. Cite file:line for every claim. When the user asks for an edit, "
    "use file_edit. Be precise and thorough."
)


class AgentRecorder:
    """Records everything the agent does for matrix scoring."""

    def __init__(self, name: str, model: str) -> None:
        self.name = name
        self.model = model
        self.events: list[dict[str, Any]] = []
        self.tool_calls: list[dict[str, Any]] = []
        self.usage: dict[str, Any] = {}
        self.first_delta_at: float | None = None
        self.started = time.monotonic()
        self.turn_count = 0
        self.is_error = False
        self.nudge_incorporated = False

    def record_tool(self, name: str, args: dict[str, Any], result: dict[str, Any]) -> None:
        is_test = name == "bash" and looks_like_test(args.get("command", ""))
        self.tool_calls.append(
            {"tool": name, "args": args, "ok": result["ok"], "is_test": is_test}
        )
        self.events.append(
            {
                "kind": "tool",
                "tool": name,
                "args": args,
                "result": result,
                "ts": datetime.now(UTC).isoformat(),
            }
        )

    def record_delta(self) -> None:
        if self.first_delta_at is None:
            self.first_delta_at = time.monotonic()

    def record_turn(self, usage: dict[str, Any] | None, is_error: bool) -> None:
        self.turn_count += 1
        if usage:
            self.usage = usage
        if is_error:
            self.is_error = True

    def finish(self) -> dict[str, Any]:
        ok = sum(1 for t in self.tool_calls if t["ok"])
        total = len(self.tool_calls)
        return {
            "name": self.name,
            "model": self.model,
            "duration_s": round(time.monotonic() - self.started, 1),
            "first_delta_latency_s": (
                round(self.first_delta_at - self.started, 2) if self.first_delta_at else None
            ),
            "num_events": len(self.events),
            "tool_calls": total,
            "tool_calls_ok": ok,
            "tool_call_success_rate": (ok / total) if total else None,
            "turn_count": self.turn_count,
            "usage": self.usage or None,
            "is_error": self.is_error,
            "nudge_incorporated": self.nudge_incorporated,
        }


def make_llm(model: str, *, streaming: bool = True, structured: type | None = None) -> ChatOpenAI:
    """Build a ChatOpenAI pointed at the LiteLLM gateway (OpenAI-compatible)."""
    import os

    base_url = os.environ["LITELLM_BASE_URL"]
    api_key = os.environ["LITELLM_API_KEY"]
    kwargs: dict[str, Any] = {
        "model": model,
        "base_url": base_url,
        "api_key": api_key,
        "streaming": streaming,
        "timeout": 600,
        "max_retries": 2,
    }
    if structured is not None:
        kwargs["model_kwargs"] = {
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": structured.__name__,
                    "schema": structured.model_json_schema(),
                },
            }
        }
    return ChatOpenAI(**kwargs)


def _bind_tools(llm: ChatOpenAI) -> Any:
    return llm.bind_tools(SPIKE_TOOLS)


def _extract_usage(ai_message: AIMessage) -> dict[str, Any] | None:
    meta = getattr(ai_message, "response_metadata", {}) or {}
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


def _is_error(ai_message: AIMessage) -> bool:
    meta = getattr(ai_message, "response_metadata", {}) or {}
    return meta.get("finish_reason", "") == "error"


def _ai_text(ai_message: AIMessage) -> str:
    content = ai_message.content
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in content)
    return str(content)


def _canary_present(text: str, canary: str) -> bool:
    return bool(canary) and canary in text


def _canary_present_in_events(events: list[dict[str, Any]], canary: str) -> bool:
    if not canary:
        return False
    return canary in json.dumps(events, default=str)


async def run_agent_loop(
    llm: ChatOpenAI,
    prompt: str,
    cwd: str | None,
    recorder: AgentRecorder,
    *,
    max_turns: int = MAX_TURNS_DEFAULT,
    on_first_tool: Any = None,
    canary: str | None = None,
) -> AgentRecorder:
    """The core ReAct loop: assistant -> tools -> assistant, until no tool calls."""
    bound = _bind_tools(llm)
    messages: list[Any] = [SystemMessage(content=SYSTEM_PROMPT)]
    user_text = f"Workspace root: {cwd}\n\n{prompt}" if cwd else prompt
    messages.append(HumanMessage(content=user_text))

    nudge_injected = False

    for turn in range(max_turns):
        first_delta_this_turn = True
        ai_message: AIMessage | None = None
        try:
            async for chunk in bound.astream(messages, stream_mode="messages"):
                if hasattr(chunk, "content") and chunk.content and first_delta_this_turn:
                    recorder.record_delta()
                    first_delta_this_turn = False
                ai_message = chunk if not isinstance(chunk, tuple) else chunk[0]
        except Exception as exc:  # noqa: BLE001
            recorder.record_turn(None, is_error=True)
            recorder.events.append({"kind": "error", "error": str(exc)})
            print(f"[spike] stream error on turn {turn}: {exc}")
            break

        if ai_message is None:
            recorder.record_turn(None, is_error=True)
            break

        messages.append(ai_message)
        recorder.record_turn(_extract_usage(ai_message), _is_error(ai_message))

        tool_calls = getattr(ai_message, "tool_calls", None) or []
        if not tool_calls:
            if canary and nudge_injected:
                recorder.nudge_incorporated = _canary_present(_ai_text(ai_message), canary) or _canary_present_in_events(
                    recorder.events, canary
                )
            break

        for tc in tool_calls:
            name = tc["name"]
            args = tc.get("args", {}) or {}
            result = await call_tool(name, args)
            recorder.record_tool(name, args, result)
            messages.append(ToolMessage(content=str(result["output"]), tool_call_id=tc["id"], name=name))

            if on_first_tool is not None and not nudge_injected:
                nudge_injected = True
                await on_first_tool(llm, messages, recorder, canary)

    return recorder


__all__ = [
    "MAX_TURNS_DEFAULT",
    "SYSTEM_PROMPT",
    "AgentRecorder",
    "make_llm",
    "run_agent_loop",
]
