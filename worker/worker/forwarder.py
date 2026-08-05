"""StepEvent -> Redis (plan §8 events split).

DURABLE leg: complete StepEvents go to a Redis STREAM (persistent, consumer-group
ack by the backend ingest — the events table is the PHI-grade system of record and
pub/sub loss would silently hole replay). TRANSIENT leg: TypingDeltas ride pub/sub
only (loss is fine). Heartbeats ride a TTL key + pub/sub.
"""

from __future__ import annotations

import json

import redis.asyncio as redis
from zagent_contracts import StepEvent, TypingDelta


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
        pipe = self.redis.pipeline(transaction=False)
        for event in events:
            pipe.xadd(self.stream_key, {
                "thread_id": self.thread_id,
                "seq": event.seq,
                "payload": json.dumps(event.model_dump(mode="json")),
            })
        await pipe.execute()

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
