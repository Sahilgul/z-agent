"""Engine core loop — the LangGraph StateGraph.

The assembled run path (this is what the Round-33 audit found missing — every
node below was a tested island; now they are MOUNTED):

  START -> [goal? goal_router] -> agent -> approval_gate? -> tools -> compaction -> agent ...

  agent          LLM + mode-aware tool binding + transient per-turn envelope
                 (env block + mode envelope + goal-stage envelope) +
                 usage/budget accounting + budget reminders.
  approval_gate  interrupt()-driven two-phase gate (Redis driver:
                 interrupt -> runner publishes card -> awaits -> Command(resume=)).
                 ONE interrupt per node execution; denials feed the 3-denial
                 circuit breaker (blocked-escalation).
  tools          Executes decided calls (denied calls become error ToolMessages,
                 never execute). Stuck-loop watchdog: nudge@3, decision-menu@5,
                 hand-off@8, force-stop@12. ask_user pauses the turn
                 (goal-mode clarify = INPUT_REQUIRED).
  compaction     prune -> summarize -> splice with the trigger and
                 the force path for context-overflow retries; emits the
                 compaction-card event.
  goal_router    The goal-mode stage subgraph: intake -> clarify? ->
                 explore -> plan -> implement -> verify -> rebase-gate -> PR,
                 with the critic loop (plan/verify exits) and blocked-escalation.

The graph is compiled WITH a checkpointer (Postgres default via
checkpointer.open_checkpointer; MemorySaver in tests). interrupt/resume is the
approval + clarify transport, so it must survive container replacement.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph import START, StateGraph
from langgraph.types import interrupt
from zagent_contracts import StepEvent, StepKind, TypingDelta

from worker.engine.compaction import Compactor
from worker.engine.events import EventEmitter
from worker.engine.goal_mode import (
    ADVANCE_ON_TURN_END,
    STAGE_ENVELOPES,
    GoalStage,
    advance_artifact,
    block_artifact,
    build_clarify_card,
    clear_pending_questions,
    get_pending_questions,
    make_goal,
    needs_clarification,
    stage_of,
)
from worker.engine.llm import estimate_cost, make_llm, with_gateway_retry, with_gateway_retry_aiter
from worker.engine.metrics import get_registry
from worker.engine.permissions import Effect
from worker.engine.permissions import evaluate as perms_evaluate
from worker.engine.state import Budget, EngineState, Mode, tag_message
from worker.engine.tools import call_tool_direct, needs_approval, tools_for_mode
from worker.engine.watchdogs import CriticRubric

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"

log = logging.getLogger(__name__)

# Stuck-loop watchdog thresholds.
_STREAK_NUDGE = 3
_STREAK_MENU = 5
_STREAK_HANDOFF = 8
_STREAK_FORCE_STOP = 12

_MAX_CRITIC_ITERATIONS = 3
_MAX_COMPACTION_RETRIES = 1


def _read_prompt(name: str) -> str:
    path = PROMPTS_DIR / name
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def _build_system_message(model: str, mode: Mode) -> SystemMessage:
    """base.md + model suffix — ONE system message per turn."""
    base = _read_prompt("base.md")
    marker = "--- PROMPT START ---"
    if marker in base:
        base = base.split(marker, 1)[1]
    end = "--- PROMPT END ---"
    if end in base:
        base = base.split(end, 1)[0]
    suffix_path = _suffix_path(model)
    suffix = Path(suffix_path).read_text(encoding="utf-8") if suffix_path and Path(suffix_path).exists() else ""
    base = base.replace("{{SKILLS_LISTING}}", "")
    msg = SystemMessage(content=base + suffix)
    return tag_message(msg, "system")  # type: ignore[arg-type]


def _suffix_path(model: str) -> str:
    from worker.engine.llm import suffix_for
    return suffix_for(model)


def _mode_of(state: EngineState) -> str:
    mode = state.get("mode", Mode.ASK)
    return mode.value if hasattr(mode, "value") else str(mode)


def _autonomy_of(state: EngineState) -> str:
    autonomy = state.get("autonomy", "supervised")
    return autonomy.value if hasattr(autonomy, "value") else str(autonomy)


def _build_turn_envelope(state: EngineState, config: RunnableConfig) -> HumanMessage | None:
    """The transient per-turn envelope (env block + mode
    envelope + goal-stage envelope). Rides as a synthetic user-role message,
    NEVER persisted, NEVER part of the system message."""
    mode = _mode_of(state)
    parts: list[str] = []

    # Environment block (the source of truth for where/when the agent is)
    workspace = config["configurable"].get("workspace", "")
    budget = state.get("budget")
    budget_line = ""
    if isinstance(budget, Budget):
        budget_line = f"budget: ${budget.used:.4f} used of ${budget.cap:.2f} cap\n"
    elif isinstance(budget, dict):
        budget_line = f"budget: ${budget.get('used', 0):.4f} used of ${budget.get('cap', 0):.2f} cap\n"
    env = (
        "<env>\n"
        f"thread: {state.get('thread_id', '?')}\n"
        f"workspace: {workspace}\n"
        f"mode: {mode}\n"
        f"autonomy: {_autonomy_of(state)}\n"
        f"{budget_line}"
        f"turn: {state.get('turn_count', 0) + 1}\n"
        "</env>"
    )
    parts.append(env)

    # Mode envelope (advertises the mode's tool filter — fixes the
    # Round-33 prompt/tool drift class)
    envelope_text = _read_prompt(f"envelopes/{mode}.md").strip()
    if envelope_text:
        parts.append(f"<mode-envelope mode=\"{mode}\">\n{envelope_text}\n</mode-envelope>")

    # Deferred-tool roster fragment: names + one-liners, <=0.5K
    # tokens; bound and discovered tools are excluded.
    from worker.engine.tools import default_tool_names
    from worker.engine.tools.discovery import roster_fragment
    bound = default_tool_names(mode) + list(state.get("discovered_tools", []) or [])
    roster = roster_fragment(mode, bound=bound).strip()
    if roster:
        parts.append(roster)

    # Goal-stage envelope (the stage subgraph's per-turn fragment)
    # L-02: `Mode.GOAL.value` is already the string "goal" (Mode is a str
    # enum), so the `or mode == "goal"` was redundant. Compare to the enum
    # member directly — it matches whether mode is Mode.GOAL or "goal".
    if mode == Mode.GOAL:
        stage_raw = state.get("stage_envelope")
        if stage_raw:
            try:
                stage_text = STAGE_ENVELOPES[GoalStage(stage_raw)]
                parts.append(stage_text)
            except ValueError:
                pass

    if not parts:
        return None
    msg = HumanMessage(content="\n\n".join(parts))
    return tag_message(msg, "envelope")  # type: ignore[arg-type]


def _bound_tools(state: EngineState, mode: str) -> list[Any]:
    """Two-tier binding: DEFAULT_TOOLS(mode) + discovered — NEVER the full
    registry. Mode-gates are re-checked at bind time: a discovered tool that
    became mode-denied drops out. MCP tools bind from the manager's live
    catalog (only after discovery)."""
    from worker.engine.tools import (
        ALL_BUILT_TOOL_BY_NAME,
        mode_allowed,
        resolve_tool_name,
    )
    bound = tools_for_mode(mode)
    bound_names = {t.name for t in bound}
    for name in state.get("discovered_tools", []) or []:
        if not mode_allowed(name, mode):
            continue  # mode-denied discovered tools drop out at bind time
        if name.startswith("mcp__"):
            from worker.engine.mcp import mcp_manager
            mgr = mcp_manager()
            server = name.split("__", 2)[1] if len(name.split("__", 2)) == 3 else ""
            tool_obj = (mgr.status.get(server) or None) and mgr.status[server].tools.get(name)
            if tool_obj is not None and name not in bound_names:
                bound.append(tool_obj)
                bound_names.add(name)
            continue
        tool_obj = ALL_BUILT_TOOL_BY_NAME.get(resolve_tool_name(name))
        if tool_obj is not None and tool_obj.name not in bound_names:
            bound.append(tool_obj)
            bound_names.add(tool_obj.name)
    return bound


# --- The agent node ---

async def agent_node(state: EngineState, config: RunnableConfig) -> dict[str, Any]:
    """Call the LLM with mode-bound tools; stream deltas; collect the AIMessage;
    account usage into the budget; fire budget reminders."""
    model = config["configurable"]["model"]
    task_id = state.get("task_id")
    emitter: EventEmitter = config["configurable"]["emitter"]
    delta_sink = config["configurable"].get("delta_sink")

    mode = _mode_of(state)
    llm = make_llm(model, streaming=True, tools=_bound_tools(state, mode))
    system = _build_system_message(model, mode)  # type: ignore[arg-type]
    messages = [system] + state.get("messages", [])
    envelope = _build_turn_envelope(state, config)
    if envelope is not None:
        messages = messages + [envelope]

    ai_message: AIMessage | None = None
    metrics = get_registry(config)
    llm_started = time.monotonic()
    try:
        # Gateway retry wraps the stream START (construction + first chunk);
        # once the first chunk arrives, partial deltas may have been emitted,
        # so mid-stream failures are NOT retried (the turn fails) (H-11).
        async for chunk in with_gateway_retry_aiter(
            lambda: _aiter(llm, messages), max_retries=2,
        ):
            content = getattr(chunk, "content", None)
            if content and delta_sink:
                await delta_sink(_delta(state, content))
            if chunk is not None:
                # Chunks ACCUMULATE — keeping only the last loses everything
                # (real SSE streams end with a usage-only chunk). The scripted
                # LLM in tests yields one chunk per call, which masked this.
                ai_message = chunk if ai_message is None else ai_message + chunk
    except Exception as exc:  # noqa: BLE001
        err = str(exc)
        # Context-length overflow -> force one compaction and retry the turn
        # (force-compaction path; the retry cap stops an infinite loop).
        if _looks_like_context_overflow(err) and state.get("compaction_retries", 0) < _MAX_COMPACTION_RETRIES:
            tuning = config["configurable"].get("tuning")
            if tuning is not None:
                tuning.observe_error(err)
            return {"needs_compaction": True, "force_compact": True}
        return {"error": err, "done": True}

    if ai_message is None:
        return {"error": "no response from LLM", "done": True}

    if metrics:
        metrics.observe("llm_call_latency_s", time.monotonic() - llm_started)
        metrics.increment("turns")

    events = emitter.from_assistant(ai_message, task_id)
    await _publish_events(config, events)

    # Budget accounting: usage -> cost -> budget.used; the gateway
    # remains the hard cap, this drives the 50%/80% reminders.
    usage = getattr(ai_message, "usage_metadata", None)
    out: dict[str, Any] = {
        "messages": state.get("messages", []) + [ai_message],
        "turn_count": state.get("turn_count", 0) + 1,
        "compaction_retries": 0,  # a healthy turn resets the overflow-retry loop guard
        "last_usage": dict(usage) if usage else None,
        # A permission ruleset can DENY or ASK on a READONLY tool (hard git
        # policies). _should_continue can't see config, so the agent node
        # flags whether one is active; the gate then evaluates each call
        # (allow/deny/ask) and enforces it (C-05).
        "permissions_active": bool(config["configurable"].get("permissions")),
    }
    budget = state.get("budget")
    if usage and budget is not None:
        used = budget.used if isinstance(budget, Budget) else float(budget.get("used", 0))
        cap = budget.cap if isinstance(budget, Budget) else float(budget.get("cap", 5.0))
        cost = estimate_cost(model, dict(usage))
        new_used = used + cost
        # Plain dict, not the Budget model — the Postgres serde is msgpack and
        # unregistered pydantic types warn now and hard-fail in a future
        # langgraph (LANGGRAPH_STRICT_MSGPACK). Readers accept both shapes.
        out["budget"] = {"used": new_used, "cap": cap}
        reminder = _budget_reminder(emitter, task_id, used, new_used, cap)
        if reminder is not None:
            await _publish_events(config, [reminder])
    tuning = config["configurable"].get("tuning")
    if tuning is not None:
        tuning.observe_healthy_turn()
    return out


def _looks_like_context_overflow(err: str) -> bool:
    low = err.lower()
    return ("context" in low and "length" in low) or "maximum context" in low or "too many tokens" in low


def _budget_reminder(emitter: EventEmitter, task_id: str | None,
                     prev_used: float, new_used: float, cap: float) -> StepEvent | None:
    """50%/80% budget reminders. Never auto-stops."""
    if cap <= 0:
        return None
    for threshold in (0.80, 0.50):
        if prev_used / cap < threshold <= new_used / cap:
            return emitter._next(
                StepKind.STATUS,
                f"⚠ budget {int(threshold * 100)}% used",
                {
                    "kind": "warning",
                    "warning": "budget",
                    "level": f"{int(threshold * 100)}",
                    "pct": new_used / cap,
                    "detail": f"budget {int(threshold * 100)}% consumed (${new_used:.4f} of ${cap:.2f})",
                },
                task_id, None,
            )
    return None


def _aiter(llm: Any, messages: list) -> Any:
    """Return the async iterator over the LLM stream (retryable wrapper).

    MUST be sync: it returns the async iterator itself. (An async def here
    returns a coroutine instead, which `async for` cannot consume — a latent
    crash the Round-33 audit's "nothing ran live" finding predicted.)
    """
    return llm.astream(messages)


def _delta(state: EngineState, content: Any) -> TypingDelta:
    text = content if isinstance(content, str) else str(content)
    return TypingDelta(
        run_id=state["run_id"], thread_id=state["thread_id"],
        context_id=state.get("context_id", state["thread_id"]),
        kind=StepKind.MESSAGE, text=text,
    )


# --- The approval gate node (interrupt-driven, Redis driver) ---

async def approval_gate_node(state: EngineState, config: RunnableConfig) -> dict[str, Any]:
    """Two-phase gate via LangGraph interrupt().

    ONE interrupt per node execution (the self-loop re-enters per undecided
    call — multi-interrupt replay semantics stay out of the picture). The
    interrupt payload IS the approval card; the runner publishes it, awaits the
    human on Redis, and resumes with Command(resume=decision). Because the
    decision crosses the checkpoint boundary, approvals survive container
    replacement. Denials feed the 3-denial circuit breaker.
    """
    autonomy = _autonomy_of(state)
    broker = config["configurable"].get("approval_broker")
    ruleset = config["configurable"].get("permissions")
    metrics = get_registry(config)
    messages = state.get("messages", [])
    last = messages[-1] if messages else None
    tool_calls = getattr(last, "tool_calls", None) or []

    approved = dict(state.get("approved_calls", {}))
    denial_streak = state.get("denial_streak", 0)

    for tc in tool_calls:
        tc_id = tc.get("id", "")
        name = tc.get("name", "")
        args = tc.get("args", {}) or {}
        if tc_id in approved:
            continue
        # Glob rulesets (findLast) — a deny rule short-circuits BEFORE the
        # card (hard git policies never reach a human); allow skips it.
        effect = perms_evaluate(name, args, ruleset)
        if effect is Effect.DENY:
            approved[tc_id] = {"approved": False,
                               "reason": "denied by permission rule (hard policy)"}
            denial_streak += 1
            if metrics:
                metrics.increment("permission_rule_denies")
            out: dict[str, Any] = {"approved_calls": approved, "denial_streak": denial_streak}
            if denial_streak >= 3:
                out["done"] = True
                out["blocked_reason"] = "3 consecutive denials — blocked-escalation"
            return out
        if effect is Effect.ALLOW:
            approved[tc_id] = {"approved": True, "via": "ruleset_allow", "args": args}
            # Any allow breaks the consecutive-denial run (H-02): the old code
            # only reset on a HUMAN allow, so a ruleset-ALLOW between two denials
            # left the streak intact and the 3-denial breaker misfired.
            denial_streak = 0
            continue
        # knowledge_draft scope=user is auto-approved.
        if name == "knowledge_draft" and args.get("scope") == "user":
            approved[tc_id] = {"approved": True, "via": "scope_user_auto", "args": args}
            denial_streak = 0  # H-02: auto-allow breaks the denial streak
            continue
        if not needs_approval(name, autonomy):
            approved[tc_id] = {"approved": True, "via": "not_required"}
            denial_streak = 0  # H-02: auto-allow breaks the denial streak
            continue
        if broker is not None and await broker.is_always_allowed(name, args):
            approved[tc_id] = {"approved": True, "via": "always_allow"}
            denial_streak = 0  # H-02: auto-allow breaks the denial streak
            continue
        payload = (
            broker.card_payload(name, args, tc_id) if broker is not None
            else {"type": "approval_request", "tool": name, "args": args, "tool_call_id": tc_id}
        )
        if metrics:
            metrics.increment("approvals_requested")
        decision = interrupt(payload)
        verdict = decision.get("decision", "deny")
        if verdict in ("allow", "allow_once", "always_allow", "edited_allow"):
            # Edit-and-resend: the human may EDIT the verbatim args before
            # approving — the edited args are what executes (still verbatim).
            exec_args = decision.get("edited_args", args)
            approved[tc_id] = {"approved": True, "via": verdict, "args": exec_args}
            denial_streak = 0
            if verdict == "always_allow" and broker is not None:
                await broker.persist_always_allow(name)
        else:
            approved[tc_id] = {"approved": False, "reason": decision.get("reason", "denied by user")}
            denial_streak += 1
            if metrics:
                metrics.increment("approvals_denied")
        # ONE interrupt per execution — return now; the self-loop re-enters
        # for any remaining undecided calls.
        out: dict[str, Any] = {"approved_calls": approved, "denial_streak": denial_streak}
        if denial_streak >= 3:
            out["done"] = True
            out["blocked_reason"] = "3 consecutive denials — blocked-escalation"
        return out

    return {"approved_calls": approved, "denial_streak": denial_streak}


def _after_gate(state: EngineState) -> str:
    """Self-loop while undecided approval-needing calls remain; blocked on the
    3-denial breaker; otherwise proceed to execution."""
    if state.get("done"):
        return "end"
    autonomy = _autonomy_of(state)
    messages = state.get("messages", [])
    last = messages[-1] if messages else None
    tool_calls = getattr(last, "tool_calls", None) or []
    approved = state.get("approved_calls", {})
    for tc in tool_calls:
        tc_id = tc.get("id", "")
        name = tc.get("name", "")
        if tc_id not in approved and needs_approval(name, autonomy):
            return "gate"
    return "tools"


# --- The tools node ---

async def tools_node(state: EngineState, config: RunnableConfig) -> dict[str, Any]:
    """Execute decided tool calls; append ToolMessages; run the stuck-loop
    watchdog. Denied calls become error ToolMessages and NEVER execute."""
    emitter: EventEmitter = config["configurable"]["emitter"]
    task_id = state.get("task_id")
    messages = state.get("messages", [])
    if not messages:
        return {"messages": messages}
    last = messages[-1]
    tool_calls = getattr(last, "tool_calls", None) or []
    if not tool_calls:
        return {"messages": messages}

    approved = state.get("approved_calls", {})
    streaks = dict(state.get("tool_streak", {}))
    new_messages = list(messages)
    out: dict[str, Any] = {}
    metrics = get_registry(config)
    # L-01: track how many tool calls were denied at the gate so we only
    # drain background-terminal notifications when at least one tool
    # actually executed. On a denied-only turn the agent did nothing, so
    # notifying about background terminals is noise and would consume
    # notifications that should wait for a real turn.
    denied_count = 0

    for tc in tool_calls:
        tc_id = tc.get("id", "")
        name = tc.get("name", "")
        args = tc.get("args", {}) or {}

        decision = approved.get(tc_id)
        # M-01: the gate may edit-and-resend args (decision["args"]); staging
        # ops (mode_request, knowledge_draft) used to read the ORIGINAL args,
        # silently dropping the human's edits. Use the gate-edited args when
        # present, matching the general-tool execution path below.
        effective_args = (decision or {}).get("args", args)
        if decision is not None and not decision.get("approved", True):
            # Denied at the gate — the agent sees the reason, nothing executes.
            denied_count += 1
            result = {"kind": "error", "ok": False,
                      "output": f"error: denied — {decision.get('reason', 'denied by user')}"}
        elif name == "update_tasks":
            # Two-artifact model: the reducer owns state; every mutation is
            # a durable event (recovery reconstructs from the event log).
            from worker.engine.tools.extended import apply_task_updates
            new_tasks, err = apply_task_updates(state.get("tasks"), args.get("updates", []))
            if err:
                result = {"kind": "error", "ok": False, "output": err}
            else:
                out["tasks"] = new_tasks
                result = {"kind": "success", "ok": True,
                          "output": f"tasks updated: {len(new_tasks['artifact'])} planned, "
                                    f"{sum(1 for s in new_tasks['tracker'].values() if s == 'completed')} completed",
                          "detail_tasks": new_tasks}
        elif name == "compact":
            # Agent-triggerable compaction: force the next compaction
            # point regardless of the token threshold.
            out["force_compact"] = True
            result = {"kind": "success", "ok": True,
                      "output": "ok: compaction requested — the engine compacts at the next boundary"}
        elif name == "tool_search":
            # Discovery: matches merge into state.discovered_tools (the
            # next LLM call binds them natively — checkpointed, session-scoped).
            from worker.engine.tools import default_tool_names
            from worker.engine.tools.discovery import tool_search_async
            result = await tool_search_async(
                (decision or {}).get("args", args),
                mode=_mode_of(state),
                bound=default_tool_names(_mode_of(state))
                + list(state.get("discovered_tools", []) or []))
            if result["ok"] and result.get("discovered"):
                merged = sorted(set(state.get("discovered_tools", []) or [])
                                | set(result["discovered"]))
                out["discovered_tools"] = merged
        elif name == "mode_request":
            # Approval-routed mode transition — reaching here means the
            # gate approved (MUTATING capability); apply the mode change.
            target = str(effective_args.get("target_mode", ""))
            valid = {m.value for m in Mode}
            if target not in valid:
                result = {"kind": "error", "ok": False,
                          "output": f"error: unknown mode {target!r} (valid: {sorted(valid)})"}
            else:
                out["mode"] = Mode(target)
                result = {"kind": "success", "ok": True,
                          "output": f"ok: mode transition approved — now in {target} mode"}
        else:
            # Execute with the VERBATIM args recorded at the gate
            # (edit-and-resend args land here via decision["args"]).
            exec_args = (decision or {}).get("args", args)
            started = time.monotonic()
            result = await call_tool_direct(name, exec_args)
            if metrics:
                metrics.observe("tool_call_latency_s", time.monotonic() - started)
                metrics.increment("tool_calls_total")
                if not result["ok"]:
                    metrics.increment("tool_calls_failed")

        # knowledge_draft: stage the draft (scope=user lands directly;
        # repo|global staged for the human-gated approve path).
        if name == "knowledge_draft" and result["ok"]:
            drafts = list(state.get("knowledge_drafts", []))
            drafts.append({
                "scope": effective_args.get("scope"), "title": effective_args.get("title"),
                "content": effective_args.get("content"), "provenance": effective_args.get("provenance", ""),
                "status": "auto_approved" if effective_args.get("scope") == "user" else "pending_approval",
            })
            out["knowledge_drafts"] = drafts

        # ask_user: snapshot the process-global pending questions into per-run
        # state (H-10). The module global is only a transport from the
        # ask_user tool (run in an executor thread) to here; snapshotting it
        # into state makes the clarify signal per-run + checkpointed instead
        # of a process-global shared across concurrent runs (coord point A).
        # The global is cleared immediately so a later run can't read it.
        if name == "ask_user" and result["ok"]:
            out["pending_questions"] = get_pending_questions()
            clear_pending_questions()

        detail: dict[str, Any] = {"tool": name, "input": args, "output": result["output"],
                                  "ok": result["ok"]}
        if result.get("detail_tasks") is not None:
            detail["tasks"] = result["detail_tasks"]
            detail["kind"] = "todo-checklist"
        event = emitter._next(
            _tool_kind(name), _tool_title(name, args), detail, task_id, None,
        )
        await _publish_events(config, [event])
        # M-05: from_assistant() stages every tool_use in emitter._pending_tools
        # but from_tool_result() (which pops) is never called in production —
        # the tools_node emits the result event directly. Pop the staged entry
        # here so the dict can't grow unbounded across a long run.
        emitter._pending_tools.pop(tc_id, None)
        new_messages.append(tag_message(ToolMessage(
            content=result["output"], tool_call_id=tc_id, name=name,
        ), "tool"))  # type: ignore[arg-type]

        # ask_user pauses the pipeline (goal-mode clarify = INPUT_REQUIRED).
        if name == "ask_user" and result["ok"]:
            out["done"] = True

        # Stuck-loop watchdog
        sig = _call_signature(name, args)
        if result["ok"]:
            streaks.pop(sig, None)
        else:
            count = streaks.get(sig, 0) + 1
            streaks[sig] = count
            watchdog = _streak_response(emitter, task_id, count)
            if watchdog is not None:
                nudge_text, warn_event, force_stop = watchdog
                new_messages.append(tag_message(HumanMessage(content=nudge_text), "nudge"))  # type: ignore[arg-type]
                await _publish_events(config, [warn_event])
                if force_stop:
                    out["done"] = True
                    out["blocked_reason"] = f"stuck loop: same failing call {count}x — force-stop"
                    out["error"] = out["blocked_reason"]
                if metrics:
                    metrics.increment("stuck_loop_triggers")

    # Background terminal completion/watch notifies at turn end.
    # L-01: only drain when at least one tool actually executed
    # (denied_count < len(tool_calls)); a denied-only turn did nothing,
    # so notifying about background terminals is noise and would consume
    # notifications that should wait for a real turn.
    if denied_count < len(tool_calls):
        from worker.engine.tools.background import terminal_manager
        for note in terminal_manager().completed_notifications():
            new_messages.append(tag_message(HumanMessage(
                content=f"[background terminal] {note}"), "nudge"))  # type: ignore[arg-type]

    out["messages"] = new_messages
    out["tool_streak"] = streaks
    return out


def _call_signature(name: str, args: dict[str, Any]) -> str:
    blob = json.dumps(args, sort_keys=True, default=str)
    return f"{name}:{hashlib.sha256(blob.encode()).hexdigest()[:12]}"


def _streak_response(emitter: EventEmitter, task_id: str | None,
                     count: int) -> tuple[str, StepEvent, bool] | None:
    """nudge@3 -> decision-menu@5 -> hand-off@8 -> force-stop@12."""
    if count == _STREAK_NUDGE:
        text = ("The same failing call has now failed 3 times in a row. Stop "
                "retrying it unchanged: re-read the error, change the approach "
                "(different arguments, a different tool, or a smaller step).")
        return text, _warning_event(emitter, task_id, "stuck_loop", f"same failing call {count}x"), False
    if count == _STREAK_MENU:
        text = ("This call has failed 5 times. Decide explicitly: (a) try a "
                "fundamentally different approach, (b) narrow the task and do "
                "the part that works, or (c) stop and report the blocker to "
                "the human. Retrying unchanged is not an option.")
        return text, _warning_event(emitter, task_id, "stuck_loop", f"decision menu at {count}x"), False
    if count == _STREAK_HANDOFF:
        text = ("8 consecutive failures on the same call. Hand this off: "
                "summarize what you tried, why it failed, and what you believe "
                "the blocker is. A human will take it from there.")
        return text, _warning_event(emitter, task_id, "stuck_loop", f"hand-off at {count}x"), False
    if count >= _STREAK_FORCE_STOP:
        text = ("Force-stop: the same call failed 12 times. This turn is "
                "terminated; the blocker is escalated to the team.")
        return text, _warning_event(emitter, task_id, "stuck_loop", f"force-stop at {count}x"), True
    return None


def _warning_event(emitter: EventEmitter, task_id: str | None, warning: str, detail: str) -> StepEvent:
    return emitter._next(
        StepKind.STATUS, f"⚠ {detail}",
        {"kind": "warning", "warning": warning, "detail": detail},
        task_id, None,
    )


def _tool_kind(name: str) -> StepKind:
    if name.startswith("mcp__"):
        return StepKind.MCP_CALL
    return {
        "file_read": StepKind.FILE_READ,
        "file_edit": StepKind.FILE_EDIT,
        "file_write": StepKind.FILE_EDIT,
        "file_search": StepKind.COMMAND,
        "file_glob": StepKind.COMMAND,
        "terminal_exec": StepKind.COMMAND,
    }.get(name, StepKind.COMMAND)


def _tool_title(name: str, args: dict[str, Any]) -> str:
    if name == "terminal_exec":
        return f"$ {str(args.get('command', ''))[:120]}"
    if name in ("file_read", "file_search", "file_glob", "file_edit", "file_write"):
        return f"{name} {args.get('file_path') or args.get('pattern') or ''}"
    if name == "memory_search":
        return f"memory.search {args.get('query', '')[:80]}"
    if name == "ask_user":
        return "asking the human"
    if name in ("spawn_agent", "spawn_swarm"):
        return f"{name}: {str(args.get('prompt') or args.get('rationale') or '')[:80]}"
    if name.startswith("mcp__"):
        return name.replace("mcp__", "").replace("__", " / ")
    return name


# --- The compaction node ---

async def compaction_node(state: EngineState, config: RunnableConfig) -> dict[str, Any]:
    """prune -> summarize -> splice + the force path for context
    overflow. Emits the compaction-card event; honesty-validator rollbacks
    surface as warnings instead of silent drops."""
    compactor: Compactor = config["configurable"].get("compactor") or Compactor()
    emitter: EventEmitter = config["configurable"]["emitter"]
    # Self-tuning limit: the learned limit drives the trigger.
    tuning = config["configurable"].get("tuning")
    if tuning is not None:
        compactor.policy.context_limit = tuning.current
    task_id = state.get("task_id")
    messages = state.get("messages", [])
    force = state.get("force_compact", False)

    try:
        new_messages, result = await compactor.compact(messages, force=force)
    except Exception as exc:
        # M-03: a summarizer/LLM failure used to propagate and kill the whole
        # turn. Log it, emit a warning card, and skip compaction this cycle
        # so the turn survives with the original messages intact.
        log.warning("compaction failed — skipping this cycle", error=str(exc))
        await _publish_events(config, [emitter._next(
            StepKind.STATUS, "⚠ compaction failed",
            {"kind": "warning", "warning": "compaction_failed",
             "detail": str(exc)[:500]},
            task_id, None,
        )])
        return {"needs_compaction": False, "force_compact": False}
    out: dict[str, Any] = {
        "needs_compaction": False,
        "force_compact": False,
    }
    if force:
        out["compaction_retries"] = state.get("compaction_retries", 0) + 1

    if result.pruned_count > 0 or result.rolled_back or force:
        out["messages"] = new_messages
        out["last_compaction_at"] = time.time()
        out["compaction_count"] = state.get("compaction_count", 0) + 1
        metrics = get_registry(config)
        if metrics:
            metrics.increment("compactions")
            metrics.observe("checkpoint_size_messages", len(new_messages))
        if result.rolled_back:
            event = emitter._next(
                StepKind.STATUS, "⚠ compaction rolled back",
                {"kind": "warning", "warning": "compaction_rollback",
                 "detail": result.rollback_reason},
                task_id, None,
            )
        else:
            event = emitter._next(
                StepKind.STATUS, "compaction",
                {
                    "kind": "compaction_card",
                    "pruned": result.pruned_count,
                    "summarized": result.summarized_count,
                    "kept": result.kept_count,
                    "before_tokens": result.before_tokens,
                    "after_tokens": result.after_tokens,
                    "summary": result.summary[:500],
                    "forced": force,
                },
                task_id, None,
            )
        await _publish_events(config, [event])
    return out


# --- The goal router node (the stage subgraph) ---

async def goal_router_node(state: EngineState, config: RunnableConfig) -> dict[str, Any]:
    """The goal-mode stage machine. Runs on goal-mode entry (intake) and after
    every completed agent turn (stage advance). The critic loop wraps the
    plan/verify exits; persistent failure routes to blocked-escalation — the
    only human gate inside goal mode besides clarify."""
    emitter: EventEmitter = config["configurable"]["emitter"]
    task_id = state.get("task_id")
    goal = state.get("goal_artifact")

    # INTAKE — first entry: build the artifact from the user story.
    if goal is None:
        story = _first_user_story(state)
        artifact = make_goal(story).artifact.model_dump(mode="json")
        if needs_clarification(story):
            artifact["stage"] = GoalStage.CLARIFY.value
            artifact["clarify_questions"] = build_clarify_card(story)
        else:
            artifact["stage"] = GoalStage.EXPLORE.value
        await _publish_events(config, [emitter._next(
            StepKind.STATUS, f"goal intake → {artifact['stage']}",
            {"kind": "goal_stage", "stage": artifact["stage"], "goal_id": artifact["goal_id"]},
            task_id, None,
        )])
        await _publish_stage_event_recap(config, emitter, task_id, artifact)
        return {"goal_artifact": artifact, "stage_envelope": artifact["stage"]}

    stage = stage_of(goal)

    # Terminal-ish stages
    if stage in (GoalStage.DONE, GoalStage.BLOCKED):
        return {"done": True}
    if stage == GoalStage.PR:
        # The PR-summary turn just completed — the platform opens the PR from
        # the summary. Goal complete.
        await _publish_events(config, [emitter._next(
            StepKind.STATUS, "goal complete — PR stage reached",
            {"kind": "goal_stage", "stage": GoalStage.DONE.value, "goal_id": goal.get("goal_id")},
            task_id, None,
        )])
        return {"goal_artifact": advance_artifact(goal, GoalStage.DONE), "done": True}

    # CLARIFY — paused until the human's answers arrive (the runner appends
    # them as a USER message and starts a new turn).
    if stage == GoalStage.CLARIFY:
        if _clarify_answered(state):
            nxt = advance_artifact(goal, GoalStage.EXPLORE)
            await _publish_stage_event(config, emitter, task_id, nxt)
            # H-10: clear the per-run clarify signal in state (was the
            # process-global clear_pending_questions()).
            return {"goal_artifact": nxt, "stage_envelope": nxt["stage"],
                    "pending_questions": None}
        # Not answered yet — end the turn (INPUT_REQUIRED). If the agent never
        # called ask_user, the clarify envelope pushes it to.
        return {"done": True}

    # Turn-end advances.
    if stage in ADVANCE_ON_TURN_END:
        nxt_stage = ADVANCE_ON_TURN_END[stage]
        if stage == GoalStage.EXPLORE:
            nxt = advance_artifact(goal, nxt_stage)
            await _publish_stage_event(config, emitter, task_id, nxt)
            return {"goal_artifact": nxt, "stage_envelope": nxt["stage"], "critic_iterations": 0}
        if stage == GoalStage.IMPLEMENT:
            nxt = advance_artifact(goal, nxt_stage)
            await _publish_stage_event(config, emitter, task_id, nxt)
            return {"goal_artifact": nxt, "stage_envelope": nxt["stage"]}
        if stage == GoalStage.REBASE_GATE:
            nxt = advance_artifact(goal, nxt_stage)
            await _publish_stage_event(config, emitter, task_id, nxt)
            return {"goal_artifact": nxt, "stage_envelope": nxt["stage"]}

    # PLAN exit — critic pass: a plan with no tracked steps blocks (merged
    # rubric, completeness dimension). Blocking findings reference the plan.
    if stage == GoalStage.PLAN:
        tracker = state.get("task_tracker") or goal.get("task_tracker")
        if tracker:
            nxt = advance_artifact(goal, GoalStage.IMPLEMENT)
            nxt["task_tracker"] = tracker
            await _publish_stage_event(config, emitter, task_id, nxt)
            return {"goal_artifact": nxt, "stage_envelope": nxt["stage"], "critic_iterations": 0}
        return await _critic_block(state, config, emitter, task_id, goal,
                             "plan produced no tracked steps — the plan must be a task list, not prose")

    # VERIFY exit — critic pass on the collected evidence (correctness
    # dimension): failing or missing verification routes back to implement;
    # persistent failure escalates.
    if stage == GoalStage.VERIFY:
        evidence = _extract_evidence(state)
        rubric = CriticRubric()
        findings = rubric.evaluate(plan=goal.get("plan_artifact"), evidence=evidence,
                                   diff_summary=goal.get("diff_summary"))
        should_block, reason = rubric.should_block(findings)
        if not should_block:
            nxt = advance_artifact(goal, GoalStage.REBASE_GATE)
            await _publish_stage_event(config, emitter, task_id, nxt)
            return {"goal_artifact": nxt, "stage_envelope": nxt["stage"], "critic_iterations": 0}
        # Back to implement with the findings (bounded by max_iterations).
        return await _critic_block(state, config, emitter, task_id, goal, reason,
                             send_back_to=GoalStage.IMPLEMENT)

    return {}


async def _critic_block(state: EngineState, config: RunnableConfig, emitter: EventEmitter,
                  task_id: str | None, goal: dict[str, Any], reason: str,
                  send_back_to: GoalStage | None = None) -> dict[str, Any]:
    """A blocking critic finding: bounded revision loop, then blocked-escalation."""
    iterations = state.get("critic_iterations", 0) + 1
    await _publish_events(config, [emitter._next(
        StepKind.STATUS, f"⚠ critic finding (round {iterations})",
        {"kind": "critic_finding", "severity": "block", "detail": reason, "iteration": iterations},
        task_id, None,
    )])
    if iterations >= _MAX_CRITIC_ITERATIONS:
        # Survivors past the iteration cap -> blocked-escalation (the human card).
        blocked = block_artifact(goal, f"critic: {reason}")
        await _publish_events(config, [emitter._next(
            StepKind.STATUS, "blocked-escalation",
            {"kind": "blocked", "reason": blocked["blocked_reason"], "goal_id": goal.get("goal_id")},
            task_id, None,
        )])
        return {"goal_artifact": blocked, "stage_envelope": blocked["stage"],
                "done": True, "blocked_reason": blocked["blocked_reason"],
                "critic_iterations": iterations}
    out: dict[str, Any] = {"critic_iterations": iterations}
    if send_back_to is not None:
        nxt = advance_artifact(goal, send_back_to)
        out["goal_artifact"] = nxt
        out["stage_envelope"] = nxt["stage"]
    # The finding rides as a nudge so the agent revises with full context.
    nudged = list(state.get("messages", [])) + [
        tag_message(HumanMessage(content=f"<critic-finding>\n{reason}\n</critic-finding>"), "nudge"),  # type: ignore[arg-type]
    ]
    out["messages"] = nudged
    return out


async def _publish_stage_event_recap(config: RunnableConfig, emitter: EventEmitter,
                               task_id: str | None, artifact: dict[str, Any]) -> None:
    """◆ recap block at every stage entry/advance — the progress-recap
    card kind (recap taxonomy)."""
    stage = artifact["stage"]
    goal_text = str(artifact.get("user_story") or artifact.get("goal", ""))[:120]
    await _publish_events(config, [emitter._next(
        StepKind.STATUS, f"◆ recap: stage {stage}",
        {
            "kind": "recap",
            "stage": stage,
            "goal_id": artifact.get("goal_id"),
            "summary": f"Stage advanced to {stage}. Goal: {goal_text}",
            "critic_iterations": artifact.get("critic_iterations", 0),
            "blockers": artifact.get("blockers", []),
        },
        task_id, None,
    )])


async def _publish_stage_event(config: RunnableConfig, emitter: EventEmitter,
                         task_id: str | None, artifact: dict[str, Any]) -> None:
    stage = artifact["stage"]
    await _publish_events(config, [emitter._next(
        StepKind.STATUS, f"goal stage → {stage}",
        {"kind": "goal_stage", "stage": stage, "goal_id": artifact.get("goal_id")},
        task_id, None,
    )])
    await _publish_stage_event_recap(config, emitter, task_id, artifact)


def _first_user_story(state: EngineState) -> str:
    for msg in state.get("messages", []):
        if isinstance(msg, HumanMessage) and (msg.additional_kwargs or {}).get("prompt_origin") == "user":
            return str(msg.content)
    return ""


def _clarify_answered(state: EngineState) -> bool:
    """The human's answers arrive as a USER-tagged message AFTER ask_user ran."""
    # H-10: read the per-run signal from state (snapshot by tools_node) instead
    # of the process-global, which was shared across concurrent runs.
    if state.get("pending_questions") is None:
        return False
    messages = state.get("messages", [])
    if not messages:
        return False
    last = messages[-1]
    return isinstance(last, HumanMessage) and (last.additional_kwargs or {}).get("prompt_origin") == "user"


def _extract_evidence(state: EngineState) -> dict[str, Any]:
    """Heuristic verify evidence: the most recent test-looking
    terminal_exec result decides tests_pass. The story->PR fixture hardens
    this into the real evidence contract."""
    messages = state.get("messages", [])
    for msg in reversed(messages):
        if isinstance(msg, ToolMessage) and msg.name == "terminal_exec":
            content = str(msg.content)
            if any(t in content for t in ("pytest", "PASSED", "FAILED", "tests passed", "npm test", "vitest")):
                if "[exit 0]" in content:
                    return {"tests_pass": True}
                return {"tests_pass": False, "detail": content[-500:]}
    return {"tests_pass": False, "detail": "no test command was run in the verify stage"}


# --- Routing ---

def _entry_route(state: EngineState) -> str:
    if _mode_of(state) == Mode.GOAL.value:
        return "goal"
    return "agent"


def _should_continue(state: EngineState) -> str:
    if state.get("done") or state.get("error"):
        return "end"
    if state.get("needs_compaction"):
        return "compaction"
    messages = state.get("messages", [])
    if not messages:
        return "end"
    last = messages[-1]
    tool_calls = getattr(last, "tool_calls", None) or []
    if tool_calls:
        # Fast path: skip the gate when no call could need approval. A
        # permission ruleset can still DENY or ASK on a READONLY tool (hard
        # git policies), so when one is active we route through the gate and
        # let it evaluate each call — the gate auto-approves no-match/allow
        # and blocks deny without a card. Previously the ruleset was never
        # consulted here, so DENY/ASK on readonly tools was bypassed (C-05).
        autonomy = _autonomy_of(state)
        if autonomy != "autonomous" and (
            state.get("permissions_active")
            or any(needs_approval(tc.get("name", ""), autonomy) for tc in tool_calls)
        ):
            return "gate"
        return "tools"
    # Turn complete. Goal mode: the stage machine decides what happens next.
    if _mode_of(state) == Mode.GOAL.value:
        return "goal"
    return "end"


def _after_goal_router(state: EngineState) -> str:
    if state.get("done"):
        return "end"
    return "agent"


def _after_compaction(state: EngineState) -> str:
    if state.get("done") or state.get("error"):
        return "end"
    return "agent"


# --- Graph builder ---

def build_graph(checkpointer: Any = None) -> Any:
    """Compile the assembled spine. The checkpointer is REQUIRED for the
    interrupt-driven approval + clarify transports — pass the production saver
    from checkpointer.open_checkpointer() (MemorySaver in tests)."""
    g = StateGraph(EngineState)
    g.add_node("goal_router", goal_router_node)
    g.add_node("agent", agent_node)
    g.add_node("approval_gate", approval_gate_node)
    g.add_node("tools", tools_node)
    g.add_node("compaction", compaction_node)

    g.add_conditional_edges(START, _entry_route, {"goal": "goal_router", "agent": "agent"})
    g.add_conditional_edges("goal_router", _after_goal_router, {"agent": "agent", "end": "__end__"})
    g.add_conditional_edges("agent", _should_continue, {
        "gate": "approval_gate",
        "tools": "tools",
        "compaction": "compaction",
        "goal": "goal_router",
        "end": "__end__",
    })
    g.add_conditional_edges("approval_gate", _after_gate, {
        "gate": "approval_gate",
        "tools": "tools",
        "end": "__end__",
    })
    g.add_edge("tools", "compaction")
    g.add_conditional_edges("compaction", _after_compaction, {"agent": "agent", "end": "__end__"})

    return g.compile(checkpointer=checkpointer)


async def _publish_events(config: RunnableConfig, events: list[StepEvent]) -> None:
    sink = config["configurable"].get("event_sink")
    if not sink or not events:
        return
    # Await inline (H-01): the old `asyncio.create_task(sink(events))` was
    # fire-and-forget — events were lost on shutdown, could publish out of
    # order vs turn_boundary, and sink exceptions vanished in an un-awaited
    # task. Awaiting flushes before the node returns (durable order) and lets
    # us surface sink errors to the log instead of swallowing them.
    try:
        await sink(events)
    except Exception as exc:
        log.warning("event sink failed: %s (count=%d)", exc, len(events))


__all__ = ["agent_node", "approval_gate_node", "build_graph", "compaction_node",
           "goal_router_node", "tools_node"]
