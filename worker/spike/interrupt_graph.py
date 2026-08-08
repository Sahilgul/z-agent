"""Phase 0 spike — check (g): interrupt + inject + resume via LangGraph.

This is the critical gate check: the entire Phase 3 approval architecture
stands on LangGraph's interrupt()/Command(resume=) surviving the gateway
translation on open models. We build a minimal LangGraph StateGraph that:

  1. Runs the agent until it makes its first tool call.
  2. interrupt()s with a HumanInterrupt asking for a steering nudge.
  3. Resumes with Command(resume=<nudge text>) containing a canary word.
  4. Verifies the nudge was visibly incorporated (canary present in the final
     answer) and that no state was lost (state_lost=false).

A g-failure triggers a LangGraph interrupt/resume investigation BEFORE Phase 2.
"""

from __future__ import annotations

import json
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.types import Command, interrupt

try:
    from langgraph.errors import GraphInterrupt
except ImportError:  # older langgraph
    GraphInterrupt = type(None)

from spike.agent_loop import SYSTEM_PROMPT, AgentRecorder, make_llm
from spike.checks import NUDGE_CANARY, NUDGE_TEXT, SOAK_PROMPT, stamp_workspace
from spike.spike_tools import SPIKE_TOOLS, call_tool

# State is a dict: {messages, nudge_injected, canary}
State = dict[str, Any]


async def _agent_node(state: State, config: dict[str, Any]) -> dict[str, Any]:
    """One assistant step. If a nudge is pending, interrupt for it first."""
    model = config["configurable"]["model"]
    recorder: AgentRecorder = config["configurable"]["recorder"]

    if state.get("nudge_pending") and not state.get("nudge_injected"):
        # The interrupt point — Phase 3 approvals stand on this surviving the gateway.
        nudge = interrupt(
            {
                "prompt": "Inject a steering nudge (a canary word will be appended automatically).",
                "canary": NUDGE_CANARY,
            }
        )
        full_nudge = f"{nudge}\n\n{NUDGE_TEXT}" if nudge else NUDGE_TEXT
        state["messages"].append(HumanMessage(content=full_nudge))
        recorder.events.append({"kind": "nudge_injected", "nudge": full_nudge})
        # nudge_injected MUST be in the node return value: with the replace
        # reducer, mutating state in place does not persist to the checkpoint,
        # so the flag was lost and the interrupt re-fired every agent step.

    llm = make_llm(model, streaming=True).bind_tools(SPIKE_TOOLS)
    messages = state["messages"]
    ai_message: AIMessage | None = None
    first_delta = True
    nudge_just_injected = bool(state.get("nudge_pending")) and not state.get("nudge_injected")
    try:
        async for chunk in llm.astream(messages, stream_mode="messages"):
            # stream_mode="messages" yields (message, metadata) TUPLES —
            # unwrap first; hasattr(tuple, "content") is always False.
            msg = chunk[0] if isinstance(chunk, tuple) else chunk
            if getattr(msg, "content", None) and first_delta:
                recorder.record_delta()
                first_delta = False
            # Chunks ACCUMULATE (same fix as agent_loop.py) — reassigning keeps
            # only the last delta, which has no content and no tool_calls, so
            # the tools node never ran and the interrupt was never exercised.
            ai_message = msg if ai_message is None else ai_message + msg
    except Exception as exc:
        recorder.record_turn(None, is_error=True)
        recorder.events.append({"kind": "error", "error": str(exc)})
        # Do NOT return {"messages": []} — the replace reducer would wipe the
        # whole conversation. End the turn with state intact.
        out: dict[str, Any] = {"done": True}
        if nudge_just_injected:
            out["nudge_injected"] = True
        return out

    if ai_message is None:
        out = {"done": True}
        if nudge_just_injected:
            out["nudge_injected"] = True
        return out

    messages.append(ai_message)
    from spike.agent_loop import _extract_usage, _is_error
    recorder.record_turn(_extract_usage(ai_message), _is_error(ai_message))

    tool_calls = getattr(ai_message, "tool_calls", None) or []
    out = {"messages": messages, "tool_calls": tool_calls}
    if nudge_just_injected:
        out["nudge_injected"] = True
    if not tool_calls:
        text = ai_message.content if isinstance(ai_message.content, str) else json.dumps(ai_message.content, default=str)
        recorder.nudge_incorporated = NUDGE_CANARY in text or NUDGE_CANARY in json.dumps(
            recorder.events, default=str
        )
        out["done"] = True
        return out

    out["done"] = False
    return out


async def _tools_node(state: State, config: dict[str, Any]) -> dict[str, Any]:
    """Execute pending tool calls and append ToolMessages."""
    recorder: AgentRecorder = config["configurable"]["recorder"]
    messages = state["messages"]
    for tc in state.get("tool_calls", []):
        name = tc["name"]
        args = tc.get("args", {}) or {}
        result = await call_tool(name, args)
        recorder.record_tool(name, args, result)
        messages.append(ToolMessage(content=str(result["output"]), tool_call_id=tc["id"], name=name))
    # After the first tool batch, arm the nudge interrupt for the next agent step.
    if not state.get("nudge_pending"):
        return {"messages": messages, "nudge_pending": True, "tool_calls": []}
    return {"messages": messages, "tool_calls": []}


def _should_continue(state: State) -> str:
    if state.get("done"):
        return "end"
    if state.get("tool_calls"):
        return "tools"
    return "agent"


async def run_interrupt_check(golden: Any, model: str, results_dir: Any) -> dict[str, Any]:
    """(g) nudge = interrupt + inject + resume. Verify no state loss + canary present."""
    from langgraph.graph import StateGraph

    ws = stamp_workspace(golden, "ServerApp", "main", results_dir / "workspaces" / f"{model}-interrupt")
    recorder = AgentRecorder("interrupt", model)

    graph_builder = StateGraph(dict)
    graph_builder.add_node("agent", _agent_node)
    graph_builder.add_node("tools", _tools_node)
    graph_builder.set_entry_point("agent")
    graph_builder.add_conditional_edges("agent", _should_continue, {"tools": "tools", "end": "__end__"})
    graph_builder.add_edge("tools", "agent")

    # Memory checkpointer — the spike validates the interrupt/resume mechanism,
    # not Postgres durability (that's Phase 2/4).
    from langgraph.checkpoint.memory import MemorySaver

    graph = graph_builder.compile(checkpointer=MemorySaver())

    initial_state: State = {
        "messages": [
            SystemMessage(content=SYSTEM_PROMPT),
            HumanMessage(content=f"Workspace root: {ws}\n\n{SOAK_PROMPT}"),
        ],
        "nudge_pending": False,
        "nudge_injected": False,
        "tool_calls": [],
        "done": False,
    }
    config = {
        "configurable": {"thread_id": f"spike-interrupt-{model}", "model": model, "cwd": str(ws), "recorder": recorder},
        "recursion_limit": 80,
    }

    # Run until it hits the interrupt.
    try:
        await graph.ainvoke(initial_state, config=config)
    except GraphInterrupt:
        # The interrupt surfaces as a GraphInterrupt in some versions; resume below.
        recorder.events.append({"kind": "interrupt_exception", "error": "GraphInterrupt"})
    except Exception as exc:
        # M-22: a REAL error (tool crash, graph bug) used to be swallowed here
        # as "interrupt_exception" and then the code RESUMED a graph that was
        # never interrupted — a no-op that masked the failure as a passing
        # interrupt-resume check. Record it distinctly and skip the resume.
        recorder.events.append({"kind": "invoke_error", "error": str(exc)})
        recorder.is_error = True
        summary = recorder.finish()
        summary["check"] = "interrupt"
        summary["nudge_incorporated"] = recorder.nudge_incorporated
        summary["state_lost"] = True
        summary["interrupt_resume_works"] = False
        summary["engine_error"] = str(exc)
        return summary

    # Resume with the nudge (the canary is appended inside _agent_node).
    try:
        await graph.ainvoke(Command(resume="Steering nudge incoming."), config=config)
    except Exception as exc:
        recorder.events.append({"kind": "resume_exception", "error": str(exc)})

    summary = recorder.finish()
    summary["check"] = "interrupt"
    summary["nudge_incorporated"] = recorder.nudge_incorporated
    summary["state_lost"] = recorder.is_error
    summary["interrupt_resume_works"] = recorder.nudge_incorporated and not recorder.is_error
    return summary


__all__ = ["run_interrupt_check"]
