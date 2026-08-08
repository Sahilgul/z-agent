"""Approval routes: pending cards for MY runs + decide. Decisions publish back
to the worker's blocking BLPOP (services/approvals.py).
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, field_validator

from app.core.deps import current_user
from app.core.timefmt import iso_z
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
# G1: edited_allow (with edited_args) was supported worker-side but
# unreachable from the API — dead vocabulary. Round-trips safely now.
_VALID_DECISIONS = {"allow", "allow_once", "always_allow", "edited_allow",
                    "deny", "deny_tool", "approved", "rejected"}


class DecideBody(BaseModel):
    decision: str  # allow_once | always_allow | edited_allow | deny | deny_tool | approved | rejected
    reason: str = ""
    edited_args: dict | None = None  # required payload for edited_allow

    @field_validator("edited_args")
    @classmethod
    def _validate_edited(cls, v, info):
        # G1: an edited_allow without edited_args would deny worker-side —
        # reject at the boundary instead.
        if info.data.get("decision") == "edited_allow" and not v:
            raise ValueError("edited_allow requires edited_args")
        return v

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
        now = datetime.now(UTC)
        return [{
            "id": a.id, "run_id": a.run_id, "thread_id": a.thread_id, "kind": a.kind,
            "payload": a.payload, "created_at": iso_z(a.created_at),
            "expires_at": iso_z(a.expires_at),
        } for a in rows if not _expired(a, now)]
    finally:
        session.close()


def _expired(approval: Approval, now: datetime) -> bool:
    if approval.expires_at is None:
        return False
    expires = approval.expires_at
    if expires.tzinfo is None:  # SQLite hands back naive UTC
        expires = expires.replace(tzinfo=UTC)
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
        decided = await service.decide(approval_id, body.decision, user.id,
                                       body.reason, body.edited_args)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    # G6: echo the recorded decision so a stale card's re-drive (idempotent
    # 200, or a "timeout" stamp) is visible to the caller.
    return {"ok": True, "decision": decided.decision}
