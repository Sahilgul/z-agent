"""Thread control routes: stop/nudge/pin on tiles (control actions, NOT chats —
the user talks only to the Lead; every intervention becomes an event the Lead
consumes).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app.core.deps import current_user
from app.db.models.user import User
from app.services.intents import load_run_for_user, load_thread_for_run

router = APIRouter(prefix="/threads", tags=["threads"])


class NudgeBody(BaseModel):
    run_id: str
    text: str


class LaneActionBody(BaseModel):
    run_id: str
    note: str = ""


@router.post("/{thread_id}/nudge")
async def nudge(thread_id: str, body: NudgeBody, request: Request,
                user: User = Depends(current_user)):
    if load_run_for_user(body.run_id, user.id) is None:
        raise HTTPException(status_code=404, detail="run not found")
    # IDOR guard (C-08): the thread must belong to this run, otherwise a
    # caller could nudge ANY user's thread by pairing their own run_id with
    # another run's thread_id.
    if load_thread_for_run(body.run_id, thread_id) is None:
        raise HTTPException(status_code=404, detail="thread not found")
    await request.app.state.run_manager.nudge_thread(body.run_id, thread_id, body.text)
    return {"ok": True}


@router.post("/{thread_id}/stop")
async def stop_thread(thread_id: str, body: LaneActionBody, request: Request,
                    user: User = Depends(current_user)):
    if load_run_for_user(body.run_id, user.id) is None:
        raise HTTPException(status_code=404, detail="run not found")
    # IDOR guard (C-09): same ownership check as nudge — stop must not
    # interrupt another user's thread.
    if load_thread_for_run(body.run_id, thread_id) is None:
        raise HTTPException(status_code=404, detail="thread not found")
    # C12: route through run_manager.stop_thread so EVERY stop path performs
    # the same bookkeeping (verified interrupt, DB stamp, relay, key
    # release) — the old route only fired a raw interrupt and left the row
    # at "running", leaking the capacity slot and the gateway key.
    await request.app.state.run_manager.stop_thread(body.run_id, thread_id)
    return {"ok": True}


@router.post("/{thread_id}/pin")
async def pin_finding(thread_id: str, body: LaneActionBody, request: Request,
                      user: User = Depends(current_user)):
    """Pin = flag the thread's notebook-so-far for the Lead's synthesis. Later
    phases wire this into the knowledge inbox; for now records the intent as an event."""
    if load_run_for_user(body.run_id, user.id) is None:
        raise HTTPException(status_code=404, detail="run not found")
    # IDOR guard (H-21): like /stop, the pin must not flag another run's
    # thread — the old code only checked run ownership, so a user could
    # publish a spoofed "pinned" status for an arbitrary thread_id.
    if load_thread_for_run(body.run_id, thread_id) is None:
        raise HTTPException(status_code=404, detail="thread not found")
    # W5-L2: route through run_manager.pin_finding so the pin is a durable
    # run event (this route used to publish ONLY the cosmetic "pinned"
    # status flash, which the next lanes poll reverted).
    await request.app.state.run_manager.pin_finding(body.run_id, thread_id, note=body.note)
    return {"ok": True}
