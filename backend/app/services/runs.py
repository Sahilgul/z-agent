"""Run state machine (plan §1a): stage -> available_actions, computed by the
orchestrator, rendered as-is by the UI. Pure Python — NO FastAPI imports.
"""

from __future__ import annotations

from zagent_contracts import ActionKind, RunStage

from app.db.models.run import Run

# The ONE control always visible (Stop) is hardcoded by the UI — never in this list.
ACTIONS_BY_STAGE: dict[str, list[str]] = {
    RunStage.QUEUED.value: [],
    RunStage.PROVISIONING.value: [],
    RunStage.INVESTIGATING.value: [],
    RunStage.PLANNING.value: [],
    RunStage.AWAITING_USER.value: [
        ActionKind.REVIEW_PLAN.value, ActionKind.APPROVE_PLAN.value, ActionKind.REJECT_PLAN.value,
    ],
    RunStage.DEVELOPING.value: [],
    RunStage.VERIFYING.value: [ActionKind.REVIEW_EVIDENCE.value, ActionKind.CREATE_PR.value],
    RunStage.PR_READY.value: [ActionKind.REVIEW_DIFF.value, ActionKind.MERGE_PR.value],
    # From interrupted, exactly two resume paths (§1a): edit-last-and-resend, send-new-message.
    RunStage.INTERRUPTED.value: [ActionKind.EDIT_AND_RESEND.value, ActionKind.RESUME_RUN.value],
    RunStage.COMPLETED.value: [],
    RunStage.FAILED.value: [ActionKind.RESUME_RUN.value],
    RunStage.ABANDONED.value: [],
}

TERMINAL_STAGES = {RunStage.COMPLETED.value, RunStage.FAILED.value, RunStage.ABANDONED.value}


def compute_available_actions(run: Run) -> list[str]:
    return ACTIONS_BY_STAGE.get(run.stage, [])


def transition(run: Run, stage: RunStage) -> Run:
    run.stage = stage.value
    run.available_actions = compute_available_actions(run)
    return run
