"""Worker heartbeat -> Thread row persistence (watchdog ground truth).

The worker heartbeats into Redis every 15s (a TTL key + the thread:heartbeats
pub/sub). The frontend watchdog, though, reads Thread.heartbeat_at from the DB —
and nothing was bridging the two, so the row stayed frozen at spawn time and
every healthy thread false-alarmed "no signal in 3+ min" forever. This consumer
subscribes to the pub/sub and stamps the row, so the watchdog reads liveness
that is actually current.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

from app.core.logging import get_logger
from app.core.redis_factory import in_memory, make_redis
from app.db.base import get_session
from app.db.models.thread import Thread
from app.orchestrator.semaphores import ACTIVE_STATUSES

log = get_logger(service="heartbeats")

CHANNEL = "thread:heartbeats"
# Throttle DB writes: a thread heartbeats every 15s, but the watchdog's stale
# threshold is 3 min — writing at most this often per thread is plenty fresh.
_MIN_WRITE_INTERVAL_SECONDS = 10.0


class HeartbeatPersister:
    def __init__(self) -> None:
        self.redis = make_redis()
        self._task: asyncio.Task | None = None
        # thread_id -> monotonic ts of last DB write, to avoid a write per beat.
        self._last_write: dict[str, float] = {}
        # thread_id -> last status we persisted. A status CHANGE must always be
        # written even inside the throttle window — the worker's running->idle
        # transition beat is unscheduled and lands right after a periodic beat,
        # so throttling it strands the row at "running" forever and the
        # watchdog nags a finished thread.
        self._last_status: dict[str, str | None] = {}

    async def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="heartbeat-persister")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
        await self.redis.aclose()

    async def _loop(self) -> None:
        pubsub = self.redis.pubsub()
        await pubsub.subscribe(CHANNEL)
        try:
            if in_memory():
                # fakeredis listen() blocks on a thread condition and would
                # freeze the dev server's one loop — poll instead (mirrors the
                # relay's delta loop workaround).
                while True:
                    raw = await pubsub.get_message(ignore_subscribe_messages=True, timeout=0)
                    if raw is None:
                        await asyncio.sleep(0.05)
                        continue
                    self._handle(raw)
            else:
                async for raw in pubsub.listen():
                    self._handle(raw)
        finally:
            await pubsub.unsubscribe(CHANNEL)
            await pubsub.aclose()

    def _handle(self, raw: dict) -> None:
        if raw.get("type") != "message":
            return
        try:
            data = json.loads(raw["data"])
        except (json.JSONDecodeError, TypeError):
            return
        thread_id = data.get("thread_id")
        if thread_id:
            self._persist(thread_id, data.get("status"))

    def _persist(self, thread_id: str, status: str | None) -> None:
        now_mono = asyncio.get_event_loop().time()
        last = self._last_write.get(thread_id)
        # M-41: the throttle used `self._last_write.get(thread_id, 0.0)`, so
        # for a NEW thread the first beat's `now_mono - 0.0` was measured against
        # loop-start (a small value early in the run), landing inside the
        # throttle window -> the first no-status beat was silently dropped.
        # Always persist the first beat per thread (no prior write to throttle).
        first_beat = last is None
        # A status transition bypasses the throttle: the running->idle beat is
        # the one that stops the watchdog from nagging a finished thread, and it
        # is unscheduled so it is the beat most likely to land inside the
        # window. Throttling is about write volume, not about losing state.
        status_changed = status is not None and self._last_status.get(thread_id) != status
        if not first_beat and not status_changed and now_mono - last < _MIN_WRITE_INTERVAL_SECONDS:
            return
        self._last_write[thread_id] = now_mono
        if status is not None:
            self._last_status[thread_id] = status
        session = get_session()
        try:
            thread = session.get(Thread, thread_id)
            if thread is None:
                return
            thread.heartbeat_at = datetime.now(timezone.utc)
            # The worker's heartbeat carries its live status; reflect it so the
            # watchdog and tiles agree with what the thread is actually doing.
            # But only while the row is ACTIVE: a beat is stale by definition
            # once the control plane has stamped a terminal status (stop_thread,
            # kill_replace, finish_thread, abandon) — a dying container's last
            # beat must never resurrect the row to "idle"/"running".
            if status and thread.status in ACTIVE_STATUSES:
                thread.status = status
            session.commit()
        except Exception as exc:  # a missed beat must never kill the loop
            session.rollback()
            log.warning("heartbeat persist failed", thread_id=thread_id, error=str(exc)[:200])
        finally:
            session.close()
