"""Approval routes: pending cards for MY runs + decide. Decisions publish back
to the worker's blocking BLPOP (services/approvals.py).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app.core.deps import current_user
from app.db.base import get_session
from app.db.models.approval import Approval
from app.db.models.run import Run
from app.db.models.user import User

router = APIRouter(prefix="/approvals", tags=["approvals"])


class DecideBody(BaseModel):
    decision: str  # allow_once | always_allow | deny | approved | rejected
    reason: str = ""


@router.get("")
def pending(user: User = Depends(current_user)):
    session = get_session()
    try:
        rows = (
            session.query(Approval)
            .join(Run, Approval.run_id == Run.id)
            .filter(Run.created_by == user.id, Approval.decision.is_(None))
            .order_by(Approval.created_at.desc())
            .all()
        )
        return [{
            "id": a.id, "run_id": a.run_id, "lane_id": a.lane_id, "kind": a.kind,
            "payload": a.payload, "created_at": a.created_at.isoformat(),
        } for a in rows]
    finally:
        session.close()


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
