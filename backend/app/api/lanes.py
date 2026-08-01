"""Lane control routes: stop/nudge/pin on tiles (control actions, NOT chats —
the user talks only to the Lead; every intervention becomes an event the Lead
consumes, plan §4).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app.core.deps import current_user
from app.db.models.user import User
from app.services.intents import load_run_for_user

router = APIRouter(prefix="/lanes", tags=["lanes"])


class NudgeBody(BaseModel):
    run_id: str
    text: str


class LaneActionBody(BaseModel):
    run_id: str


@router.post("/{lane_id}/nudge")
async def nudge(lane_id: str, body: NudgeBody, request: Request,
                user: User = Depends(current_user)):
    if load_run_for_user(body.run_id, user.id) is None:
        raise HTTPException(status_code=404, detail="run not found")
    await request.app.state.run_manager.nudge_lane(body.run_id, lane_id, body.text)
    return {"ok": True}


@router.post("/{lane_id}/stop")
async def stop_lane(lane_id: str, body: LaneActionBody, request: Request,
                    user: User = Depends(current_user)):
    if load_run_for_user(body.run_id, user.id) is None:
        raise HTTPException(status_code=404, detail="run not found")
    await request.app.state.control.interrupt(lane_id)
    return {"ok": True}


@router.post("/{lane_id}/pin")
async def pin_finding(lane_id: str, body: LaneActionBody, request: Request,
                      user: User = Depends(current_user)):
    """Pin = flag the lane's notebook-so-far for the Lead's synthesis. Phase 3
    wires this into the knowledge inbox; Phase 1 records the intent as an event."""
    if load_run_for_user(body.run_id, user.id) is None:
        raise HTTPException(status_code=404, detail="run not found")
    relay = request.app.state.relay
    await relay.publish_lane_status(body.run_id, lane_id, "pinned")
    return {"ok": True}
