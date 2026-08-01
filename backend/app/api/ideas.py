"""Ideas routes (plan §6): team-wide threads + comments, Ask Counsel, Lead
summarize, Promote to Plan. No per-user scoping — the Ideas space is the §7a
shared-by-design exception.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app.core.deps import current_user
from app.db.models.user import User
from app.services import ideas

router = APIRouter(prefix="/ideas", tags=["ideas"])


class ThreadBody(BaseModel):
    title: str
    body: str = ""


class CommentBody(BaseModel):
    body: str


@router.get("")
def threads(status: str | None = None, user: User = Depends(current_user)):
    return ideas.list_threads(status=status)


@router.post("", status_code=201)
def create(body: ThreadBody, user: User = Depends(current_user)):
    return ideas.create_thread(body.title, body.body, user.id)


@router.get("/{thread_id}")
def detail(thread_id: int, user: User = Depends(current_user)):
    try:
        return ideas.get_thread(thread_id)
    except ideas.IdeasError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/{thread_id}/comments", status_code=201)
def add_comment(thread_id: int, body: CommentBody, user: User = Depends(current_user)):
    try:
        return ideas.comment(thread_id, "user", str(user.id), body.body)
    except ideas.IdeasError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/{thread_id}/ask-counsel", status_code=201)
async def ask_counsel(thread_id: int, user: User = Depends(current_user)):
    try:
        return await ideas.ask_counsel(thread_id)
    except ideas.IdeasError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/{thread_id}/summarize")
async def summarize(thread_id: int, user: User = Depends(current_user)):
    try:
        return await ideas.summarize(thread_id)
    except ideas.IdeasError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/{thread_id}/promote", status_code=201)
async def promote(thread_id: int, request: Request, user: User = Depends(current_user)):
    """Carry the thread into a plan-mode run: the Lead synthesis + all voices
    become the task brief; the run id is pinned back on the thread."""
    try:
        task = ideas.plan_task_for(thread_id)
    except ideas.IdeasError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    run_manager = request.app.state.run_manager
    run = await run_manager.create_run(
        source="button", initiated_by=user.id, mode_name="plan", task=task)
    return ideas.mark_promoted(thread_id, run.id)
