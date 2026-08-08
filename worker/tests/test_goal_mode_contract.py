"""Contract tests for goal mode core."""

from __future__ import annotations

import pytest

from worker.engine.fanout import set_current_thread_id
from worker.engine.goal_mode import (
    GoalArtifact,
    GoalStage,
    _reset_pending_questions,
    ask_user,
    build_clarify_card,
    clear_pending_questions,
    get_pending_questions,
    make_goal,
    needs_clarification,
)


@pytest.fixture(autouse=True)
def _clean_questions():
    _reset_pending_questions()
    yield
    _reset_pending_questions()


# --- intake ---

def test_make_goal_parses_user_story():
    g = make_goal("Add a health-check endpoint to the NestJS service",
                  repo="ServerApp", target_branch="main", budget_usd=15.0)
    assert g.artifact.user_story.startswith("Add a health-check")
    assert g.artifact.repo == "ServerApp"
    assert g.artifact.target_branch == "main"
    assert g.artifact.budget_usd == 15.0
    assert g.artifact.stage == GoalStage.INTAKE


# --- stage progression ---

def test_stage_graph_advances_in_order():
    g = make_goal("do the thing", repo="r")
    assert g.artifact.stage == GoalStage.INTAKE
    assert g.advance() == GoalStage.CLARIFY
    assert g.advance() == GoalStage.EXPLORE
    assert g.advance() == GoalStage.PLAN
    assert g.advance() == GoalStage.IMPLEMENT
    assert g.advance() == GoalStage.VERIFY
    assert g.advance() == GoalStage.REBASE_GATE
    assert g.advance() == GoalStage.PR
    assert g.advance() == GoalStage.DONE


def test_stage_graph_block_sets_blocked():
    g = make_goal("do the thing", repo="r")
    g.block("merge conflict on rebase")
    assert g.artifact.stage == GoalStage.BLOCKED
    assert g.artifact.blocked_reason == "merge conflict on rebase"


def test_blocked_stage_does_not_advance():
    g = make_goal("do the thing", repo="r")
    g.block("stuck")
    assert g.next_stage() == GoalStage.BLOCKED


# --- clarify heuristic ---

def test_needs_clarification_short_story():
    assert needs_clarification("fix it") is True


def test_needs_clarification_vague_pronoun():
    assert needs_clarification("fix the thing so it works better now") is True


def test_needs_clarification_specific_story():
    assert needs_clarification(
        "Add a GET /health endpoint to the NestJS service returning 200 + uptime"
    ) is False


def test_build_clarify_card_returns_2_to_4_questions():
    card = build_clarify_card("do the thing")
    assert 2 <= len(card) <= 4
    for q in card:
        assert q["prompt"]
        assert len(q["options"]) >= 2


# --- ask_user tool ---

def test_ask_user_accepts_valid_questions():
    qs = [
        {"id": "q1", "prompt": "What?", "options": [{"id": "a", "label": "A"}, {"id": "b", "label": "B"}]},
        {"id": "q2", "prompt": "Which?", "options": [{"id": "c", "label": "C"}, {"id": "d", "label": "D"}]},
    ]
    # H-10: ask_user stages under the per-run ContextVar thread_id (the
    # runner sets it in production; the test sets it explicitly so the
    # thread-keyed store can be read back under the same id).
    set_current_thread_id("test-thread")
    result = ask_user.invoke({"questions": qs})
    assert result.startswith("asked 2 questions")
    assert get_pending_questions("test-thread") == qs


def test_ask_user_rejects_too_few_questions():
    qs = [{"id": "q1", "prompt": "only one", "options": [{"id": "a", "label": "A"}, {"id": "b", "label": "B"}]}]
    set_current_thread_id("test-thread")
    result = ask_user.invoke({"questions": qs})
    assert "error" in result
    assert get_pending_questions("test-thread") is None


def test_ask_user_rejects_too_many_questions():
    qs = [{"id": f"q{i}", "prompt": f"q{i}", "options": [{"id": "a", "label": "A"}, {"id": "b", "label": "B"}]}
           for i in range(5)]
    result = ask_user.invoke({"questions": qs})
    assert "error" in result


def test_ask_user_stages_per_thread_no_cross_read():
    """H-10 (coord point A): two concurrent runs in one process must NOT
    cross-read each other's clarify questions. The old process-global scalar
    let run B's ask_user overwrite run A's staged questions before A's
    tools_node snapshotted them. The thread-keyed store isolates them: stage
    under thread A, then stage under thread B, then read A back and assert
    A is untouched by B's write (and clearing A does not touch B)."""
    qa = [{"id": "a1", "prompt": "A?", "options": [{"id": "a", "label": "A"}, {"id": "b", "label": "B"}]},
          {"id": "a2", "prompt": "A2?", "options": [{"id": "a", "label": "A"}, {"id": "b", "label": "B"}]}]
    qb = [{"id": "b1", "prompt": "B?", "options": [{"id": "a", "label": "A"}, {"id": "b", "label": "B"}]},
          {"id": "b2", "prompt": "B2?", "options": [{"id": "a", "label": "A"}, {"id": "b", "label": "B"}]}]
    set_current_thread_id("thread-A")
    assert ask_user.invoke({"questions": qa}).startswith("asked 2 questions")
    set_current_thread_id("thread-B")
    assert ask_user.invoke({"questions": qb}).startswith("asked 2 questions")
    # B's stage did NOT clobber A's entry (the old global would have).
    assert get_pending_questions("thread-A") == qa
    assert get_pending_questions("thread-B") == qb
    # Clearing A leaves B intact (the old clear() wiped the global for both).
    clear_pending_questions("thread-A")
    assert get_pending_questions("thread-A") is None
    assert get_pending_questions("thread-B") == qb


def test_ask_user_rejects_question_without_prompt():
    qs = [
        {"id": "q1", "prompt": "ok", "options": [{"id": "a", "label": "A"}, {"id": "b", "label": "B"}]},
        {"id": "q2", "prompt": "", "options": [{"id": "a", "label": "A"}, {"id": "b", "label": "B"}]},
    ]
    result = ask_user.invoke({"questions": qs})
    assert "missing prompt" in result


def test_ask_user_rejects_question_with_one_option():
    qs = [
        {"id": "q1", "prompt": "ok", "options": [{"id": "a", "label": "A"}]},
        {"id": "q2", "prompt": "ok2", "options": [{"id": "a", "label": "A"}, {"id": "b", "label": "B"}]},
    ]
    result = ask_user.invoke({"questions": qs})
    assert "needs >= 2 options" in result


# --- goal artifact persistence ---

def test_goal_artifact_serializes():
    a = GoalArtifact(goal_id="g1", user_story="test", repo="r")
    blob = a.model_dump_json()
    restored = GoalArtifact.model_validate_json(blob)
    assert restored.goal_id == "g1"
    assert restored.user_story == "test"
    assert restored.schema_version == 1
