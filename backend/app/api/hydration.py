"""Hydration routes (plan §8/WU5): deterministic pre-run hydration — my ADO
tickets, fleet blast radius, title hydration, and the prewarm pool. All pure
code; the agent never self-reports any of this.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app.core.deps import current_user
from app.db.models.user import User
from app.services import hydration as hydration_service

router = APIRouter(prefix="/hydration", tags=["hydration"])


class PrewarmBody(BaseModel):
    repos: list[dict]


@router.get("/my-tickets")
async def my_tickets(request: Request, user: User = Depends(current_user)):
    """The user's ADO 'My active tickets' for the New-Run picker. 422 when the
    user hasn't bound their ADO identity (§1b) — the UI needs to distinguish
    'bind your account' from 'no active tickets', which an empty list can't say."""
    if not getattr(user, "ado_descriptor", None):
        raise HTTPException(status_code=422, detail="ADO identity not bound — link your account first")
    client = getattr(request.app.state, "ado_client", None) or hydration_service.AdoClient()
    return {"tickets": await hydration_service.my_tickets(user, ado_client=client)}


@router.get("/blast-radius")
def blast_radius(repo: str, _user: User = Depends(current_user)):
    """Layer 0 fleet-graph blast radius for a repo (the planner's scope hint).
    Degrades to [] when the graph is unavailable."""
    return {"repo": repo, "blast_radius": hydration_service.blast_radius(repo)}


@router.get("/title")
async def hydrate_title(request: Request, work_item_id: int | None = None, task: str = "",
                        user: User = Depends(current_user)):
    """Resolve a run title from an ADO work item (when the user taps a ticket),
    falling back to the typed task. The frontend uses this to pre-fill the title."""
    client = getattr(request.app.state, "ado_client", None) or hydration_service.AdoClient()
    title = await hydration_service.hydrate_title(work_item_id, task, ado_client=client)
    return {"work_item_id": work_item_id, "title": title}


@router.post("/prewarm")
async def prewarm(body: PrewarmBody, request: Request, user: User = Depends(current_user)):
    """Record desired prewarms (stub, WU5). The live pool lands with the VM move —
    semantics are defined in orchestrator/thread_manager.py (see /prewarm-status)."""
    pool = getattr(request.app.state, "prewarm_pool", None) or hydration_service.PrewarmPool()
    return await pool.prewarm(body.repos)


@router.get("/prewarm-status")
def prewarm_status(_user: User = Depends(current_user)):
    """Pool truth (plan §2): enabled=false until the live pool lands. The UI must
    render warmth from THIS, never from the record-intent endpoint above."""
    from app.orchestrator.thread_manager import prewarm_status as _status
    return _status()
