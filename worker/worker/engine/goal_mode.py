"""Goal mode core — the long-horizon stage subgraph.

Goal mode is a meta-mode: user story → PR, fully autonomous after an optional
clarify step. It composes the existing modes (ask, plan, development) into a
stage subgraph with persistent objective state. NO human approval gates
inside the pipeline: human interaction is ONLY the
initial clarify (if needed) or blocked-escalation. The pipeline runs
autonomously until PR creation.

Stages:
  intake    — parse the user story into a GoalArtifact (objective, success criteria, repo).
  clarify?  — if the story is ambiguous, ask_user (2-4 questions, free-text card). Otherwise skip.
  explore   — read-only investigation (ask mode) to map the change surface.
  plan      — produce a Plan + a task tracker (the two-artifact todo-plan).
  implement — execute the plan (development mode) with the task tracker.
  verify    — run tests / checks; collect evidence.
  rebase-gate — ensure the branch is rebased on the target; resolve conflicts or escalate.
  pr       — create the PR; the goal is done.

The goal budget is a separate envelope from the thread budget:
the goal budget is the SUM of all threads spawned for the goal.

The stage graph ships WITHOUT critics (a later layer adds critic×3 +
blocked-escalation). The ask_user tool and QuestionCard are here.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Any

from langchain_core.tools import tool
from pydantic import BaseModel, Field

# --- Goal artifact (the persistent objective state) ---

class GoalStage(str, Enum):
    INTAKE = "intake"
    CLARIFY = "clarify"
    EXPLORE = "explore"
    PLAN = "plan"
    IMPLEMENT = "implement"
    VERIFY = "verify"
    REBASE_GATE = "rebase_gate"
    PR = "pr"
    DONE = "done"
    BLOCKED = "blocked"


class QuestionOption(BaseModel):
    id: str
    label: str


class QuestionCard(BaseModel):
    """The ask_user card — 2-4 questions, free-text allowed."""
    questions: list[dict[str, Any]] = Field(
        description="Each: {id, prompt, options: [{id, label}], allow_multiple?}"
    )


class GoalArtifact(BaseModel):
    """The frozen objective + live state. Persists across the whole pipeline."""
    schema_version: int = 1
    goal_id: str
    user_story: str
    objective: str = ""
    success_criteria: list[str] = Field(default_factory=list)
    repo: str | None = None
    target_branch: str = "main"
    stage: GoalStage = GoalStage.INTAKE
    clarify_questions: list[dict[str, Any]] | None = None
    plan_artifact: dict[str, Any] | None = None
    task_tracker: list[dict[str, Any]] | None = None
    pr_url: str | None = None
    blocked_reason: str | None = None
    budget_usd: float = 20.0
    cost_usd: float = 0.0


# --- ask_user tool (the only human-interaction surface in goal mode) ---

# H-10 (coord point A): pending clarify questions are a transport from the
# ask_user tool (run in an executor thread) to the graph's tools_node (run
# in the coroutine). A process-global SCALAR here let two concurrent runs
# in one process cross-read each other's questions — run A's ask_user and
# run B's ask_user both wrote the same global, so tools_node could snapshot
# run B's questions into run A's state (and clear run A's before it read
# them). Key the store by thread_id so each run's clarify signal is
# isolated. ask_user reads its thread_id from the per-run ContextVar the
# runner sets (fanout._current_thread_id), which call_tool_direct
# propagates into the executor via contextvars.copy_context(); tools_node
# reads the same id from state["thread_id"]. The runner sets the ContextVar
# to self.thread_id, so the two ids always agree.
_pending_questions: dict[str, list[dict[str, Any]] | None] = {}


def set_pending_questions(thread_id: str, qs: list[dict[str, Any]] | None) -> None:
    _pending_questions[thread_id] = qs


@tool
def ask_user(questions: list[dict[str, Any]]) -> str:
    """Ask the human 2-4 clarifying questions. Each question is:
    {id, prompt, options: [{id, label}], allow_multiple?}. Free-text is
    always allowed (the human can type instead of picking). This is the ONLY
    human-interaction surface in goal mode — after this, the pipeline runs
    autonomously to PR creation.
    """
    if not (2 <= len(questions) <= 4):
        return "error: ask_user requires 2-4 questions"
    for q in questions:
        if not q.get("prompt"):
            return f"error: question {q.get('id', '?')} missing prompt"
        opts = q.get("options", [])
        if len(opts) < 2:
            return f"error: question {q.get('id', '?')} needs >= 2 options"
    # H-10: stage under THIS run's thread_id (ContextVar, set by the runner
    # and propagated into the executor by call_tool_direct's copy_context),
    # not a process-global shared across concurrent runs.
    from worker.engine.fanout import _current_thread_id
    set_pending_questions(_current_thread_id(), questions)
    return f"asked {len(questions)} questions; pipeline paused for clarification"


def get_pending_questions(thread_id: str) -> list[dict[str, Any]] | None:
    return _pending_questions.get(thread_id)


def clear_pending_questions(thread_id: str) -> None:
    _pending_questions.pop(thread_id, None)


def _reset_pending_questions() -> None:
    """Test-only: clear every thread's staged questions (cross-test isolation)."""
    _pending_questions.clear()


# --- Stage graph (the core, no critics) ---

@dataclass
class GoalGraph:
    """The stage subgraph. Linear pipeline, no critic loop.

    A later layer wraps implement/verify with critic×3 + blocked-escalation.
    """
    artifact: GoalArtifact

    def next_stage(self) -> GoalStage:
        order = [
            GoalStage.INTAKE, GoalStage.CLARIFY, GoalStage.EXPLORE,
            GoalStage.PLAN, GoalStage.IMPLEMENT, GoalStage.VERIFY,
            GoalStage.REBASE_GATE, GoalStage.PR, GoalStage.DONE,
        ]
        try:
            i = order.index(self.artifact.stage)
        except ValueError:
            return GoalStage.BLOCKED
        return order[min(i + 1, len(order) - 1)]

    def advance(self) -> GoalStage:
        nxt = self.next_stage()
        self.artifact.stage = nxt
        return nxt

    def block(self, reason: str) -> None:
        self.artifact.stage = GoalStage.BLOCKED
        self.artifact.blocked_reason = reason


def make_goal(user_story: str, *, repo: str | None = None,
               target_branch: str = "main", budget_usd: float = 20.0) -> GoalGraph:
    """Intake: parse the user story into a GoalArtifact."""
    artifact = GoalArtifact(
        goal_id=str(uuid.uuid4()),
        user_story=user_story,
        repo=repo,
        target_branch=target_branch,
        budget_usd=budget_usd,
    )
    return GoalGraph(artifact=artifact)


def needs_clarification(user_story: str) -> bool:
    """Heuristic: does the story need clarification before autonomous work?

    Uses a simple heuristic (missing repo, missing success signal,
    pronouns like 'it'/'that' without referent). A later layer may add an LLM judge.
    """
    if not user_story or len(user_story.strip()) < 20:
        return True
    vague = (" it ", " that ", " this ", "the thing", "you know")
    return any(v in user_story.lower() for v in vague)


def build_clarify_card(user_story: str) -> list[dict[str, Any]]:
    """Build the 2-4 clarifying questions for an ambiguous story."""
    return [
        {
            "id": "objective",
            "prompt": "What is the specific outcome you want?",
            "options": [
                {"id": "bugfix", "label": "Fix a bug"},
                {"id": "feature", "label": "Add a feature"},
                {"id": "refactor", "label": "Refactor / clean up"},
            ],
        },
        {
            "id": "repo",
            "prompt": "Which repository is the target?",
            "options": [
                {"id": "current", "label": "The current repo"},
                {"id": "other", "label": "A different repo (I'll specify)"},
            ],
        },
    ]


# --- Stage envelopes (injected per-turn by the graph's goal router) ---
#
# Each stage gets a short synthetic-reminder envelope (per-turn
# fragments, never part of the system message). The goal router advances the
# artifact; the agent node renders the current stage's envelope transiently.

STAGE_ENVELOPES: dict[GoalStage, str] = {
    GoalStage.INTAKE: (
        "<goal-stage>intake</goal-stage>\n"
        "Parse the user story into a concrete objective and success criteria. "
        "If anything material is ambiguous, use ask_user now — this is the ONLY "
        "clarification point; after it, the pipeline runs autonomously to PR."
    ),
    GoalStage.CLARIFY: (
        "<goal-stage>clarify</goal-stage>\n"
        "Call ask_user with 2-4 questions (2-5 options each, free-text always "
        "allowed). The pipeline pauses until the human answers. Do NOT start "
        "exploring before the answers arrive."
    ),
    GoalStage.EXPLORE: (
        "<goal-stage>explore</goal-stage>\n"
        "Read-only investigation. Map the change surface: which files, which "
        "modules, which tests. No writes in this stage. End with a concise "
        "summary of what you found; the pipeline then moves to planning."
    ),
    GoalStage.PLAN: (
        "<goal-stage>plan</goal-stage>\n"
        "Produce the plan as a structured task list (update_tasks) — frozen "
        "plan artifact plus a live tracker. No code edits in this stage. "
        "A critic reviews the plan before implementation; blocking findings "
        "come back to you for revision."
    ),
    GoalStage.IMPLEMENT: (
        "<goal-stage>implement</goal-stage>\n"
        "Execute the plan against the task tracker, one item at a time. Mark "
        "each item completed as soon as it is done. Read-before-edit is "
        "enforced. Stay inside the plan's scope."
    ),
    GoalStage.VERIFY: (
        "<goal-stage>verify</goal-stage>\n"
        "Run the tests and checks that prove the success criteria. Collect "
        "the evidence. If verification fails, fix within scope and re-verify; "
        "repeated failure escalates to blocked — never claim unverified work."
    ),
    GoalStage.REBASE_GATE: (
        "<goal-stage>rebase-gate</goal-stage>\n"
        "Ensure the branch is fresh against the target. Rebase and resolve "
        "conflicts you own; if a conflict is unresolvable (semantic, another "
        "thread's work), escalate to blocked with a precise explanation."
    ),
    GoalStage.PR: (
        "<goal-stage>pr</goal-stage>\n"
        "Summarize the work: what changed, why, and how it was verified. The "
        "platform opens the PR from this summary. This is the last stage."
    ),
}

# Stages where the agent's turn-end advances the pipeline (goal router rules).
ADVANCE_ON_TURN_END: dict[GoalStage, GoalStage] = {
    GoalStage.EXPLORE: GoalStage.PLAN,
    GoalStage.IMPLEMENT: GoalStage.VERIFY,
    GoalStage.REBASE_GATE: GoalStage.PR,
}


def stage_of(goal_artifact: dict[str, Any]) -> GoalStage:
    """Read the current stage from a checkpointed goal artifact dict."""
    raw = goal_artifact.get("stage", GoalStage.INTAKE.value)
    try:
        return GoalStage(raw)
    except ValueError:
        return GoalStage.BLOCKED


def advance_artifact(goal_artifact: dict[str, Any], next_stage: GoalStage) -> dict[str, Any]:
    """Return a NEW artifact dict with the stage advanced (checkpoint-safe)."""
    return {**goal_artifact, "stage": next_stage.value}


def block_artifact(goal_artifact: dict[str, Any], reason: str) -> dict[str, Any]:
    """Return a NEW artifact dict routed to blocked-escalation."""
    return {**goal_artifact, "stage": GoalStage.BLOCKED.value, "blocked_reason": reason}


__all__ = [
    "ADVANCE_ON_TURN_END",
    "STAGE_ENVELOPES",
    "GoalArtifact",
    "GoalGraph",
    "GoalStage",
    "QuestionCard",
    "QuestionOption",
    "advance_artifact",
    "ask_user",
    "block_artifact",
    "build_clarify_card",
    "clear_pending_questions",
    "get_pending_questions",
    "make_goal",
    "needs_clarification",
    "stage_of",
]