"""Event bus: worker -> Redis STREAMS (durable ingest, consumer-group ack) ->
backend -> DB + WS (plan §8 events split).

The events table is the PHI-grade system of record; Streams + acks mean a backend
crash mid-write never loses an event. Transient typing deltas ride pub/sub ONLY.
Poison-pill path (plan §10): a StepEvent failing Pydantic validation goes to the
DEAD-LETTER stream + watchdog card — never acked-and-dropped (that would silently
hole the record), never blocks the consumer group.
"""

from __future__ import annotations

import asyncio
import json

import redis.asyncio as redis
from pydantic import ValidationError
from zagent_contracts import StepEvent

from app.core.config import get_settings
from app.core.logging import get_logger
from app.core.redis_factory import in_memory, make_redis
from app.db.base import get_session
from app.db.models.event import Event
from app.db.models.lane import Lane
from app.db.models.run import Run

log = get_logger(service="ingest")

GROUP = "ingest"
CONSUMER = "backend-1"
STREAM_PREFIX = "events:"
DEADLETTER_SUFFIX = ":deadletter"
# Idle-loop poll interval; tests monkeypatch this down to keep the suite fast.
IDLE_POLL_SECONDS = 0.5


class IngestConsumer:
    """Single-writer ingest: the ONLY path from worker events into the DB."""

    def __init__(self, relay) -> None:
        self.settings = get_settings()
        self.redis = make_redis()
        self.relay = relay  # events.relay.Relay — fanout to WS subscribers
        self.run_streams: set[str] = set()
        self._task: asyncio.Task | None = None

    def register_run(self, run_id: str) -> None:
        # Callers pass the BARE run id (run_manager/lane_manager); workers xadd
        # to events:<run_id>. Normalize here so both conventions converge on the
        # real stream key — before this, bare ids made the consumer read an
        # empty "rm1" stream forever while events piled up unconsumed.
        self.run_streams.add(run_id if run_id.startswith(STREAM_PREFIX)
                             else f"{STREAM_PREFIX}{run_id}")

    def unregister_run(self, run_id: str) -> None:
        self.run_streams.discard(run_id if run_id.startswith(STREAM_PREFIX)
                                 else f"{STREAM_PREFIX}{run_id}")

    async def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="ingest-consumer")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
        await self.redis.aclose()

    async def _ensure_group(self, stream: str) -> None:
        try:
            await self.redis.xgroup_create(stream, GROUP, id="0", mkstream=True)
        except redis.ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    async def _loop(self) -> None:
        while True:
            streams = {s: ">" for s in self.run_streams}
            if not streams:
                await asyncio.sleep(IDLE_POLL_SECONDS)
                continue
            for stream in streams:
                await self._ensure_group(stream)
            try:
                results = await self.redis.xreadgroup(
                    GROUP, CONSUMER, streams, count=200,
                    block=None if in_memory() else 1000,
                )
            except redis.ResponseError:
                await asyncio.sleep(IDLE_POLL_SECONDS)
                continue
            if not results and in_memory():
                await asyncio.sleep(IDLE_POLL_SECONDS)
                continue
            for stream, messages in results or []:
                run_id = stream.removeprefix(STREAM_PREFIX)
                for msg_id, fields in messages:
                    await self._process(stream, msg_id, fields, run_id)

    async def _process(self, stream: str, msg_id: str, fields: dict, run_id: str) -> None:
        try:
            event = StepEvent.model_validate(json.loads(fields["payload"]))
        except (ValidationError, json.JSONDecodeError, KeyError) as exc:
            # Poison pill: dead-letter + ack (moved, NOT dropped; group not blocked)
            await self.redis.xadd(stream + DEADLETTER_SUFFIX, {
                "original_id": msg_id, "error": str(exc)[:500],
                "payload": fields.get("payload", ""),
            })
            await self.redis.xack(stream, GROUP, msg_id)
            log.error("event dead-lettered", stream=stream, msg_id=msg_id, error=str(exc)[:200])
            return

        session = get_session()
        try:
            session.add(Event(
                run_id=event.run_id, lane_id=event.lane_id, seq=event.seq,
                ts=event.ts, type=event.kind.value, title=event.title[:512],
                payload=event.detail, sdk_message_uuid=event.sdk_message_uuid,
            ))
            lane = session.get(Lane, event.lane_id)
            if lane and event.seq >= lane.next_seq:
                lane.next_seq = event.seq + 1
            run = session.get(Run, run_id)
            if run:
                from datetime import datetime, timezone
                run.last_active_at = datetime.now(timezone.utc)
            session.commit()
        finally:
            session.close()

        await self.redis.xack(stream, GROUP, msg_id)
        await self.relay.publish_step(run_id, event)
