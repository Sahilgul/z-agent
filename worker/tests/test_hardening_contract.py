"""Durability + rollback-drill tests.

Validates:
- Redis stream events survive a consumer disconnect (the events table is the
  system of record; pub/sub loss is acceptable, stream loss is not).
- Postgres checkpoint survives a process restart (the DeltaChannel JSONL
  mirror is the replay source when Postgres is unavailable).
- The rollback drill: simulate a failed cutover, verify the engine can
  resume from the last checkpoint without losing the conversation.

These use fakeredis (no real Redis) and the MemorySaver (no real Postgres) so
they run in CI. The real Redis/Postgres durability is validated by the
operational soak (run inside the container).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from zagent_contracts import StepEvent, StepKind

from worker.engine.checkpointer import DeltaChannel, make_checkpointer
from worker.engine.compaction import CompactionPolicy, Compactor
from worker.engine.hardening import SoakResult, evaluate_slo
from worker.engine.state import Budget, Mode, PromptOrigin, tag_message

# --- scripted LLM (for the checkpointer resume drill) ---

from typing import Any

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import MemorySaver

from worker.engine.events import EventEmitter
from worker.engine.graph import build_graph


class _ScriptedLLM:
    """A fake ChatOpenAI: plays back a fixed script of AIMessages."""

    def __init__(self, script: list[AIMessage]) -> None:
        self._script = list(script)

    def astream(self, messages: list, stream_mode: str = "messages") -> Any:
        msg = self._script.pop(0) if self._script else AIMessage(content="(script exhausted)")

        async def _gen() -> Any:
            yield msg

        return _gen()


def _ai(text: str, tool_calls: list[dict[str, Any]] | None = None) -> AIMessage:
    kwargs: dict[str, Any] = {"content": text}
    if tool_calls:
        kwargs["tool_calls"] = tool_calls
    return AIMessage(**kwargs)


def _tc(tc_id: str, name: str, args: dict[str, Any]) -> dict[str, Any]:
    return {"id": tc_id, "name": name, "args": args}


def _resume_config(thread_id: str = "resume-t") -> dict[str, Any]:
    async def _sink(events: list) -> None:
        pass

    return {
        "configurable": {
            "thread_id": thread_id,
            "model": "kimi-foundry",
            "emitter": EventEmitter("run-1", thread_id),
            "approval_broker": None,
            "compactor": Compactor(),
            "workspace": "/ws",
            "event_sink": _sink,
        },
        "recursion_limit": 80,
    }


def _resume_initial(prompt: str = "do the work") -> dict[str, Any]:
    return {
        "run_id": "run-1", "thread_id": "t-1", "context_id": "t-1",
        "task_id": "task-1", "mode": Mode.DEVELOPMENT, "autonomy": "autonomous",
        "budget": Budget(cap=5.0),
        "messages": [tag_message(HumanMessage(content=f"Workspace root: /ws\n\n{prompt}"), "user")],
        "done": False, "error": None,
        "approved_calls": {}, "denial_streak": 0, "tool_streak": {},
        "turn_count": 0, "compaction_count": 0, "compaction_retries": 0,
    }

# --- Redis stream durability ---

@pytest.mark.asyncio
async def test_redis_stream_survives_consumer_disconnect():
    """Events written via the ENGINE's Forwarder.publish_events survive even
    if the consumer disconnects mid-replay. The stream is the durable leg;
    pub/sub is lossy.

    M-26: the old test called fakeredis.xadd/xread directly — it tested
    fakeredis's stream behavior, NOT the engine's publish path. Drive the
    real Forwarder (with a fakeredis backend injected) so the durability
    contract is verified against the code that actually publishes events
    (pipeline + xadd + JSON payload)."""
    pytest.importorskip("fakeredis.aioredis")
    import fakeredis.aioredis as fake_mod
    from worker.forwarder import Forwarder
    from worker.engine.events import EventEmitter

    r = fake_mod.FakeRedis(decode_responses=True)
    # Inject the fakeredis backend into a REAL Forwarder so we exercise the
    # engine's publish path (pipeline + xadd), not fakeredis in isolation.
    fwd = Forwarder("redis://fake", "run-1", "t1")
    fwd.redis = r

    # Build events through the real EventEmitter (the engine's event factory).
    em = EventEmitter("run-1", "t1")
    events = [em._next(StepKind.STATUS, f"event-{i}", {"i": i}, None, None)
              for i in range(5)]
    await fwd.publish_events(events)

    # Consumer reads some, then "disconnects" (we just stop reading).
    first_batch = await r.xread({fwd.stream_key: "0"}, count=2, block=100)
    assert len(first_batch) == 1 and len(first_batch[0][1]) == 2

    # The remaining events are still in the stream. Read all and verify the
    # unconsumed ones are present (fakeredis doesn't support exclusive min).
    all_entries = await r.xrange(fwd.stream_key)
    assert len(all_entries) == 5  # all 5 still in the stream
    # M-26: verify the payloads round-trip through the engine's JSON encoding
    # (the real publish path writes json.dumps(event.model_dump())).
    payloads = [json.loads(e[1]["payload"]) for e in all_entries]
    assert [p["seq"] for p in payloads] == [0, 1, 2, 3, 4]
    await r.aclose()


# --- DeltaChannel JSONL mirror durability ---

@pytest.mark.asyncio
async def test_delta_channel_mirror_is_replayable(tmp_path: Path):
    """The JSONL mirror is the replay source when Postgres is unavailable."""
    dc = DeltaChannel(tmp_path)
    await dc.append("t1", "t1", "ckpt-1", {"task_id": "task-1", "turn": 0})
    await dc.append("t1", "t1", "ckpt-2", {"task_id": "task-2", "turn": 1})

    mirror = dc.mirror_path("t1")
    assert mirror.exists()
    lines = mirror.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert first["checkpoint_id"] == "ckpt-1"
    assert first["metadata"]["task_id"] == "task-1"


@pytest.mark.asyncio
async def test_delta_channel_replay_recovers_task_boundaries(tmp_path: Path):
    """Replaying the mirror yields the task boundaries (each task = one checkpoint)."""
    dc = DeltaChannel(tmp_path)
    for i in range(5):
        await dc.append("t1", "t1", f"ckpt-{i}", {"task_id": f"task-{i}", "turn": i})

    mirror = dc.mirror_path("t1")
    tasks = []
    for line in mirror.read_text(encoding="utf-8").strip().splitlines():
        entry = json.loads(line)
        tasks.append(entry["metadata"]["task_id"])
    assert tasks == [f"task-{i}" for i in range(5)]


# --- Checkpointer resume ---

@pytest.mark.asyncio
async def test_memory_saver_resume_preserves_conversation(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """A resumed thread continues from the last checkpoint, not from scratch.

    H-19: the old test created a MemorySaver and asserted `cp is not None` —
    it never ran the graph or resumed from a checkpoint, so the resume
    contract was untested. Here we run a real turn on the graph with a
    MemorySaver, then re-invoke with a nudge delta (the resume path) and
    assert the first turn's messages survive in the resumed state."""
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path))
    (tmp_path / "hello.txt").write_text("line one\nline two\n")
    saver = MemorySaver()
    graph = build_graph(checkpointer=saver)
    config = _resume_config()

    llm = _ScriptedLLM([
        _ai("", [_tc("tc1", "file_read", {"file_path": "hello.txt"})]),  # turn 1
        _ai("resumed turn two"),                                  # turn 2 (nudge)
    ])
    monkeypatch.setattr("worker.engine.graph.make_llm", lambda *a, **k: llm)

    # Turn 1: runs the agent -> tools loop and checkpoints the conversation.
    first = await graph.ainvoke(_resume_initial(), config)
    assert first.get("error") is None
    first_messages = list(first.get("messages", []))
    # file_read returns line-numbered text ("     1|line one\n     2|line two\n...").
    assert any("line one" in str(getattr(m, "content", "")) for m in first_messages), (
        "turn 1 must have read hello.txt")

    # Resume: a completed graph re-enters from START when invoked with a
    # state delta (ainvoke(None) would be a no-op). The checkpoint must
    # persist turn 1's messages — NOT start from scratch.
    snap = await graph.aget_state(config)
    messages = list(snap.values.get("messages", []))
    messages.append(tag_message(HumanMessage(content="nudge: now turn two"), "nudge"))
    resumed = await graph.ainvoke({"messages": messages, "done": False}, config)
    resumed_messages = list(resumed.get("messages", []))
    # The original user message + the tool result must survive the resume.
    contents = [str(getattr(m, "content", "")) for m in resumed_messages]
    assert "Workspace root: /ws\n\ndo the work" in contents, (
        "resume must preserve turn 1's user message, not start from scratch")
    assert any("line one" in c for c in contents), (
        "resume must preserve turn 1's tool result")


# --- Rollback drill ---

@pytest.mark.asyncio
async def test_rollback_drill_compaction_preserves_protected():
    """The rollback drill: a compaction that would drop protected messages
    rolls back, leaving the conversation intact (no data loss)."""
    from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
    compactor = Compactor(policy=CompactionPolicy(context_limit=10, recent_window=1, floor_messages=1))
    messages = [
        tag_message(SystemMessage(content="system"), PromptOrigin.SYSTEM),  # type: ignore[arg-type]
        tag_message(HumanMessage(content="user-1"), PromptOrigin.USER),  # type: ignore[arg-type]
        *[tag_message(AIMessage(content=f"a{i}" * 20), PromptOrigin.ASSISTANT) for i in range(20)],  # type: ignore[arg-type]
        tag_message(HumanMessage(content="recent"), PromptOrigin.USER),  # type: ignore[arg-type]
    ]
    new, _result = await compactor.compact(messages)
    contents = [str(m.content) for m in new]
    assert "system" in contents
    assert "user-1" in contents


@pytest.mark.asyncio
async def test_compaction_rolls_back_when_protected_message_dropped(monkeypatch):
    """G-02: the honesty validator's rollback branch is unreachable under the
    current _PRUNE_ORDER — protected origins (SYSTEM/USER/NUDGE) are never
    pruned, so a protected message always survives and `missing` is always
    empty. Force the branch by temporarily admitting a protected origin
    (USER) into the prune order so a USER message in the prunable span gets
    dropped; the validator must catch it, roll back, and leave the
    conversation intact (original list, unchanged, after_tokens == before_tokens)."""
    from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
    from worker.engine import compaction as comp_mod

    monkeypatch.setattr(
        comp_mod, "_PRUNE_ORDER",
        [PromptOrigin.TOOL, PromptOrigin.ENVELOPE, PromptOrigin.MEMORY,
         PromptOrigin.ASSISTANT, PromptOrigin.USER],
    )
    compactor = Compactor(policy=CompactionPolicy(context_limit=10, recent_window=1, floor_messages=1))
    user_msg = tag_message(HumanMessage(content="user-1"), PromptOrigin.USER)  # type: ignore[arg-type]
    messages = [
        tag_message(SystemMessage(content="system"), PromptOrigin.SYSTEM),  # type: ignore[arg-type]
        user_msg,
        *[tag_message(AIMessage(content=f"a{i}" * 20), PromptOrigin.ASSISTANT) for i in range(20)],  # type: ignore[arg-type]
        tag_message(HumanMessage(content="recent"), PromptOrigin.USER),  # type: ignore[arg-type]
    ]
    new, result = await compactor.compact(messages)
    assert result.rolled_back is True
    assert "protected message changed/dropped" in result.rollback_reason
    # The conversation is left intact: the original list is returned unchanged.
    assert new is messages
    assert result.after_tokens == result.before_tokens


@pytest.mark.asyncio
async def test_publish_events_swallows_sink_failure():
    """G-03: _publish_events must not crash the graph when the event sink
    raises — the failure is logged and swallowed (events are best-effort;
    a sink blip must not kill the turn). Covers the except-branch at
    graph.py:_publish_events."""
    from worker.engine.graph import _publish_events

    async def boom(_events):
        raise RuntimeError("sink down")

    config = {"configurable": {"event_sink": boom}}
    events = [StepEvent(run_id="r1", thread_id="t1", seq=0,
                        kind=StepKind.MESSAGE, title="turn")]
    # Must not raise — the sink failure is swallowed.
    await _publish_events(config, events)


@pytest.mark.asyncio
async def test_publish_events_noop_without_sink_or_events():
    """G-03: the no-sink and no-events early returns are covered (the
    guard at the top of _publish_events)."""
    from worker.engine.graph import _publish_events

    async def sink(_events):
        return None

    # No events -> early return, sink not called.
    await _publish_events({"configurable": {"event_sink": sink}}, [])
    # No sink -> early return.
    await _publish_events({"configurable": {}},
                          [StepEvent(run_id="r1", thread_id="t1", seq=0,
                                     kind=StepKind.MESSAGE, title="x")])
    # Sink None -> early return.
    await _publish_events({"configurable": {"event_sink": None}},
                          [StepEvent(run_id="r1", thread_id="t1", seq=0,
                                     kind=StepKind.MESSAGE, title="x")])


@pytest.mark.asyncio
async def test_agent_node_mid_stream_llm_failure_emits_partial_delta_then_errors(monkeypatch):
    """G-04: a mid-stream LLM failure (after partial deltas) is NOT retried
    (the turn fails, by design — H-11). The partial delta must reach the
    delta_sink, and the node returns {error, done=True} — NOT
    needs_compaction (the failure isn't a context overflow)."""
    from langchain_core.messages import AIMessage, HumanMessage
    from worker.engine.events import EventEmitter
    from worker.engine.graph import agent_node
    from worker.engine.state import Budget, Mode, tag_message

    class MidStreamFailingLLM:
        """Yields one chunk (a partial delta) then raises mid-stream."""
        def astream(self, messages, stream_mode: str = "messages"):
            async def _gen():
                yield AIMessage(content="partial delta text")
                raise RuntimeError("mid-stream boom")
            return _gen()

    monkeypatch.setattr("worker.engine.graph.make_llm",
                        lambda *a, **k: MidStreamFailingLLM())
    # No tools needed for this failure-path test.
    monkeypatch.setattr("worker.engine.graph._bound_tools", lambda *a, **k: [])

    deltas: list[str] = []

    async def delta_sink(delta):
        deltas.append(delta.text)

    config = {"configurable": {
        "model": "kimi-foundry",
        "emitter": EventEmitter("run-1", "t-1"),
        "delta_sink": delta_sink,
        "thread_id": "t-1",
    }}
    state = {
        "run_id": "run-1", "thread_id": "t-1", "context_id": "t-1",
        "task_id": "task-1", "mode": Mode.DEVELOPMENT, "autonomy": "autonomous",
        "budget": Budget(cap=5.0),
        "messages": [tag_message(HumanMessage(content="do the work"), "user")],
        "done": False, "error": None,
        "approved_calls": {}, "denial_streak": 0, "tool_streak": {},
        "turn_count": 0, "compaction_count": 0, "compaction_retries": 0,
    }
    result = await agent_node(state, config)
    assert result.get("done") is True
    assert "mid-stream boom" in result.get("error", "")
    assert "needs_compaction" not in result
    assert deltas == ["partial delta text"]


@pytest.mark.asyncio
async def test_tools_node_rejects_invalid_mode_request_target():
    """G-05: a mode_request with an invalid target_mode is rejected at the
    graph level (the tools_node) — the agent sees an error ToolMessage
    naming the bad mode and the valid set, and the run's mode does NOT
    change. Covers the `target not in valid` branch in tools_node."""
    from langchain_core.messages import AIMessage, ToolMessage
    from worker.engine.events import EventEmitter
    from worker.engine.graph import tools_node
    from worker.engine.state import Mode, tag_message

    emitter = EventEmitter("run-1", "t-1")
    config = {"configurable": {"emitter": emitter, "thread_id": "t-1"}}
    ai = tag_message(AIMessage(
        content="",
        tool_calls=[{"id": "tc1", "name": "mode_request",
                      "args": {"target_mode": "bogus"}}],
    ), "assistant")
    state = {
        "run_id": "run-1", "thread_id": "t-1", "context_id": "t-1",
        "task_id": "task-1", "mode": Mode.DEVELOPMENT, "autonomy": "supervised",
        "messages": [ai],
        "approved_calls": {},  # no gate decision -> approved path
        "done": False,
    }
    out = await tools_node(state, config)
    new_msgs = out.get("messages", [])
    tool_msgs = [m for m in new_msgs if isinstance(m, ToolMessage)]
    assert tool_msgs, "expected a ToolMessage for the rejected mode_request"
    assert "unknown mode" in tool_msgs[-1].content
    assert "bogus" in tool_msgs[-1].content
    # The run's mode was NOT changed (no `mode` key in the node output).
    assert "mode" not in out


# --- SLO evaluation ---

def test_slo_passes_on_healthy_soak():
    result = SoakResult(
        turns=35, tool_calls=40, tool_calls_ok=40,
        first_delta_latency_s=2.0, turn_latencies=[10.0] * 35,
        events_emitted=100, events_lost=0, is_error=False, drift=0.0,
    )
    verdict = evaluate_slo(result)
    assert verdict["passed"] is True


def test_slo_fails_on_drift():
    result = SoakResult(
        turns=35, tool_calls=40, tool_calls_ok=35,
        first_delta_latency_s=2.0, turn_latencies=[10.0] * 35,
        events_emitted=100, events_lost=0, is_error=False, drift=-0.20,  # > -0.10 threshold
    )
    verdict = evaluate_slo(result)
    assert verdict["passed"] is False
    assert verdict["checks"]["no_drift"] is False


def test_slo_fails_on_event_loss():
    result = SoakResult(
        turns=35, tool_calls=40, tool_calls_ok=40,
        first_delta_latency_s=2.0, turn_latencies=[10.0] * 35,
        events_emitted=100, events_lost=2, is_error=False, drift=0.0,
    )
    verdict = evaluate_slo(result)
    assert verdict["passed"] is False
    assert verdict["checks"]["no_event_loss"] is False


def test_slo_fails_on_error():
    result = SoakResult(
        turns=35, tool_calls=40, tool_calls_ok=40,
        first_delta_latency_s=2.0, turn_latencies=[10.0] * 35,
        events_emitted=100, events_lost=0, is_error=True, drift=0.0,
    )
    verdict = evaluate_slo(result)
    assert verdict["passed"] is False
    assert verdict["checks"]["no_error"] is False


def test_slo_fails_on_short_soak():
    result = SoakResult(turns=15)  # < 30
    verdict = evaluate_slo(result)
    assert verdict["passed"] is False
    assert verdict["checks"]["turns_met"] is False


# --- StepEvent schema durability (contract enforced) ---

def test_step_event_round_trips_through_json():
    """A StepEvent survives JSON serialization (the events table stores it as JSON)."""
    e = StepEvent(
        run_id="r1", thread_id="t1", context_id="t1", task_id="task-1",
        seq=0, kind=StepKind.FILE_READ, title="read x.py",
        detail={"tool": "file_read", "ok": True},
    )
    blob = e.model_dump_json()
    restored = StepEvent.model_validate_json(blob)
    assert restored.run_id == "r1"
    assert restored.thread_id == "t1"
    assert restored.kind == StepKind.FILE_READ
    assert restored.schema_version == 1


def test_step_event_effective_context_id_defaults_to_thread():
    e = StepEvent(run_id="r1", thread_id="t1", seq=0, kind=StepKind.MESSAGE, title="x")
    assert e.effective_context_id() == "t1"
    e2 = StepEvent(run_id="r1", thread_id="t1", context_id="t1::worker-1",
                   seq=0, kind=StepKind.MESSAGE, title="x")
    assert e2.effective_context_id() == "t1::worker-1"
