"""Thread control routes: stop/nudge/pin on tiles (control actions, NOT chats —
the user talks only to the Lead; every intervention becomes an event the Lead
consumes, plan §4).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app.core.deps import current_user
from app.db.models.user import User
from app.services.intents import load_run_for_user

router = APIRouter(prefix="/threads", tags=["threads"])


class NudgeBody(BaseModel):
    run_id: str
    text: str


class LaneActionBody(BaseModel):
    run_id: str


@router.post("/{thread_id}/nudge")
async def nudge(thread_id: str, body: NudgeBody, request: Request,
                user: User = Depends(current_user)):
    if load_run_for_user(body.run_id, user.id) is None:
        raise HTTPException(status_code=404, detail="run not found")
    await request.app.state.run_manager.nudge_thread(body.run_id, thread_id, body.text)
    return {"ok": True}


@router.post("/{thread_id}/stop")
async def stop_thread(thread_id: str, body: LaneActionBody, request: Request,
                    user: User = Depends(current_user)):
    if load_run_for_user(body.run_id, user.id) is None:
        raise HTTPException(status_code=404, detail="run not found")
    await request.app.state.control.interrupt(thread_id)
    return {"ok": True}


@router.post("/{thread_id}/pin")
async def pin_finding(thread_id: str, body: LaneActionBody, request: Request,
                      user: User = Depends(current_user)):
    """Pin = flag the thread's notebook-so-far for the Lead's synthesis. Phase 3
    wires this into the knowledge inbox; Phase 1 records the intent as an event."""
    if load_run_for_user(body.run_id, user.id) is None:
        raise HTTPException(status_code=404, detail="run not found")
    relay = request.app.state.relay
    await relay.publish_thread_status(body.run_id, thread_id, "pinned")
    return {"ok": True}
