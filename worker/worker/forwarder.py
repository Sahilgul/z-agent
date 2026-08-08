"""StepEvent -> Redis (events split).

DURABLE leg: complete StepEvents go to a Redis STREAM (persistent, consumer-group
ack by the backend ingest — the events table is the PHI-grade system of record and
pub/sub loss would silently hole replay). TRANSIENT leg: TypingDeltas ride pub/sub
only (loss is fine). Heartbeats ride a TTL key + pub/sub.
"""

from __future__ import annotations

import json
import time

import redis.asyncio as redis
from collegium_contracts import StepEvent, TypingDelta


class Forwarder:
    def __init__(self, redis_url: str, run_id: str, thread_id: str) -> None:
        self.redis = redis.from_url(redis_url, decode_responses=True)
        self.run_id = run_id
        self.thread_id = thread_id
        self.stream_key = f"events:{run_id}"
        self.delta_channel = f"deltas:{run_id}"
        self.heartbeat_key = f"thread:{thread_id}:heartbeat"
        self.heartbeat_channel = "thread:heartbeats"

    async def publish_events(self, events: list[StepEvent]) -> None:
        if not events:
            return
        # D9: the durable leg must survive a Redis blip — a dropped turn
        # boundary or engine-error signal silently holes the record. Bounded
        # retry with backoff; after the retries the exception propagates so
        # the caller's own doctrine applies (the runner marks the turn failed
        # rather than pretending the event landed).
        import asyncio
        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                pipe = self.redis.pipeline(transaction=False)
                for event in events:
                    pipe.xadd(self.stream_key, {
                        "thread_id": self.thread_id,
                        "seq": event.seq,
                        "payload": json.dumps(event.model_dump(mode="json")),
                        # D4: bound the stream so an unconsumed run can't grow
                        # Redis without limit; the events TABLE is the system
                        # of record, the stream is only the transport.
                    }, maxlen=100_000, approximate=True)
                await pipe.execute()
                return
            except Exception as exc:
                last_exc = exc
                if attempt < 2:
                    await asyncio.sleep(0.25 * (2 ** attempt))
        raise last_exc  # type: ignore[misc]

    async def publish_approval_request(self, payload: dict) -> None:
        """Bridge the engine's approval card to the backend ApprovalService:
        XADD to approvals:{run_id} so the backend creates the DECIDABLE
        Approval row (the interactive card with buttons). Without this the
        engine path only emitted a display-only StepEvent and no decision
        could ever arrive — the BLPOP always timed out into a deny."""
        await self.redis.xadd(f"approvals:{self.run_id}", {
            "approval_id": payload["approval_id"],
            "thread_id": self.thread_id,
            "kind": "tool",
            "payload": json.dumps({
                "tool": payload.get("tool"),
                "args": payload.get("args"),
                "preview": payload.get("preview"),
                "destructive": payload.get("destructive", False),
                "always_allowable": payload.get("always_allowable", False),
            }),
            "requested_at": str(time.time()),
        }, maxlen=200, approximate=True)  # cap: a retrying gate can't grow it forever

    async def publish_deltas(self, deltas: list[TypingDelta]) -> None:
        if not deltas:
            return
        pipe = self.redis.pipeline(transaction=False)
        for delta in deltas:
            pipe.publish(self.delta_channel, json.dumps(delta.model_dump(mode="json")))
        await pipe.execute()

    async def heartbeat(self, status: str) -> None:
        await self.redis.set(self.heartbeat_key, status, ex=90)
        await self.redis.publish(self.heartbeat_channel, json.dumps({
            "thread_id": self.thread_id, "run_id": self.run_id, "status": status,
        }))

    async def close(self) -> None:
        await self.redis.aclose()
