"""Contract tests for the RA spine (plan §23 — DONE = WIRED + EVIDENCED).

These drive the REAL assembled graph (agent -> approval_gate -> tools ->
compaction -> goal_router) with a scripted LLM — no gateway. They evidence:

  1. a 5-turn development thread on the real graph with a checkpointer;
  2. compaction firing on a seeded overflow (the §9 trigger);
  3. interrupt/resume carrying an approval across the checkpoint boundary —
     including across a GRAPH REBUILD (the container-replacement case);
  4. the 3-denial circuit breaker (blocked-escalation);
  5. the stuck-loop watchdog (nudge@3);
  6. the goal-mode stage machine (intake -> explore -> plan) and the critic
     loop (blocked-escalation after bounded iterations);
  7. ask_user pausing the pipeline (clarify = INPUT_REQUIRED) and the
     human-answer resume;
  8. budget reminders at 50%/80%;
  9. Postgres checkpointer evidence (gated on DATABASE_URL — run with the
     throwaway container for the RA exit artifact).
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from worker.engine.compaction import CompactionPolicy, Compactor
from worker.engine.events import EventEmitter
from worker.engine.goal_mode import GoalStage, clear_pending_questions
from worker.engine.graph import _should_continue, build_graph
from worker.engine.state import Budget, Mode, tag_message
from worker.engine.checkpointer import open_checkpointer

# ----------------------------------------------------------- scripted LLM


class ScriptedLLM:
    """A fake ChatOpenAI: plays back a fixed script of AIMessages."""

    def __init__(self, script: list[AIMessage]) -> None:
        self._script = list(script)

    def astream(self, messages: list, stream_mode: str = "messages") -> Any:
        msg = self._script.pop(0) if self._script else AIMessage(content="(script exhausted)")

        async def _gen() -> Any:
            yield msg

        return _gen()


def _tc(tc_id: str, name: str, args: dict[str, Any]) -> dict[str, Any]:
    return {"id": tc_id, "name": name, "args": args}


def _ai(text: str, tool_calls: list[dict[str, Any]] | None = None,
        usage: dict[str, int] | None = None) -> AIMessage:
    kwargs: dict[str, Any] = {"content": text}
    if tool_calls:
        kwargs["tool_calls"] = tool_calls
    if usage:
        kwargs["usage_metadata"] = {"input_tokens": usage.get("in", 100),
                                    "output_tokens": usage.get("out", 50),
                                    "total_tokens": usage.get("in", 100) + usage.get("out", 50)}
    return AIMessage(**kwargs)


class FakeBroker:
    """Runner-side broker stand-in: never always-allows; the test resumes
    interrupts manually with Command(resume=...)."""

    def __init__(self) -> None:
        self.persisted: list[str] = []

    async def is_always_allowed(self, name: str, args: dict[str, Any]) -> bool:
        return False

    def card_payload(self, name: str, args: dict[str, Any], tc_id: str) -> dict[str, Any]:
        return {
            "type": "approval_request", "approval_id": f"ap-{tc_id}",
            "tool": name, "args": args, "tool_call_id": tc_id,
            "preview": f"{name} preview", "destructive": False,
            "always_allowable": True,
        }

    async def persist_always_allow(self, name: str) -> None:
        self.persisted.append(name)


class EventCollector:
    def __init__(self) -> None:
        self.events: list[Any] = []

    async def sink(self, events: list) -> None:
        self.events.extend(events)

    async def flush(self) -> None:
        # _publish_events schedules the sink with create_task — drain it.
        await asyncio.sleep(0.05)


def _config(collector: EventCollector, *, saver_thread: str = "t-1",
            compactor: Compactor | None = None, workspace: str = "/ws") -> dict[str, Any]:
    return {
        "configurable": {
            "thread_id": saver_thread,
            "model": "kimi-foundry",
            "emitter": EventEmitter("run-1", "t-1"),
            "approval_broker": FakeBroker(),
            "compactor": compactor or Compactor(),
            "workspace": workspace,
            "event_sink": collector.sink,
        },
        "recursion_limit": 80,
    }


def _initial(mode: Mode = Mode.DEVELOPMENT, prompt: str = "do the work",
             autonomy: str = "supervised", workspace_note: bool = True) -> dict[str, Any]:
    content = prompt if not workspace_note else f"Workspace root: /ws\n\n{prompt}"
    return {
        "run_id": "run-1", "thread_id": "t-1", "context_id": "t-1",
        "task_id": "task-1", "mode": mode, "autonomy": autonomy,
        "budget": Budget(cap=5.0),
        "messages": [tag_message(HumanMessage(content=content), "user")],
        "done": False, "error": None,
        "approved_calls": {}, "denial_streak": 0, "tool_streak": {},
        "turn_count": 0, "compaction_count": 0, "compaction_retries": 0,
    }


def _patch_llm(monkeypatch: pytest.MonkeyPatch, script: list[AIMessage]) -> ScriptedLLM:
    llm = ScriptedLLM(script)
    monkeypatch.setattr("worker.engine.graph.make_llm", lambda *a, **k: llm)
    return llm


# ----------------------------------------------------------- 1. 5-turn e2e


@pytest.mark.asyncio
async def test_five_turn_development_thread_e2e(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """The RA exit evidence: a 5-turn development thread on the real graph
    (agent -> gate -> tools -> compaction loop) with a checkpointer."""
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path))
    (tmp_path / "hello.txt").write_text("line one\nline two\n")
    collector = EventCollector()
    saver = MemorySaver()
    graph = build_graph(checkpointer=saver)
    config = _config(collector)

    _patch_llm(monkeypatch, [
        _ai("", [_tc("tc1", "file_read", {"file_path": "hello.txt"})]),            # turn 1
        _ai("", [_tc("tc2", "file_glob", {"pattern": "*.txt"})]),                  # turn 2
        _ai("", [_tc("tc3", "file_search", {"pattern": "line"})]),                 # turn 3
        _ai("", [_tc("tc4", "terminal_exec", {"command": "echo spine-check"})]),   # turn 4
        _ai("All five turns done — the spine is wired.", usage={"in": 200, "out": 80}),  # turn 5
    ])

    result = await graph.ainvoke(_initial(autonomy="autonomous"), config)
    await collector.flush()

    assert result.get("error") is None
    assert result.get("turn_count") == 5
    # Every scripted tool actually executed through the real tools node.
    kinds = [e.kind.value for e in collector.events]
    assert "file_read" in kinds
    assert "command" in kinds  # glob/search/terminal cards
    texts = [str(e.detail.get("output", "")) for e in collector.events]
    assert any("line one" in t for t in texts)
    assert any("spine-check" in t for t in texts)
    # The checkpointer holds the thread (resume source).
    snap = await graph.aget_state(config)
    assert snap.values.get("turn_count") == 5
    assert len(snap.values.get("messages", [])) > 5


# ----------------------------------------------------------- 2. compaction fires


@pytest.mark.asyncio
async def test_compaction_fires_on_seeded_overflow(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Seeded message overflow -> the compaction node prunes and emits the
    compaction card. The §9 trigger is IN the run path, not just tested."""
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path))
    (tmp_path / "x.txt").write_text("hello\n")
    collector = EventCollector()
    tiny = Compactor(CompactionPolicy(context_limit=100, floor_messages=1, recent_window=2))
    graph = build_graph(checkpointer=MemorySaver())
    config = _config(collector, compactor=tiny)

    # Seed a large history (well over the 100-token policy limit).
    seeded = [
        tag_message(HumanMessage(content="initial request " + "x" * 200), "user"),
        *[
            tag_message(HumanMessage(content=f"old tool output {i} " + "y" * 200), "tool")
            for i in range(10)
        ],
    ]
    state = _initial(autonomy="autonomous")
    state["messages"] = seeded

    _patch_llm(monkeypatch, [
        _ai("", [_tc("tc1", "file_read", {"file_path": "x.txt"})]),
        _ai("done"),
    ])

    result = await graph.ainvoke(state, config)
    await collector.flush()

    assert result.get("error") is None
    assert result.get("compaction_count", 0) >= 1
    card_events = [e for e in collector.events if e.detail.get("kind") == "compaction_card"]
    assert card_events, "a compaction card event must be emitted"
    assert card_events[0].detail["pruned"] > 0
    # The pruned span left an honest marker in the conversation.
    markers = [m for m in result["messages"] if "[compacted]" in str(getattr(m, "content", ""))]
    assert markers


@pytest.mark.asyncio
async def test_context_overflow_forces_compaction_and_retries(monkeypatch: pytest.MonkeyPatch):
    """The force path: the LLM raises a context-length error, the graph routes
    to a forced compaction, and the turn RETRIES instead of dying."""
    collector = EventCollector()
    tiny = Compactor(CompactionPolicy(context_limit=50, floor_messages=1, recent_window=2))
    graph = build_graph(checkpointer=MemorySaver())
    config = _config(collector, compactor=tiny)

    class OverflowOnceLLM(ScriptedLLM):
        def __init__(self, script: list[AIMessage]) -> None:
            super().__init__(script)
            self._raised = False

        def astream(self, messages: list, stream_mode: str = "messages") -> Any:
            if not self._raised:
                self._raised = True
                raise RuntimeError("maximum context length exceeded")
            return super().astream(messages, stream_mode)

    llm = OverflowOnceLLM([_ai("recovered after compaction")])
    monkeypatch.setattr("worker.engine.graph.make_llm", lambda *a, **k: llm)

    state = _initial(autonomy="autonomous")
    state["messages"] = [
        tag_message(HumanMessage(content="big job " + "z" * 300), "user"),
        *[tag_message(HumanMessage(content="old " + "y" * 200), "tool") for _ in range(8)],
    ]
    result = await graph.ainvoke(state, config)
    await collector.flush()

    assert result.get("error") is None
    assert result.get("compaction_retries", 0) == 0  # reset after the healthy retry
    assert any(e.detail.get("kind") == "compaction_card" and e.detail.get("forced")
               for e in collector.events)


# ----------------------------------------------------------- 3. approval interrupt/resume


@pytest.mark.asyncio
async def test_approval_interrupt_resume_round_trip(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Supervised file_write: the gate interrupt()s with the card payload;
    Command(resume=allow) executes with the verbatim args. This is the plan §11
    Redis-driver transport (interrupt -> publish -> await -> resume)."""
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path))
    collector = EventCollector()
    graph = build_graph(checkpointer=MemorySaver())
    config = _config(collector)

    _patch_llm(monkeypatch, [
        _ai("", [_tc("tc1", "file_write", {"file_path": "out.txt", "content": "approved content"})]),
        _ai("wrote it"),
    ])

    await graph.ainvoke(_initial(), config)  # supervised -> gate interrupts

    snap = await graph.aget_state(config)
    interrupts = [i for task in snap.tasks for i in task.interrupts]
    assert len(interrupts) == 1
    payload = interrupts[0].value
    assert payload["type"] == "approval_request"
    assert payload["tool"] == "file_write"
    assert payload["args"]["content"] == "approved content"  # VERBATIM on the card

    # Nothing executed yet — the file must NOT exist.
    assert not (tmp_path / "out.txt").exists()

    result = await graph.ainvoke(Command(resume={"decision": "allow"}), config)
    await collector.flush()

    assert result.get("error") is None
    assert (tmp_path / "out.txt").read_text() == "approved content"
    assert result["approved_calls"]["tc1"]["approved"] is True


@pytest.mark.asyncio
async def test_approval_survives_graph_rebuild(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """The container-replacement case: the SAME checkpointer behind a NEWLY
    compiled graph still holds the pending approval — resume works across a
    process restart (this is what the CAS runtime could never do)."""
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path))
    collector = EventCollector()
    saver = MemorySaver()
    config = _config(collector)

    _patch_llm(monkeypatch, [
        _ai("", [_tc("tc1", "file_write", {"file_path": "survivor.txt", "content": "still here"})]),
        _ai("done"),
    ])

    graph_a = build_graph(checkpointer=saver)
    await graph_a.ainvoke(_initial(), config)
    snap = await graph_a.aget_state(config)
    assert any(i for task in snap.tasks for i in task.interrupts)

    # Simulate the container dying and a fresh process booting: new graph,
    # same checkpointer.
    graph_b = build_graph(checkpointer=saver)
    snap_b = await graph_b.aget_state(config)
    interrupts = [i for task in snap_b.tasks for i in task.interrupts]
    assert len(interrupts) == 1, "the pending approval must survive the rebuild"

    result = await graph_b.ainvoke(Command(resume={"decision": "allow"}), config)
    await collector.flush()
    assert (tmp_path / "survivor.txt").read_text() == "still here"
    assert result.get("error") is None


# ----------------------------------------------------------- 4. denial breaker


@pytest.mark.asyncio
async def test_three_denials_trigger_blocked_escalation(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """3 consecutive denials -> blocked-escalation (done + blocked_reason);
    the denied tool NEVER executes."""
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path))
    collector = EventCollector()
    graph = build_graph(checkpointer=MemorySaver())
    config = _config(collector)

    _patch_llm(monkeypatch, [
        _ai("", [_tc("tc1", "file_write", {"file_path": "a.txt", "content": "1"})]),
        _ai("", [_tc("tc2", "file_write", {"file_path": "b.txt", "content": "2"})]),
        _ai("", [_tc("tc3", "file_write", {"file_path": "c.txt", "content": "3"})]),
        _ai("should never get here"),
    ])

    await graph.ainvoke(_initial(), config)
    for decision in ({"decision": "deny", "reason": "no"},) * 3:
        result = await graph.ainvoke(Command(resume=decision), config)
    await collector.flush()

    snap = await graph.aget_state(config)
    assert snap.values.get("denial_streak") == 3
    assert snap.values.get("done") is True
    assert "blocked" in (snap.values.get("blocked_reason") or "")
    assert not (tmp_path / "a.txt").exists()
    assert not (tmp_path / "b.txt").exists()
    assert not (tmp_path / "c.txt").exists()
    # The agent SAW the denials as error tool results (information, not crash).
    denial_msgs = [m for m in snap.values["messages"]
                   if "denied" in str(getattr(m, "content", ""))]
    assert denial_msgs


# ----------------------------------------------------------- 5. stuck loop


@pytest.mark.asyncio
async def test_stuck_loop_nudges_at_three(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """The same failing call 3x -> a NUDGE-tagged message + a warning event."""
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path))
    collector = EventCollector()
    graph = build_graph(checkpointer=MemorySaver())
    config = _config(collector)

    _patch_llm(monkeypatch, [
        _ai("", [_tc("tc1", "file_read", {"file_path": "ghost.txt"})]),
        _ai("", [_tc("tc2", "file_read", {"file_path": "ghost.txt"})]),
        _ai("", [_tc("tc3", "file_read", {"file_path": "ghost.txt"})]),
        _ai("gave up"),
    ])

    result = await graph.ainvoke(_initial(autonomy="autonomous"), config)
    await collector.flush()

    nudges = [m for m in result["messages"]
              if (m.additional_kwargs or {}).get("prompt_origin") == "nudge"]
    assert nudges, "a stuck-loop nudge must be injected at streak 3"
    assert any("3 times" in str(m.content) for m in nudges)
    warnings = [e for e in collector.events if e.detail.get("kind") == "warning"]
    assert any("stuck_loop" == e.detail.get("warning") for e in warnings)


# ----------------------------------------------------------- 6. goal stage machine + critic


@pytest.mark.asyncio
async def test_goal_intake_explore_plan_advancement(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Goal mode: entry routes through the goal router (intake), a clear story
    skips clarify, and the explore turn-end advances the pipeline to plan."""
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path))
    (tmp_path / "routes.py").write_text("def upload(): pass\n")
    collector = EventCollector()
    graph = build_graph(checkpointer=MemorySaver())
    config = _config(collector)

    story = "Add rate limiting to the upload endpoint in api/routes.py"
    _patch_llm(monkeypatch, [
        _ai("", [_tc("tc1", "file_read", {"file_path": "routes.py"})]),   # explore turn
        _ai("Explored: routes.py has the upload handler."),                # explore ends
        # Plan stage: the agent produces prose, never a tracked task list
        # (update_tasks lands in RC) — the critic must block and escalate.
        _ai("The plan is to add a middleware."),
        _ai("Revised plan: add middleware with a token bucket."),
        _ai("Re-revised plan: token bucket middleware in routes.py."),
    ])

    result = await graph.ainvoke(_initial(mode=Mode.GOAL, prompt=story, autonomy="autonomous"), config)
    await collector.flush()

    goal = result.get("goal_artifact")
    assert goal is not None, "intake must create the goal artifact"
    stage_events = [e for e in collector.events if e.detail.get("kind") == "goal_stage"]
    stages_seen = [e.detail["stage"] for e in stage_events]
    assert GoalStage.EXPLORE.value in stages_seen
    assert GoalStage.PLAN.value in stages_seen
    assert stages_seen.index(GoalStage.EXPLORE.value) < stages_seen.index(GoalStage.PLAN.value)
    # The critic loop mounted at the plan exit: prose-only plans block, bounded
    # iterations, then blocked-escalation.
    assert goal["stage"] == GoalStage.BLOCKED.value
    findings = [e for e in collector.events if e.detail.get("kind") == "critic_finding"]
    assert len(findings) >= 3


@pytest.mark.asyncio
async def test_critic_loop_blocks_after_bounded_iterations(monkeypatch: pytest.MonkeyPatch):
    """A plan stage that never produces a tracked plan: the critic blocks,
    nudges, and after 3 iterations routes to blocked-escalation (done)."""
    collector = EventCollector()
    graph = build_graph(checkpointer=MemorySaver())
    config = _config(collector)

    story = "Implement caching for the search endpoint in the api module"
    _patch_llm(monkeypatch, [
        _ai("Let me think about the plan."),       # plan turn 1 — no tracker
        _ai("Still thinking about the plan."),     # plan turn 2 — no tracker
        _ai("More plan thinking."),                # plan turn 3 — no tracker
    ])

    state = _initial(mode=Mode.GOAL, prompt=story, autonomy="autonomous")
    state["goal_artifact"] = {
        "goal_id": "g-1", "user_story": story, "stage": GoalStage.PLAN.value,
        "schema_version": 1,
    }
    state["stage_envelope"] = GoalStage.PLAN.value

    result = await graph.ainvoke(state, config)
    await collector.flush()

    assert result.get("done") is True
    assert "critic" in (result.get("blocked_reason") or "")
    assert result["goal_artifact"]["stage"] == GoalStage.BLOCKED.value
    blocked = [e for e in collector.events if e.detail.get("kind") == "blocked"]
    assert blocked, "blocked-escalation must emit a durable event"
    findings = [e for e in collector.events if e.detail.get("kind") == "critic_finding"]
    assert len(findings) >= 3


# ----------------------------------------------------------- 7. ask_user pause/resume


@pytest.mark.asyncio
async def test_ask_user_pauses_then_human_answer_advances(monkeypatch: pytest.MonkeyPatch):
    """Clarify stage: ask_user pauses the pipeline (INPUT_REQUIRED). When the
    human's answer arrives as a USER message, the router advances to explore."""
    clear_pending_questions()
    collector = EventCollector()
    graph = build_graph(checkpointer=MemorySaver())
    config = _config(collector)

    story = "fix it"  # short + vague -> clarify
    _patch_llm(monkeypatch, [
        _ai("", [_tc("tc1", "ask_user", {"questions": [
            {"id": "q1", "prompt": "What outcome?", "options": [
                {"id": "a", "label": "Fix bug"}, {"id": "b", "label": "Add feature"}]},
            {"id": "q2", "prompt": "Which repo?", "options": [
                {"id": "a", "label": "This one"}, {"id": "b", "label": "Other"}]},
        ]})]),
        _ai("Exploring now."),
    ])

    state = _initial(mode=Mode.GOAL, prompt=story, autonomy="autonomous", workspace_note=False)
    result = await graph.ainvoke(state, config)
    await collector.flush()

    assert result.get("done") is True, "ask_user must pause the pipeline"
    assert result["goal_artifact"]["stage"] == GoalStage.CLARIFY.value

    # The human answers (the platform appends their message; new turn starts).
    # A completed graph re-enters from START when invoked with a state delta.
    snap = await graph.aget_state(config)
    messages = list(snap.values["messages"])
    messages.append(tag_message(HumanMessage(content="Fix the crash bug in this repo"), "user"))

    result = await graph.ainvoke({"messages": messages, "done": False}, config)
    await collector.flush()
    # The human's answer advanced clarify -> explore -> plan (stage events in
    # order); the prose-only plan then hit the bounded critic loop.
    stages_seen = [e.detail["stage"] for e in collector.events
                   if e.detail.get("kind") == "goal_stage"]
    assert GoalStage.EXPLORE.value in stages_seen
    assert GoalStage.PLAN.value in stages_seen
    assert stages_seen.index(GoalStage.EXPLORE.value) < stages_seen.index(GoalStage.PLAN.value)
    assert result["goal_artifact"]["stage"] in (GoalStage.PLAN.value, GoalStage.BLOCKED.value)
    clear_pending_questions()


# ----------------------------------------------------------- 8. budget reminders


@pytest.mark.asyncio
async def test_budget_reminder_fires_at_threshold(monkeypatch: pytest.MonkeyPatch):
    collector = EventCollector()
    graph = build_graph(checkpointer=MemorySaver())
    config = _config(collector)

    _patch_llm(monkeypatch, [_ai("done", usage={"in": 10_000_000, "out": 5_000_000})])
    state = _initial(autonomy="autonomous")
    state["budget"] = Budget(used=0.0, cap=1.0)

    result = await graph.ainvoke(state, config)
    await collector.flush()

    assert result["budget"].used > 0
    warnings = [e for e in collector.events
                if e.detail.get("kind") == "warning" and e.detail.get("warning") == "budget"]
    assert warnings, "a budget warning event must fire when a threshold is crossed"
    assert warnings[0].detail["level"] == "80"


# ----------------------------------------------------------- routing sanity


def test_fast_path_skips_gate_for_readonly_batch():
    ai = _ai("", [_tc("tc1", "file_read", {"file_path": "x"})])
    assert _should_continue({"messages": [ai], "mode": Mode.DEVELOPMENT, "autonomy": "supervised"}) == "tools"


def test_gate_route_for_mutating_in_supervised():
    ai = _ai("", [_tc("tc1", "file_write", {"file_path": "x", "content": "y"})])
    assert _should_continue({"messages": [ai], "mode": Mode.DEVELOPMENT, "autonomy": "supervised"}) == "gate"


def test_autonomous_skips_gate():
    ai = _ai("", [_tc("tc1", "file_write", {"file_path": "x", "content": "y"})])
    assert _should_continue({"messages": [ai], "mode": Mode.DEVELOPMENT, "autonomy": "autonomous"}) == "tools"


def test_goal_mode_turn_end_routes_to_goal_router():
    ai = _ai("stage done")
    assert _should_continue({"messages": [ai], "mode": Mode.GOAL, "autonomy": "autonomous"}) == "goal"


def test_ask_mode_turn_end_routes_to_end():
    ai = _ai("answer")
    assert _should_continue({"messages": [ai], "mode": Mode.ASK, "autonomy": "supervised"}) == "end"


# ----------------------------------------------------------- 9. Postgres evidence


@pytest.mark.asyncio
async def test_open_checkpointer_memory_fallback_without_database_url(monkeypatch: pytest.MonkeyPatch):
    """No DATABASE_URL -> loud MemorySaver fallback (dev/test only)."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    async with open_checkpointer() as saver:
        assert isinstance(saver, MemorySaver)


@pytest.mark.asyncio
async def test_postgres_checkpointer_holds_thread_and_approval(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
):
    """RA EXIT EVIDENCE (Postgres): a thread runs on the REAL Postgres
    checkpointer, an approval interrupt persists IN THE DATABASE, and a fresh
    connection resumes it. Skipped unless DATABASE_URL is set (spin the
    throwaway container: scripts/ra_evidence.sh)."""
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        pytest.skip("DATABASE_URL unset — run with the throwaway Postgres container")
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path))
    collector = EventCollector()

    _patch_llm(monkeypatch, [
        _ai("", [_tc("tc1", "file_write", {"file_path": "pg.txt", "content": "postgres-proof"})]),
        _ai("done"),
    ])

    async with open_checkpointer(conn_string=dsn) as saver:
        graph = build_graph(checkpointer=saver)
        config = _config(collector, saver_thread=f"pg-proof-{os.getpid()}")
        await graph.ainvoke(_initial(), config)
        snap = await graph.aget_state(config)
        assert any(i for task in snap.tasks for i in task.interrupts), \
            "the approval interrupt must persist in Postgres"

    # Fresh connection (new process posture): the pending approval is still there.
    async with open_checkpointer(conn_string=dsn) as saver2:
        graph2 = build_graph(checkpointer=saver2)
        config2 = _config(collector, saver_thread=f"pg-proof-{os.getpid()}")
        snap2 = await graph2.aget_state(config2)
        interrupts = [i for task in snap2.tasks for i in task.interrupts]
        assert len(interrupts) == 1, "Postgres must hold the pending approval across connections"
        result = await graph2.ainvoke(Command(resume={"decision": "allow"}), config2)
        await collector.flush()

    assert (tmp_path / "pg.txt").read_text() == "postgres-proof"
    assert result.get("error") is None
