"""Backend -> worker thread control. Interrupt is immediate; nudge is
graceful interrupt+inject+resume on the worker side; kill_replace ends the
container and the thread manager re-stamps with the session volume mounted.

Ack protocol (K14): pub/sub is lossy across worker reconnects, so critical
controls (interrupt, kill) carry a message id; the worker SETs
``thread:{id}:ack:{msg_id}`` once handled. Callers that need exactly-once
semantics (stop/replace/resume) pass ``wait_ack=True`` and fall back to
verified container exit when no ack arrives. The wait is gated on
COLLEGIUM_FEATURE_CONTROL_ACKS so the rollout can flip it independently.
"""

from __future__ import annotations

import asyncio
import json
import uuid

from app.core.config import get_settings
from app.core.redis_factory import make_redis

ACK_POLL_S = 0.25
# G3: decision payloads outlive the worker's own timeout so a slow human's
# decide still lands if the card hasn't been swept — but not forever.
APPROVAL_DECISION_TTL_S = 24 * 3600


class LaneControl:
    def __init__(self) -> None:
        self.redis = make_redis()

    def _channel(self, thread_id: str) -> str:
        return f"thread:{thread_id}:control"

    async def _publish(self, thread_id: str, payload: dict, *,
                       wait_ack: bool = False, ack_timeout_s: float = 10.0) -> bool:
        """Publish a control message. Critical messages carry an id; with
        wait_ack (and the feature flag on) poll for the worker's ack key.
        Returns True when the worker acked; False when fire-and-forget or
        the ack timed out (caller falls back to container-exit verification).
        """
        msg_id = str(uuid.uuid4())
        payload = {**payload, "id": msg_id}
        await self.redis.publish(self._channel(thread_id), json.dumps(payload))
        if not wait_ack or not get_settings().feature_control_acks:
            return False
        key = f"thread:{thread_id}:ack:{msg_id}"
        waited = 0.0
        while waited < ack_timeout_s:
            if await self.redis.get(key):
                return True
            await asyncio.sleep(ACK_POLL_S)
            waited += ACK_POLL_S
        return False

    async def interrupt(self, thread_id: str, *, wait_ack: bool = False,
                        ack_timeout_s: float = 10.0) -> bool:
        return await self._publish(thread_id, {"type": "interrupt"},
                                   wait_ack=wait_ack, ack_timeout_s=ack_timeout_s)

    async def nudge(self, thread_id: str, text: str) -> None:
        await self._publish(thread_id, {"type": "nudge", "text": text})

    async def set_mode(self, thread_id: str, permission_mode: str) -> None:
        await self._publish(thread_id, {"type": "mode", "mode": permission_mode})

    async def kill(self, thread_id: str, *, wait_ack: bool = False,
                   ack_timeout_s: float = 10.0) -> bool:
        return await self._publish(thread_id, {"type": "kill"},
                                   wait_ack=wait_ack, ack_timeout_s=ack_timeout_s)

    async def spawn_done(self, parent_thread_id: str, spawn_id: str,
                         status: str = "completed") -> None:
        """C1: tell the parent worker that a spawned child thread reached a
        terminal state. The worker's registry moves the spawn out of
        "running" so the fan-out slot frees (the old phantom path never sent
        this, so the registry saturated permanently)."""
        await self._publish(parent_thread_id,
                            {"type": "spawn_done", "text": spawn_id,
                             "status": status})

    async def resolve_approval(self, approval_id: str, decision: str,
                               reason: str = "",
                               edited_args: dict | None = None) -> None:
        payload = json.dumps({"decision": decision, "reason": reason,
                              **({"edited_args": edited_args}
                                 if edited_args is not None else {})})
        # G2: BLPOP is destructive — a worker crash between the pop and the
        # graph checkpoint lost the decision (backend said allow, engine
        # timed out into deny). Also SET a durable copy; the worker reads
        # the durable key first on (re)entry, so the window closes.
        await self.redis.set(
            f"approval:{approval_id}:decision_value", payload,
            ex=APPROVAL_DECISION_TTL_S)
        # G3: the list gets a TTL too — an orphaned decision (worker already
        # timed out) used to sit in Redis forever.
        await self.redis.rpush(f"approval:{approval_id}:decision", payload)
        await self.redis.expire(f"approval:{approval_id}:decision",
                                APPROVAL_DECISION_TTL_S)

    async def close(self) -> None:
        await self.redis.aclose()
