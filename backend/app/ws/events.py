"""WebSocket endpoint (plan §8 ws/events.py): auth, per-user scoping, per-run
subscribe. §7a: a socket only receives events for runs the authenticated user
created — the relay fanout is keyed off this check.
"""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.core.security import decode_token
from app.db.base import get_session
from app.db.models.run import Run
from app.db.models.user import User

router = APIRouter()


def _authenticate(websocket: WebSocket) -> User | None:
    token = websocket.cookies.get("zagent_token")
    if not token:
        return None
    try:
        payload = decode_token(token)
    except Exception:
        return None
    session = get_session()
    try:
        user = session.get(User, int(payload["sub"]))
        if user is None or user.status != "active" or user.token_version != payload.get("token_version"):
            return None
        return user
    finally:
        session.close()


@router.websocket("/ws/runs/{run_id}")
async def run_events_ws(websocket: WebSocket, run_id: str):
    user = _authenticate(websocket)
    if user is None:
        await websocket.close(code=4401)
        return
    session = get_session()
    try:
        run = session.get(Run, run_id)
        if run is None or run.created_by != user.id:
            await websocket.close(code=4404)
            return
    finally:
        session.close()

    await websocket.accept()
    relay = websocket.app.state.relay
    queue = relay.subscribe(run_id)
    try:
        while True:
            message = await queue.get()
            await websocket.send_text(json.dumps(message))
    except (WebSocketDisconnect, asyncio.CancelledError):
        pass
    finally:
        relay.unsubscribe(run_id, queue)
