"""Improvement Inbox API (plan §7): team-wide ranked proposals, accept →
Development run, dismiss → flywheel preference signal."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.core.deps import current_user
from app.db.models.user import User
from app.services import proposals

router = APIRouter(prefix="/proposals", tags=["proposals"])


class DismissBody(BaseModel):
    reason: str = Field(default="", max_length=500)


@router.get("")
def inbox(status: str | None = "proposed", user: User = Depends(current_user)):
    return {"items": proposals.inbox(status)}


@router.post("/{proposal_id}/accept", status_code=201)
async def accept(proposal_id: int, request: Request, user: User = Depends(current_user)):
    try:
        return await proposals.accept(proposal_id, user.id, request.app.state.run_manager)
    except proposals.ProposalError as exc:
        status = 404 if "not found" in str(exc) else 422
        raise HTTPException(status_code=status, detail=str(exc)) from exc


@router.post("/{proposal_id}/dismiss")
def dismiss(proposal_id: int, body: DismissBody, user: User = Depends(current_user)):
    try:
        return proposals.dismiss(proposal_id, user.id, body.reason)
    except proposals.ProposalError as exc:
        status = 404 if "not found" in str(exc) else 422
        raise HTTPException(status_code=status, detail=str(exc)) from exc
