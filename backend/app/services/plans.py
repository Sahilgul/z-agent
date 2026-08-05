"""Plan HITL service: approve/reject a drafted Plan.

Pure DB state changes only — no blueprint execution. The API layer calls these
then asks run_manager to chain into the next blueprint (development on approve,
re-plan on reject). approve marks the Plan approved + every PlanStep pending so
the development blueprint picks them up; reject marks the Plan rejected and rolls
the run back to PLANNING for a fresh planner pass with the critic's notes.
"""

from __future__ import annotations

from datetime import datetime, timezone

from zagent_contracts import RunStage

from app.db.base import get_session
from app.db.models.run import Plan, Run
from app.services.runs import transition


def latest_plan(run_id: str) -> Plan | None:
    session = get_session()
    try:
        return (
            session.query(Plan)
            .filter_by(run_id=run_id)
            .order_by(Plan.created_at.desc(), Plan.id.desc())
            .first()
        )
    finally:
        session.close()


def critic_notes(plan: Plan) -> str:
    """Pull the most recent critic notes off a (rejected) Plan's structured
    payload so run_manager can inject them as a Lead nudge on re-plan."""
    notes = (plan.structured or {}).get("critic_notes") if plan else None
    if isinstance(notes, list) and notes:
        return str(notes[-1])
    if isinstance(notes, str) and notes:
        return notes
    return ""


def approve_plan(run_id: str, user_id: int) -> Plan:
    """Mark the latest Plan approved + all its steps pending, and stage the run
    for development. Raises ValueError when no draft plan exists."""
    session = get_session()
    try:
        plan = (
            session.query(Plan)
            .filter_by(run_id=run_id)
            .order_by(Plan.created_at.desc(), Plan.id.desc())
            .first()
        )
        if plan is None:
            raise ValueError("no plan to approve")
        if plan.status != "draft":
            # Defense-in-depth behind the stage gate: only a plan awaiting a
            # decision may be approved — never re-approve or approve a rejected one.
            raise ValueError(f"plan is '{plan.status}', not awaiting decision")
        plan.status = "approved"
        plan.decided_by = user_id
        plan.decided_at = datetime.now(timezone.utc)
        for step in plan.steps:
            step.status = "pending"
        run = session.get(Run, run_id)
        if run is not None:
            transition(run, RunStage.DEVELOPING)
        session.commit()
        session.refresh(plan)
        return plan
    finally:
        session.close()


def reject_plan(run_id: str, user_id: int, notes: str = "") -> Plan:
    """Mark the latest Plan rejected and roll the run back to PLANNING. The
    critic's notes are returned to the caller so run_manager can inject them as
    a Lead nudge on the re-plan pass."""
    session = get_session()
    try:
        plan = (
            session.query(Plan)
            .filter_by(run_id=run_id)
            .order_by(Plan.created_at.desc(), Plan.id.desc())
            .first()
        )
        if plan is None:
            raise ValueError("no plan to reject")
        if plan.status != "draft":
            raise ValueError(f"plan is '{plan.status}', not awaiting decision")
        plan.status = "rejected"
        plan.decided_by = user_id
        plan.decided_at = datetime.now(timezone.utc)
        if notes:
            structured = dict(plan.structured or {})
            # Normalize legacy shapes (a bare string from older rows) before
            # appending — critic_notes is ALWAYS a list on the way out (C1).
            existing = structured.get("critic_notes")
            if isinstance(existing, str):
                existing = [existing] if existing else []
            elif not isinstance(existing, list):
                existing = []
            existing.append(notes)
            structured["critic_notes"] = existing
            plan.structured = structured
        run = session.get(Run, run_id)
        if run is not None:
            transition(run, RunStage.PLANNING)
        session.commit()
        session.refresh(plan)
        return plan
    finally:
        session.close()
