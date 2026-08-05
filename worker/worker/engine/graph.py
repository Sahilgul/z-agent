"""Engine core loop — the LangGraph StateGraph (RA spine, plan §6/§23).

The assembled run path (this is what the Round-33 audit found missing — every
node below was a tested island; now they are MOUNTED):

  START -> [goal? goal_router] -> agent -> approval_gate? -> tools -> compaction -> agent ...

  agent          LLM + mode-aware tool binding + transient per-turn envelope
                 (D1 env block + mode envelope + goal-stage envelope) +
                 usage/budget accounting + budget reminders.
  approval_gate  interrupt()-driven two-phase gate (plan §11 Redis driver:
                 interrupt -> runner publishes card -> awaits -> Command(resume=)).
                 ONE interrupt per node execution; denials feed the 3-denial
                 circuit breaker (blocked-escalation).
  tools          Executes decided calls (denied calls become error ToolMessages,
                 never execute). Stuck-loop watchdog: nudge@3, decision-menu@5,
                 hand-off@8, force-stop@12 (plan §13). ask_user pauses the turn
                 (goal-mode clarify = INPUT_REQUIRED).
  compaction     prune -> summarize -> splice (plan §9) with the §9 trigger and
                 the force path for context-overflow retries; emits the
                 compaction-card event.
  goal_router    The goal-mode stage subgraph (plan §8): intake -> clarify? ->
                 explore -> plan -> implement -> verify -> rebase-gate -> PR,
                 with the critic loop (plan/verify exits) and blocked-escalation.

The graph is compiled WITH a checkpointer (RA: Postgres default via
checkpointer.open_checkpointer; MemorySaver in tests). interrupt/resume is the
approval + clarify transport, so it must survive container replacement.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
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
from worker.engine.llm import estimate_cost, make_llm, with_gateway_retry
from worker.engine.state import Budget, EngineState, Mode, tag_message
from worker.engine.tools import call_tool_direct, needs_approval, tools_for_mode
from worker.engine.watchdogs import CriticRubric

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"

# Stuck-loop watchdog thresholds (plan §13).
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
    """base.md + model suffix (plan §10 — ONE system message per turn)."""
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
    """The transient per-turn envelope (plan §10 — D1 env block + D2 mode
    envelope + goal-stage envelope). Rides as a synthetic user-role message,
    NEVER persisted, NEVER part of the system message."""
    mode = _mode_of(state)
    parts: list[str] = []

    # D1 — environment block (the source of truth for where/when the agent is)
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

    # D2 — mode envelope (advertises the mode's tool filter — fixes the
    # Round-33 prompt/tool drift class)
    envelope_text = _read_prompt(f"envelopes/{mode}.md").strip()
    if envelope_text:
        parts.append(f"<mode-envelope mode=\"{mode}\">\n{envelope_text}\n</mode-envelope>")

    # Goal-stage envelope (the stage subgraph's per-turn fragment)
    if mode == Mode.GOAL.value or mode == "goal":
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


# --- The agent node ---

async def agent_node(state: EngineState, config: RunnableConfig) -> dict[str, Any]:
    """Call the LLM with mode-bound tools; stream deltas; collect the AIMessage;
    account usage into the budget; fire budget reminders."""
    model = config["configurable"]["model"]
    task_id = state.get("task_id")
    emitter: EventEmitter = config["configurable"]["emitter"]
    delta_sink = config["configurable"].get("delta_sink")

    mode = _mode_of(state)
    llm = make_llm(model, streaming=True, tools=tools_for_mode(mode))
    system = _build_system_message(model, mode)  # type: ignore[arg-type]
    messages = [system] + state.get("messages", [])
    envelope = _build_turn_envelope(state, config)
    if envelope is not None:
        messages = messages + [envelope]

    ai_message: AIMessage | None = None
    try:
        # Gateway retry wraps the stream start; once streaming, retries are
        # not safe (partial deltas) — a mid-stream failure fails the turn.
        async for chunk in with_gateway_retry(
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
        # (plan §9 force-compaction path; the retry cap stops an infinite loop).
        if _looks_like_context_overflow(err) and state.get("compaction_retries", 0) < _MAX_COMPACTION_RETRIES:
            tuning = config["configurable"].get("tuning")
            if tuning is not None:
                tuning.observe_error(err)
            return {"needs_compaction": True, "force_compact": True}
        return {"error": err, "done": True}

    if ai_message is None:
        return {"error": "no response from LLM", "done": True}

    events = emitter.from_assistant(ai_message, task_id)
    _publish_events(config, events)

    # Budget accounting (plan §13): usage -> cost -> budget.used; the gateway
    # remains the hard cap, this drives the 50%/80% reminders.
    usage = getattr(ai_message, "usage_metadata", None)
    out: dict[str, Any] = {
        "messages": state.get("messages", []) + [ai_message],
        "turn_count": state.get("turn_count", 0) + 1,
        "compaction_retries": 0,  # a healthy turn resets the overflow-retry loop guard
        "last_usage": dict(usage) if usage else None,
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
            _publish_events(config, [reminder])
    tuning = config["configurable"].get("tuning")
    if tuning is not None:
        tuning.observe_healthy_turn()
    return out


def _looks_like_context_overflow(err: str) -> bool:
    low = err.lower()
    return ("context" in low and "length" in low) or "maximum context" in low or "too many tokens" in low


def _budget_reminder(emitter: EventEmitter, task_id: str | None,
                     prev_used: float, new_used: float, cap: float) -> StepEvent | None:
    """50%/80% budget reminders (plan §13). Never auto-stops."""
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


# --- The approval gate node (interrupt-driven, plan §11 Redis driver) ---

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
        if not needs_approval(name, autonomy):
            approved[tc_id] = {"approved": True, "via": "not_required"}
            continue
        if broker is not None and await broker.is_always_allowed(name, args):
            approved[tc_id] = {"approved": True, "via": "always_allow"}
            continue
        payload = (
            broker.card_payload(name, args, tc_id) if broker is not None
            else {"type": "approval_request", "tool": name, "args": args, "tool_call_id": tc_id}
        )
        decision = interrupt(payload)
        verdict = decision.get("decision", "deny")
        if verdict in ("allow", "allow_once", "always_allow"):
            approved[tc_id] = {"approved": True, "via": verdict, "args": args}
            denial_streak = 0
            if verdict == "always_allow" and broker is not None:
                await broker.persist_always_allow(name)
        else:
            approved[tc_id] = {"approved": False, "reason": decision.get("reason", "denied by user")}
            denial_streak += 1
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

    for tc in tool_calls:
        tc_id = tc.get("id", "")
        name = tc.get("name", "")
        args = tc.get("args", {}) or {}

        decision = approved.get(tc_id)
        if decision is not None and not decision.get("approved", True):
            # Denied at the gate — the agent sees the reason, nothing executes.
            result = {"kind": "error", "ok": False,
                      "output": f"error: denied — {decision.get('reason', 'denied by user')}"}
        else:
            # §3: execute with the VERBATIM args recorded at the gate.
            exec_args = (decision or {}).get("args", args)
            result = await call_tool_direct(name, exec_args)

        event = emitter._next(
            _tool_kind(name), _tool_title(name, args),
            {"tool": name, "input": args, "output": result["output"], "ok": result["ok"]},
            task_id, None,
        )
        _publish_events(config, [event])
        new_messages.append(tag_message(ToolMessage(
            content=result["output"], tool_call_id=tc_id, name=name,
        ), "tool"))  # type: ignore[arg-type]

        # ask_user pauses the pipeline (goal-mode clarify = INPUT_REQUIRED).
        if name == "ask_user" and result["ok"]:
            out["done"] = True

        # Stuck-loop watchdog (plan §13)
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
                _publish_events(config, [warn_event])
                if force_stop:
                    out["done"] = True
                    out["blocked_reason"] = f"stuck loop: same failing call {count}x — force-stop"
                    out["error"] = out["blocked_reason"]

    out["messages"] = new_messages
    out["tool_streak"] = streaks
    return out


def _call_signature(name: str, args: dict[str, Any]) -> str:
    blob = json.dumps(args, sort_keys=True, default=str)
    return f"{name}:{hashlib.sha256(blob.encode()).hexdigest()[:12]}"


def _streak_response(emitter: EventEmitter, task_id: str | None,
                     count: int) -> tuple[str, StepEvent, bool] | None:
    """nudge@3 -> decision-menu@5 -> hand-off@8 -> force-stop@12 (plan §13)."""
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
    """prune -> summarize -> splice (plan §9) + the force path for context
    overflow. Emits the compaction-card event; honesty-validator rollbacks
    surface as warnings instead of silent drops."""
    compactor: Compactor = config["configurable"].get("compactor") or Compactor()
    emitter: EventEmitter = config["configurable"]["emitter"]
    # Self-tuning limit (plan §9): the learned limit drives the trigger.
    tuning = config["configurable"].get("tuning")
    if tuning is not None:
        compactor.policy.context_limit = tuning.current
    task_id = state.get("task_id")
    messages = state.get("messages", [])
    force = state.get("force_compact", False)

    new_messages, result = compactor.compact(messages, force=force)
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
        _publish_events(config, [event])
    return out


# --- The goal router node (the stage subgraph, plan §8) ---

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
        _publish_events(config, [emitter._next(
            StepKind.STATUS, f"goal intake → {artifact['stage']}",
            {"kind": "goal_stage", "stage": artifact["stage"], "goal_id": artifact["goal_id"]},
            task_id, None,
        )])
        return {"goal_artifact": artifact, "stage_envelope": artifact["stage"]}

    stage = stage_of(goal)

    # Terminal-ish stages
    if stage in (GoalStage.DONE, GoalStage.BLOCKED):
        return {"done": True}
    if stage == GoalStage.PR:
        # The PR-summary turn just completed — the platform opens the PR from
        # the summary. Goal complete.
        _publish_events(config, [emitter._next(
            StepKind.STATUS, "goal complete — PR stage reached",
            {"kind": "goal_stage", "stage": GoalStage.DONE.value, "goal_id": goal.get("goal_id")},
            task_id, None,
        )])
        return {"goal_artifact": advance_artifact(goal, GoalStage.DONE), "done": True}

    # CLARIFY — paused until the human's answers arrive (the runner appends
    # them as a USER message and starts a new turn).
    if stage == GoalStage.CLARIFY:
        if _clarify_answered(state):
            clear_pending_questions()
            nxt = advance_artifact(goal, GoalStage.EXPLORE)
            _publish_stage_event(config, emitter, task_id, nxt)
            return {"goal_artifact": nxt, "stage_envelope": nxt["stage"]}
        # Not answered yet — end the turn (INPUT_REQUIRED). If the agent never
        # called ask_user, the clarify envelope pushes it to.
        return {"done": True}

    # Turn-end advances.
    if stage in ADVANCE_ON_TURN_END:
        nxt_stage = ADVANCE_ON_TURN_END[stage]
        if stage == GoalStage.EXPLORE:
            nxt = advance_artifact(goal, nxt_stage)
            _publish_stage_event(config, emitter, task_id, nxt)
            return {"goal_artifact": nxt, "stage_envelope": nxt["stage"], "critic_iterations": 0}
        if stage == GoalStage.IMPLEMENT:
            nxt = advance_artifact(goal, nxt_stage)
            _publish_stage_event(config, emitter, task_id, nxt)
            return {"goal_artifact": nxt, "stage_envelope": nxt["stage"]}
        if stage == GoalStage.REBASE_GATE:
            nxt = advance_artifact(goal, nxt_stage)
            _publish_stage_event(config, emitter, task_id, nxt)
            return {"goal_artifact": nxt, "stage_envelope": nxt["stage"]}

    # PLAN exit — critic pass: a plan with no tracked steps blocks (merged
    # rubric, completeness dimension). Blocking findings reference the plan.
    if stage == GoalStage.PLAN:
        tracker = state.get("task_tracker") or goal.get("task_tracker")
        if tracker:
            nxt = advance_artifact(goal, GoalStage.IMPLEMENT)
            nxt["task_tracker"] = tracker
            _publish_stage_event(config, emitter, task_id, nxt)
            return {"goal_artifact": nxt, "stage_envelope": nxt["stage"], "critic_iterations": 0}
        return _critic_block(state, config, emitter, task_id, goal,
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
            _publish_stage_event(config, emitter, task_id, nxt)
            return {"goal_artifact": nxt, "stage_envelope": nxt["stage"], "critic_iterations": 0}
        # Back to implement with the findings (bounded by max_iterations).
        return _critic_block(state, config, emitter, task_id, goal, reason,
                             send_back_to=GoalStage.IMPLEMENT)

    return {}


def _critic_block(state: EngineState, config: RunnableConfig, emitter: EventEmitter,
                  task_id: str | None, goal: dict[str, Any], reason: str,
                  send_back_to: GoalStage | None = None) -> dict[str, Any]:
    """A blocking critic finding: bounded revision loop, then blocked-escalation."""
    iterations = state.get("critic_iterations", 0) + 1
    _publish_events(config, [emitter._next(
        StepKind.STATUS, f"⚠ critic finding (round {iterations})",
        {"kind": "critic_finding", "severity": "block", "detail": reason, "iteration": iterations},
        task_id, None,
    )])
    if iterations >= _MAX_CRITIC_ITERATIONS:
        # Survivors past the iteration cap -> blocked-escalation (the human card).
        blocked = block_artifact(goal, f"critic: {reason}")
        _publish_events(config, [emitter._next(
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


def _publish_stage_event(config: RunnableConfig, emitter: EventEmitter,
                         task_id: str | None, artifact: dict[str, Any]) -> None:
    _publish_events(config, [emitter._next(
        StepKind.STATUS, f"goal stage → {artifact['stage']}",
        {"kind": "goal_stage", "stage": artifact["stage"], "goal_id": artifact.get("goal_id")},
        task_id, None,
    )])


def _first_user_story(state: EngineState) -> str:
    for msg in state.get("messages", []):
        if isinstance(msg, HumanMessage) and (msg.additional_kwargs or {}).get("prompt_origin") == "user":
            return str(msg.content)
    return ""


def _clarify_answered(state: EngineState) -> bool:
    """The human's answers arrive as a USER-tagged message AFTER ask_user ran."""
    if get_pending_questions() is None:
        return False
    messages = state.get("messages", [])
    if not messages:
        return False
    last = messages[-1]
    return isinstance(last, HumanMessage) and (last.additional_kwargs or {}).get("prompt_origin") == "user"


def _extract_evidence(state: EngineState) -> dict[str, Any]:
    """Heuristic verify evidence (RA mount): the most recent test-looking
    terminal_exec result decides tests_pass. RE's story->PR fixture hardens
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
        # Fast path: skip the gate when no call could need approval.
        autonomy = _autonomy_of(state)
        if any(needs_approval(tc.get("name", ""), autonomy) for tc in tool_calls):
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
    """Compile the assembled spine (RA). The checkpointer is REQUIRED for the
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


def _publish_events(config: RunnableConfig, events: list[StepEvent]) -> None:
    sink = config["configurable"].get("event_sink")
    if sink and events:
        # sink is an async callable; schedule without blocking the graph
        asyncio.create_task(sink(events))


__all__ = ["agent_node", "approval_gate_node", "build_graph", "compaction_node",
           "goal_router_node", "tools_node"]
