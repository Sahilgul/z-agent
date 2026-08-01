"""Push subscription API (plan Phase 4): VAPID public key for the client,
subscription save/remove. Opt-in only — the frontend asks after the first
AwaitingYou moment, never on landing."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.core.deps import current_user
from app.core.config import get_settings
from app.db.models.user import User
from app.services import autonomy, push

router = APIRouter(tags=["push"])


class SubscriptionBody(BaseModel):
    endpoint: str = Field(min_length=10, max_length=1000)
    keys: dict = Field(default_factory=dict)


@router.get("/push/vapid-public-key")
def vapid_key(user: User = Depends(current_user)):
    return {"public_key": get_settings().vapid_public_key,
            "enabled": bool(get_settings().vapid_public_key)}


@router.post("/push/subscriptions", status_code=201)
def subscribe(body: SubscriptionBody, user: User = Depends(current_user)):
    try:
        return push.save_subscription(user.id, body.endpoint, body.keys)
    except push.PushError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.delete("/push/subscriptions", status_code=204)
def unsubscribe(body: SubscriptionBody, user: User = Depends(current_user)):
    push.remove_subscription(user.id, body.endpoint)


@router.get("/me/autonomy-cap")
def autonomy_cap(user: User = Depends(current_user)):
    """Evidence-based dial ceiling for the composer + promotion trail."""
    return autonomy.cap_for(user.id)
