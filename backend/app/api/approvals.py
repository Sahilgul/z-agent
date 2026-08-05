"""Approval routes: pending cards for MY runs + decide. Decisions publish back
to the worker's blocking BLPOP (services/approvals.py).
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, field_validator

from app.core.deps import current_user
from app.db.base import get_session
from app.db.models.approval import Approval
from app.db.models.run import Run
from app.db.models.user import User

router = APIRouter(prefix="/approvals", tags=["approvals"])

# H-24: the worker's ApprovalBroker accepts exactly these decision strings
# (worker/worker/engine/approvals.py) plus the plan-approval verdicts
# (approved/rejected). The old `decision: str` accepted ANY string — it
# landed in the audit row and was replayed to the worker's blocking BLPOP,
# where the worker's own guard silently denied "unknown decision". Validate
# at the API boundary so a typo returns 422, not a silent deny or a 500.
_VALID_DECISIONS = {"allow", "allow_once", "always_allow", "deny", "deny_tool",
                    "approved", "rejected"}


class DecideBody(BaseModel):
    decision: str  # allow_once | always_allow | deny | deny_tool | approved | rejected
    reason: str = ""

    @field_validator("decision")
    @classmethod
    def _validate_decision(cls, v: str) -> str:
        if v not in _VALID_DECISIONS:
            raise ValueError(
                f"decision must be one of {sorted(_VALID_DECISIONS)}, got {v!r}")
        return v


@router.get("")
def pending(run_id: str | None = None, user: User = Depends(current_user)):
    """Pending cards for MY runs. `run_id` narrows to one session — the console
    docks approvals inside the open session, so it asks for that run only."""
    session = get_session()
    try:
        query = (
            session.query(Approval)
            .join(Run, Approval.run_id == Run.id)
            .filter(Run.created_by == user.id, Approval.decision.is_(None))
        )
        if run_id:
            query = query.filter(Approval.run_id == run_id)
        rows = query.order_by(Approval.created_at.desc()).all()
        # The sweep in services/approvals.py stamps decision=timeout, but it runs
        # on its own tick — never hand the console a card the worker has already
        # stopped waiting on.
        now = datetime.now(timezone.utc)
        return [{
            "id": a.id, "run_id": a.run_id, "thread_id": a.thread_id, "kind": a.kind,
            "payload": a.payload, "created_at": a.created_at.isoformat(),
            "expires_at": a.expires_at.isoformat() if a.expires_at else None,
        } for a in rows if not _expired(a, now)]
    finally:
        session.close()


def _expired(approval: Approval, now: datetime) -> bool:
    if approval.expires_at is None:
        return False
    expires = approval.expires_at
    if expires.tzinfo is None:  # SQLite hands back naive UTC
        expires = expires.replace(tzinfo=timezone.utc)
    return expires <= now


@router.post("/{approval_id}/decide")
async def decide(approval_id: str, body: DecideBody, request: Request,
                 user: User = Depends(current_user)):
    session = get_session()
    try:
        approval = session.get(Approval, approval_id)
        if approval is None:
            raise HTTPException(status_code=404, detail="approval not found")
        run = session.get(Run, approval.run_id)
        if run is None or run.created_by != user.id:
            raise HTTPException(status_code=404, detail="approval not found")
    finally:
        session.close()
    service = request.app.state.approval_service
    try:
        await service.decide(approval_id, body.decision, user.id, body.reason)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"ok": True}
