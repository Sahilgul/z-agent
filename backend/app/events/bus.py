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
from datetime import UTC

import redis.asyncio as redis
import sqlalchemy as sa
from collegium_contracts import StepEvent, StepKind
from pydantic import ValidationError

from app.core.config import get_settings
from app.core.logging import get_logger
from app.core.redis_factory import in_memory, make_redis
from app.db.base import get_session
from app.db.models.event import Event
from app.db.models.run import Run
from app.db.models.thread import Thread
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
        self._drain_tasks: set[asyncio.Task] = set()
        # D3: dead-letter depth per stream — the sweep alerts on GROWTH only.
        self._deadletter_seen: dict[str, int] = {}
        self._loops_since_scan = 0

    async def _drain_run(self, stream: str) -> None:
        """D4: on shutdown, claim and process orphaned pending entries so an
        acknowledged-but-crashed message isn't lost with the process."""
        try:
            claimed = await self.redis.xautoclaim(
                stream, GROUP, CONSUMER, min_idle_time=0, start_id="0-0", count=100)
            entries = claimed[1] if isinstance(claimed, (list, tuple)) and len(claimed) > 1 else []
            run_id = stream.removeprefix(STREAM_PREFIX)
            for msg_id, fields in entries or []:
                if not fields:  # deleted tombstones
                    continue
                try:
                    await self._process(stream, msg_id, fields, run_id)
                except Exception as exc:
                    log.warning("shutdown drain: dead-lettering failed event",
                                stream=stream, error=str(exc)[:200])
                    try:
                        await self._deadletter(stream, msg_id, fields, str(exc))
                    except Exception:
                        pass
        except Exception as exc:
            log.warning("shutdown drain failed", stream=stream, error=str(exc)[:200])

    async def _scan_deadletters(self) -> None:
        """D3: dead letters were write-only — a poison payload vanished from
        the pipeline silently. Alert on GROWTH via log + a note on the run."""
        for stream in list(self.run_streams):
            run_id = stream.removeprefix(STREAM_PREFIX)
            try:
                depth = await self.redis.xlen(f"{stream}:deadletter")
            except Exception:
                continue
            seen = self._deadletter_seen.get(stream, 0)
            if depth <= seen:
                continue
            self._deadletter_seen[stream] = depth
            log.error("dead letters accumulating", run_id=run_id,
                      depth=depth, new=depth - seen)
            try:
                await self.relay.publish_note(
                    run_id,
                    f"{depth - seen} event(s) dead-lettered for this run "
                    f"({depth} total) — the ingest pipeline dropped a payload; "
                    "check backend logs for the parse error.")
            except Exception:
                pass

    def register_run(self, run_id: str) -> None:
        # Callers pass the BARE run id (run_manager/thread_manager); workers xadd
        # to events:<run_id>. Normalize here so both conventions converge on the
        # real stream key — before this, bare ids made the consumer read an
        # empty "rm1" stream forever while events piled up unconsumed.
        self.run_streams.add(run_id if run_id.startswith(STREAM_PREFIX)
                             else f"{STREAM_PREFIX}{run_id}")

    def unregister_run(self, run_id: str) -> None:
        stream = (run_id if run_id.startswith(STREAM_PREFIX)
                  else f"{STREAM_PREFIX}{run_id}")
        # D4: best-effort drain of pending entries before the stream leaves
        # the read set. Fire-and-forget — stop() closes the client after the
        # consumer task, and the drain is idempotent by the unique constraint.
        try:
            task = asyncio.get_running_loop().create_task(self._drain_run(stream))
            self._drain_tasks.add(task)
            task.add_done_callback(self._drain_tasks.discard)
        except RuntimeError:
            pass  # no running loop (sync test teardown)
        self.run_streams.discard(stream)

    async def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="ingest-consumer")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
        # D4: give shutdown drains a moment to land before closing the client.
        if self._drain_tasks:
            try:
                await asyncio.wait(self._drain_tasks, timeout=5.0)
            except Exception:
                pass
        await self.redis.aclose()

    async def _deadletter(self, stream: str, msg_id: str, fields: dict,
                          error: str) -> None:
        await self.redis.xadd(stream + DEADLETTER_SUFFIX, {
            "original_id": msg_id, "error": error[:500],
            "payload": fields.get("payload", ""),
        })
        await self.redis.xack(stream, GROUP, msg_id)

    async def _ensure_group(self, stream: str) -> None:
        try:
            await self.redis.xgroup_create(stream, GROUP, id="0", mkstream=True)
        except redis.ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    async def _loop(self) -> None:
        while True:
            # D3: sweep dead letters roughly once a minute (1s block per loop).
            self._loops_since_scan += 1
            if self._loops_since_scan >= 60:
                self._loops_since_scan = 0
                try:
                    await self._scan_deadletters()
                except Exception:
                    log.warning("dead-letter sweep failed", exc_info=True)
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
                            await self._deadletter(stream, msg_id, fields,
                                                   f"consumer: {str(exc)[:400]}")
                        except Exception as inner:
                            log.error("dead-letter write failed; message will be retried",
                                     stream=stream, msg_id=msg_id, error=str(inner)[:200])
                        log.error("event consumer error dead-lettered",
                                 stream=stream, msg_id=msg_id, error=str(exc)[:200])

    async def _process(self, stream: str, msg_id: str, fields: dict, run_id: str) -> None:
        try:
            event = StepEvent.model_validate(json.loads(fields["payload"]))
        except (ValidationError, json.JSONDecodeError, KeyError) as exc:
            # Poison pill: dead-letter + ack (moved, NOT dropped; group not blocked)
            await self._deadletter(stream, msg_id, fields, str(exc))
            log.error("event dead-lettered", stream=stream, msg_id=msg_id, error=str(exc)[:200])
            return

        session = get_session()
        duplicate = False
        try:
            session.add(Event(
                run_id=event.run_id, thread_id=event.thread_id, seq=event.seq,
                ts=event.ts, type=event.kind.value, title=event.title[:512],
                payload=event.detail, sdk_message_uuid=event.sdk_message_uuid,
            ))
            # D1: flush the insert FIRST so a (run_id, thread_id, seq)
            # collision is caught before any side-effect columns update —
            # a redelivered entry is acked and skipped, never double-stored.
            try:
                session.flush()
            except sa.exc.IntegrityError:
                session.rollback()
                duplicate = True
                log.info("duplicate event redelivered — acking without store",
                         run_id=run_id, thread_id=event.thread_id, seq=event.seq)
            if duplicate:
                await self.redis.xack(stream, GROUP, msg_id)
                return
            thread = session.get(Thread, event.thread_id)
            if thread and event.seq >= thread.next_seq:
                thread.next_seq = event.seq + 1
            # Resumable identity capture (B1/D8): key on the DETAIL FIELD of a
            # STATUS event, never on the event title — the custom engine emits
            # an "engine identity" event at boot and the SDK's session_id rides
            # "turn complete"; a copy edit to either title must not silently
            # unresume every thread.
            if (
                thread is not None
                and event.kind == StepKind.STATUS
                and event.detail.get("session_id")
                # engine_identity: the custom engine's dedicated boot event
                # (B1). "turn complete": the legacy SDK normalize path.
                and (event.detail.get("kind") == "engine_identity"
                     or event.title == "turn complete")
            ):
                thread.session_id = str(event.detail["session_id"])
            run = session.get(Run, run_id)
            if run:
                from datetime import datetime
                run.last_active_at = datetime.now(UTC)
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

        # D6: relay BEFORE the ack. The old order (ack, then relay) meant a
        # relay crash after the ack silently dropped the live WS delivery —
        # the row existed but subscribers never saw it until a reconnect.
        # Now a relay failure propagates -> the message stays pending ->
        # redelivery re-relays it (the DB insert above is idempotent).
        await self.relay.publish_step(run_id, event)
        await self.redis.xack(stream, GROUP, msg_id)
