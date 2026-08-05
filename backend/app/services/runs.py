"""Run state machine: stage -> available_actions, computed by the
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
    # From interrupted, exactly two resume paths: edit-last-and-resend, send-new-message.
    RunStage.INTERRUPTED.value: [ActionKind.EDIT_AND_RESEND.value, ActionKind.RESUME_RUN.value],
    RunStage.COMPLETED.value: [],
    RunStage.FAILED.value: [ActionKind.RESUME_RUN.value],
    RunStage.ABANDONED.value: [],
}

TERMINAL_STAGES = {RunStage.COMPLETED.value, RunStage.FAILED.value, RunStage.ABANDONED.value}


def compute_available_actions(run: Run) -> list[str]:
    return ACTIONS_BY_STAGE.get(run.stage, [])


def transition(run: Run, stage: RunStage, *, allow_terminal_exit: bool = False) -> Run:
    # H-41: a terminal run (COMPLETED/FAILED/ABANDONED) must not be
    # re-transitioned by stop/abandon/kill_replace — the old code had no
    # guard, so a stop on an already-COMPLETED run resurrected it to
    # INTERRUPTED. Resume is the only legitimate exit from a terminal
    # state, so it passes allow_terminal_exit=True.
    if run.stage in TERMINAL_STAGES and not allow_terminal_exit:
        raise ValueError(
            f"cannot transition terminal run ({run.stage}) -> {stage.value}")
    run.stage = stage.value
    run.available_actions = compute_available_actions(run)
    return run
