from zagent_contracts import ActionKind, RunStage

from app.db.models.run import Run
from app.services.runs import (
    ACTIONS_BY_STAGE, TERMINAL_STAGES, compute_available_actions, transition,
)


def test_compute_available_actions_per_stage():
    run = Run(id="r", created_by=1, mode="ask")
    for stage in RunStage:
        run.stage = stage.value
        actions = compute_available_actions(run)
        assert actions == ACTIONS_BY_STAGE.get(stage.value, [])


def test_compute_available_actions_awaiting_user():
    run = Run(id="r", created_by=1, mode="ask", stage="awaiting_user")
    assert set(compute_available_actions(run)) == {
        ActionKind.REVIEW_PLAN.value, ActionKind.APPROVE_PLAN.value, ActionKind.REJECT_PLAN.value,
    }


def test_compute_available_actions_verifying():
    run = Run(id="r", created_by=1, mode="ask", stage="verifying")
    assert set(compute_available_actions(run)) == {
        ActionKind.REVIEW_EVIDENCE.value, ActionKind.CREATE_PR.value,
    }


def test_compute_available_actions_pr_ready():
    run = Run(id="r", created_by=1, mode="ask", stage="pr_ready")
    assert set(compute_available_actions(run)) == {
        ActionKind.REVIEW_DIFF.value, ActionKind.MERGE_PR.value,
    }


def test_compute_available_actions_interrupted():
    run = Run(id="r", created_by=1, mode="ask", stage="interrupted")
    assert set(compute_available_actions(run)) == {
        ActionKind.EDIT_AND_RESEND.value, ActionKind.RESUME_RUN.value,
    }


def test_compute_available_actions_failed():
    run = Run(id="r", created_by=1, mode="ask", stage="failed")
    assert compute_available_actions(run) == [ActionKind.RESUME_RUN.value]


def test_transition_updates_stage_and_actions():
    run = Run(id="r", created_by=1, mode="ask", stage="queued", available_actions=["x"])
    transition(run, RunStage.AWAITING_USER)
    assert run.stage == "awaiting_user"
    assert ActionKind.APPROVE_PLAN.value in run.available_actions


def test_terminal_stages_set():
    assert TERMINAL_STAGES == {"completed", "failed", "abandoned"}
    for s in ("completed", "abandoned"):
        run = Run(id="r", created_by=1, mode="ask", stage=s)
        assert compute_available_actions(run) == []
    run = Run(id="r", created_by=1, mode="ask", stage="failed")
    assert compute_available_actions(run) == [ActionKind.RESUME_RUN.value]
