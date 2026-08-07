"""Webhook ingress: ONE generic endpoint, signature-verified,
idempotent. Normalizes into contracts.TriggerEvent and hands to the triggers
engine — the ADO vocabulary lives in trigger ROWS, never in code.
"""

from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException, Request

from app.services import triggers

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


@router.post("/ado")
async def ado_webhook(request: Request, x_collegium_signature: str | None = Header(default=None)):
    body = await request.body()
    if not triggers.verify_signature(body, x_collegium_signature):
        raise HTTPException(status_code=401, detail="bad webhook signature")
    try:
        event = triggers.normalize_ado_work_item(await request.json())
    except (triggers.TriggerError, ValueError, KeyError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return await triggers.process(event, request.app.state.run_manager)
