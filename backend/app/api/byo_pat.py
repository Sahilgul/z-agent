"""BYO-PAT routes (plan §1b Phase 3): store (identity-proofed), status
(write-only — never returns the secret), revoke. Mounted under /me.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.core.deps import current_user
from app.db.models.user import User
from app.services import byo_pat

router = APIRouter(prefix="/me/byo-pat", tags=["byo-pat"])


class PatBody(BaseModel):
    pat: str


@router.get("")
def status(user: User = Depends(current_user)):
    return byo_pat.pat_status(user.id)


@router.post("")
async def store(body: PatBody, user: User = Depends(current_user)):
    try:
        return await byo_pat.store_pat(user.id, body.pat)
    except byo_pat.ByoPatError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.delete("", status_code=204)
def remove(user: User = Depends(current_user)):
    byo_pat.revoke(user.id)
