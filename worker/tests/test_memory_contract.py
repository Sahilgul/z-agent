"""Contract tests for memory + compaction."""

from __future__ import annotations

from pathlib import Path

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from worker.engine.compaction import (
    CompactionPolicy,
    Compactor,
    SelfTuningLimit,
)
from worker.engine.memory import EpisodicMemory, set_episodic_memory
from worker.engine.state import PromptOrigin, tag_message


def _msg(content: str, origin: PromptOrigin) -> object:
    if origin == PromptOrigin.TOOL:
        m = ToolMessage(content=content, tool_call_id="fake-tc-id")
    else:
        cls = {"system": SystemMessage, "user": HumanMessage, "assistant": AIMessage,
               "nudge": HumanMessage, "memory": AIMessage, "envelope": HumanMessage}[origin]
        m = cls(content=content)
    return tag_message(m, origin)  # type: ignore[arg-type]


# ----------------------------------------------------------- Compaction: prune

def test_compaction_prunes_tool_outputs_first():
    policy = CompactionPolicy(context_limit=100, recent_window=2, floor_messages=1)
    compactor = Compactor(policy=policy)
    messages = [
        _msg("system", PromptOrigin.SYSTEM),
        _msg("user task", PromptOrigin.USER),
        _msg("big tool output 1" * 20, PromptOrigin.TOOL),
        _msg("big tool output 2" * 20, PromptOrigin.TOOL),
        _msg("assistant analysis", PromptOrigin.ASSISTANT),
        _msg("recent tool", PromptOrigin.TOOL),
        _msg("recent assistant", PromptOrigin.ASSISTANT),
    ]
    _new, result = compactor.compact(messages)
    assert result.pruned_count >= 2
    assert result.rolled_back is False


def test_compaction_never_prunes_protected_origins():
    """System, user, nudge are verbatim-protected — the honesty validator
    rolls back if any are dropped."""
    policy = CompactionPolicy(context_limit=50, recent_window=1, floor_messages=1)
    compactor = Compactor(policy=policy)
    messages = [
        _msg("system", PromptOrigin.SYSTEM),
        _msg("user 1", PromptOrigin.USER),
        _msg("tool output" * 20, PromptOrigin.TOOL),
        _msg("nudge", PromptOrigin.NUDGE),
        _msg("assistant", PromptOrigin.ASSISTANT),
        _msg("recent", PromptOrigin.ASSISTANT),
    ]
    new, result = compactor.compact(messages)
    # All protected messages must survive
    contents = [str(m.content) for m in new]
    assert "system" in contents
    assert "user 1" in contents
    assert "nudge" in contents
    assert result.rolled_back is False


def test_compaction_honesty_validator_rolls_back_on_protected_loss():
    """If the splice somehow dropped a protected message, roll back."""
    policy = CompactionPolicy(context_limit=10, recent_window=1, floor_messages=1)
    compactor = Compactor(policy=policy)
    # Force a scenario: many protected messages that can't all fit
    messages = [
        _msg(f"user {i}", PromptOrigin.USER) for i in range(30)
    ] + [_msg("recent", PromptOrigin.ASSISTANT)]
    new, result = compactor.compact(messages)
    # All user messages are protected; none should be dropped
    assert new is messages or result.rolled_back is True or len(new) >= 30


def test_compaction_no_op_when_under_limit():
    policy = CompactionPolicy(context_limit=1_000_000, recent_window=5, floor_messages=1)
    compactor = Compactor(policy=policy)
    messages = [_msg("x", PromptOrigin.ASSISTANT) for _ in range(10)]
    new, result = compactor.compact(messages)
    assert new is messages
    assert result.pruned_count == 0


def test_compaction_summary_carries_marker():
    policy = CompactionPolicy(context_limit=100, recent_window=2, floor_messages=1)
    summarizer = lambda text: "summary of pruned span"
    compactor = Compactor(policy=policy, summarizer=summarizer)
    messages = [
        _msg("system", PromptOrigin.SYSTEM),
        *[_msg(f"tool {i}" * 10, PromptOrigin.TOOL) for i in range(10)],
        _msg("recent", PromptOrigin.ASSISTANT),
        _msg("recent2", PromptOrigin.ASSISTANT),
    ]
    _new, result = compactor.compact(messages)
    assert result.summary.startswith("[compacted]")
    assert "summary of pruned span" in result.summary


# ----------------------------------------------------------- Self-tuning limit

def test_self_tuning_tightens_on_context_error():
    stl = SelfTuningLimit(initial=120_000, floor=32_000, step_down=8_000)
    stl.observe_error("context_length_exceeded")
    assert stl.current == 112_000
    stl.observe_error("some other error")  # not a context error
    assert stl.current == 112_000  # unchanged


def test_self_tuning_relaxes_after_healthy_turns():
    stl = SelfTuningLimit(initial=112_000, ceiling=200_000, step_up=4_000,
                          healthy_turns_threshold=3)
    for _ in range(3):
        stl.observe_healthy_turn()
    assert stl.current == 116_000  # relaxed


def test_self_tuning_respects_floor_and_ceiling():
    stl = SelfTuningLimit(initial=36_000, floor=32_000, step_down=8_000)
    stl.observe_error("context_length_exceeded")
    assert stl.current == 32_000  # floor
    stl2 = SelfTuningLimit(initial=198_000, ceiling=200_000, step_up=4_000,
                           healthy_turns_threshold=1)
    stl2.observe_healthy_turn()
    assert stl2.current == 200_000  # ceiling


# ----------------------------------------------------------- Episodic memory (T3 FTS)

def test_episodic_memory_record_and_search(tmp_path: Path):
    ep = EpisodicMemory(tmp_path / "ep.db")
    ep.record(run_id="r1", thread_id="t1", task_id="task-1", turn=0,
              kind="message", title="found the dedupe bug",
              summary="the scribe dedupes questions by hashing the question text")
    ep.record(run_id="r1", thread_id="t1", task_id="task-2", turn=1,
              kind="message", title="checked the DB schema",
              summary="the schema has a questions table with a dedupe_key column")
    results = ep.search("dedupe")
    assert len(results) >= 1
    titles = [r.get("title", "") for r in results]
    assert any("dedupe" in t.lower() for t in titles)
    ep.close()


def test_episodic_memory_search_scopes_by_thread(tmp_path: Path):
    ep = EpisodicMemory(tmp_path / "ep2.db")
    ep.record(run_id="r1", thread_id="t1", task_id="a", turn=0,
              kind="message", title="alpha finding", summary="alpha detail")
    ep.record(run_id="r1", thread_id="t2", task_id="b", turn=0,
              kind="message", title="alpha finding", summary="alpha detail")
    results = ep.search("alpha", thread_id="t1")
    assert len(results) == 1
    assert results[0]["thread_id"] == "t1"
    ep.close()


def test_episodic_memory_empty_query_returns_nothing(tmp_path: Path):
    ep = EpisodicMemory(tmp_path / "ep3.db")
    assert ep.search("") == []
    ep.close()


def test_set_episodic_memory_wires_tool(tmp_path: Path):
    ep = EpisodicMemory(tmp_path / "ep4.db")
    ep.record(run_id="r1", thread_id="t1", task_id="x", turn=0,
              kind="message", title="found it", summary="the answer is 42")
    set_episodic_memory(ep)
    from worker.engine.memory import memory_search
    result = memory_search.invoke({"query": "answer", "limit": 5})
    assert "42" in result
    ep.close()
