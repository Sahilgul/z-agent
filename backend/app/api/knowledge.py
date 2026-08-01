"""Knowledge routes (plan §3/§7): corpus browsing (shared + own), the team-wide
draft inbox (the PHI checkpoint), and approve/reject decisions. The corpus and
its approval cards are the §7a shared-by-design exception — no per-user hiding.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.core.deps import current_user
from app.db.models.user import User
from app.services import knowledge

router = APIRouter(prefix="/knowledge", tags=["knowledge"])


class DraftBody(BaseModel):
    content: str
    trigger_description: str = ""
    proposed_scope: str = "global"
    repo: str | None = None
    source_run_id: str | None = None


class ApproveBody(BaseModel):
    scope: str
    repo: str | None = None


@router.get("")
def corpus(user: User = Depends(current_user)):
    return knowledge.corpus_for(user.id)


@router.get("/pending")
def pending_drafts(user: User = Depends(current_user)):
    return knowledge.pending()


@router.post("", status_code=201)
def create_draft(body: DraftBody, user: User = Depends(current_user)):
    try:
        item = knowledge.draft(
            content=body.content, trigger_description=body.trigger_description,
            created_by=user.id, repo=body.repo, source_run_id=body.source_run_id,
            proposed_scope=body.proposed_scope,
        )
    except knowledge.KnowledgeError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    # The PHI checkpoint is visible in the response: a draft is NEVER born
    # into the shared corpus, whatever scope was proposed.
    return knowledge._serialize(item) | {"phi_checkpoint": "draft stored scope=user until approved"}


@router.post("/{item_id}/approve")
def approve_item(item_id: int, body: ApproveBody, user: User = Depends(current_user)):
    try:
        return knowledge.approve(item_id, body.scope, user.id, repo=body.repo)
    except knowledge.KnowledgeError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/{item_id}/reject")
def reject_item(item_id: int, user: User = Depends(current_user)):
    try:
        return knowledge.reject(item_id, user.id)
    except knowledge.KnowledgeError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
