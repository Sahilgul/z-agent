"""Worker heartbeat -> Lane row persistence (watchdog ground truth).

The worker heartbeats into Redis every 15s (a TTL key + the lane:heartbeats
pub/sub). The frontend watchdog, though, reads Lane.heartbeat_at from the DB —
and nothing was bridging the two, so the row stayed frozen at spawn time and
every healthy lane false-alarmed "no signal in 3+ min" forever. This consumer
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
from app.db.models.lane import Lane

log = get_logger(service="heartbeats")

CHANNEL = "lane:heartbeats"
# Throttle DB writes: a lane heartbeats every 15s, but the watchdog's stale
# threshold is 3 min — writing at most this often per lane is plenty fresh.
_MIN_WRITE_INTERVAL_SECONDS = 10.0


class HeartbeatPersister:
    def __init__(self) -> None:
        self.redis = make_redis()
        self._task: asyncio.Task | None = None
        # lane_id -> monotonic ts of last DB write, to avoid a write per beat.
        self._last_write: dict[str, float] = {}

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
        lane_id = data.get("lane_id")
        if lane_id:
            self._persist(lane_id, data.get("status"))

    def _persist(self, lane_id: str, status: str | None) -> None:
        now_mono = asyncio.get_event_loop().time()
        last = self._last_write.get(lane_id, 0.0)
        if now_mono - last < _MIN_WRITE_INTERVAL_SECONDS:
            return
        self._last_write[lane_id] = now_mono
        session = get_session()
        try:
            lane = session.get(Lane, lane_id)
            if lane is None:
                return
            lane.heartbeat_at = datetime.now(timezone.utc)
            # The worker's heartbeat carries its live status; reflect it so the
            # watchdog and tiles agree with what the lane is actually doing.
            if status:
                lane.status = status
            session.commit()
        except Exception as exc:  # a missed beat must never kill the loop
            session.rollback()
            log.warning("heartbeat persist failed", lane_id=lane_id, error=str(exc)[:200])
        finally:
            session.close()
