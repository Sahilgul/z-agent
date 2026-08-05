"""fleet-bench API (plan §9 Phase 5): case mining + eval runs are admin-gated
(they spend money); reports are team-visible — the compounding proof belongs
to everyone."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.core.deps import admin_user, current_user
from app.db.models.user import User
from app.services import bench

router = APIRouter(prefix="/bench", tags=["bench"])


class CaseBody(BaseModel):
    repo: str = Field(min_length=1, max_length=128)
    title: str = Field(min_length=1, max_length=256)
    task_text: str = Field(min_length=1)
    base_commit: str = Field(min_length=1, max_length=64)
    fail_to_pass: list[str] = Field(min_length=1)
    pass_to_pass: list[str] = Field(default_factory=list)
    work_item_id: int | None = None
    held_out: bool = False


class ResultBody(BaseModel):
    outcomes: dict[str, bool]


@router.get("/cases")
def cases(held_out: bool | None = None, user: User = Depends(current_user)):
    return {"items": bench.list_cases(held_out)}


@router.post("/cases", status_code=201)
def create_case(body: CaseBody, _: User = Depends(admin_user)):
    try:
        return bench.create_case(**body.model_dump())
    except bench.BenchError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/cases/{case_id}/run", status_code=201)
async def run_case(case_id: int, request: Request, _: User = Depends(admin_user)):
    try:
        return await bench.start_eval(case_id, request.app.state.run_manager)
    except bench.BenchError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/evals/{eval_id}/result")
def record_result(eval_id: int, body: ResultBody, _: User = Depends(admin_user)):
    try:
        return bench.record_result(eval_id, body.outcomes)
    except bench.BenchError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/report")
def get_report(user: User = Depends(current_user)):
    return bench.report()


@router.get("/report/before-after")
def get_before_after(split: datetime, user: User = Depends(current_user)):
    return bench.before_after(split)
