"""Event bus: worker -> Redis STREAMS (durable ingest, consumer-group ack) ->
backend -> DB + WS (events split).

The events table is the PHI-grade system of record; Streams + acks mean a backend
crash mid-write never loses an event. Every stored event is also appended to the
run's flat JSONL transcript (services/transcript.py) for open/export without a
database. Transient typing deltas ride pub/sub ONLY.
Poison-pill path: a StepEvent failing Pydantic validation goes to the
DEAD-LETTER stream + watchdog card — never acked-and-dropped (that would silently
hole the record), never blocks the consumer group.
"""

from __future__ import annotations

import asyncio
import json

import redis.asyncio as redis
from pydantic import ValidationError
from zagent_contracts import StepEvent, StepKind

from app.core.config import get_settings
from app.core.logging import get_logger
from app.core.redis_factory import in_memory, make_redis
from app.db.base import get_session
from app.db.models.event import Event
from app.db.models.thread import Thread
from app.db.models.run import Run
from app.services import transcript

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
        # Callers pass the BARE run id (run_manager/thread_manager); workers xadd
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
                    try:
                        await self._process(stream, msg_id, fields, run_id)
                    except Exception as exc:  # H-43: a single bad event must
                        # not kill the ingest consumer for ALL runs. The old
                        # code let a DB error (or any non-validation exception
                        # from _process) propagate out of the loop, terminating
                        # the whole consumer. Dead-letter + ack the poison
                        # message (moved, not re-processed forever) and keep
                        # draining the other runs.
                        try:
                            await self.redis.xadd(stream + DEADLETTER_SUFFIX, {
                                "original_id": msg_id,
                                "error": f"consumer: {str(exc)[:400]}",
                                "payload": fields.get("payload", ""),
                            })
                            await self.redis.xack(stream, GROUP, msg_id)
                        except Exception as inner:  # noqa: BLE001 — Redis itself down
                            log.error("dead-letter write failed; message will be retried",
                                     stream=stream, msg_id=msg_id, error=str(inner)[:200])
                        log.error("event consumer error dead-lettered",
                                 stream=stream, msg_id=msg_id, error=str(exc)[:200])

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
                run_id=event.run_id, thread_id=event.thread_id, seq=event.seq,
                ts=event.ts, type=event.kind.value, title=event.title[:512],
                payload=event.detail, sdk_message_uuid=event.sdk_message_uuid,
            ))
            thread = session.get(Thread, event.thread_id)
            if thread and event.seq >= thread.next_seq:
                thread.next_seq = event.seq + 1
            # The worker's "turn complete" status event carries the SDK
            # session_id (worker/worker/normalize.py). Nothing else writes it,
            # so without this capture the thread is never resumable — the
            # replay-only banner on every session and kill_replace's claimed
            # resume both depend on this single field.
            if (
                thread is not None
                and event.kind == StepKind.STATUS
                and event.title == "turn complete"
                and event.detail.get("session_id")
            ):
                thread.session_id = str(event.detail["session_id"])
            run = session.get(Run, run_id)
            if run:
                from datetime import datetime, timezone
                run.last_active_at = datetime.now(timezone.utc)
            session.commit()
        finally:
            session.close()

        # Flat JSONL mirror. Best-effort: the row is already committed, so a
        # transcript failure must not re-deliver or drop the event.
        try:
            transcript.append(run_id, {
                "run_id": event.run_id, "thread_id": event.thread_id, "seq": event.seq,
                "ts": event.ts.isoformat() if hasattr(event.ts, "isoformat") else event.ts,
                "kind": event.kind.value, "title": event.title,
                "detail": event.detail, "sdk_message_uuid": event.sdk_message_uuid,
            })
        except OSError as exc:
            log.warning("transcript append failed", run_id=run_id, error=str(exc)[:200])

        await self.redis.xack(stream, GROUP, msg_id)
        await self.relay.publish_step(run_id, event)
