"""Campaign mode + delivery rollups + cost dashboard."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.core.deps import current_user
from app.db.models.user import User
from app.services import campaigns, stats

router = APIRouter(tags=["campaigns"])


class CampaignBody(BaseModel):
    task: str = Field(min_length=4)
    repos: list[str] | None = None  # None = every ready repo
    title: str = Field(default="", max_length=256)


@router.post("/campaigns", status_code=201)
async def launch(body: CampaignBody, request: Request, user: User = Depends(current_user)):
    try:
        return await campaigns.launch(body.task, body.repos, user.id,
                                      request.app.state.run_manager, title=body.title)
    except campaigns.CampaignError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/deliveries")
def deliveries(user: User = Depends(current_user)):
    """Fleet rollup — metadata only (stages, counts, costs, PR statuses)."""
    return {"items": campaigns.list_deliveries()}


@router.get("/stats/cost")
def cost(days: int = 30, user: User = Depends(current_user)):
    return stats.cost_dashboard(days)
