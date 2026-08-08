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
import time
from datetime import UTC, datetime

from app.core.logging import get_logger
from app.core.redis_factory import in_memory, make_redis
from app.core.timefmt import aware_utc
from app.db.base import get_session
from app.db.models.run import Run
from app.db.models.thread import Thread
from app.orchestrator.semaphores import ACTIVE_STATUSES

log = get_logger(service="heartbeats")

CHANNEL = "thread:heartbeats"
# Throttle DB writes: a thread heartbeats every 15s, but the watchdog's stale
# threshold is 3 min — writing at most this often per thread is plenty fresh.
_MIN_WRITE_INTERVAL_SECONDS = 10.0
# E3: a thread whose container is GONE is dead no matter what the row says.
# The reaper scans active rows whose heartbeat is stale and confirms via
# Docker before stamping failed — a live container with a Redis blip is
# left alone; a dead one can never remain "running" forever.
STALE_AFTER_SECONDS = 180.0
REAPER_INTERVAL_SECONDS = 60.0
# A thread whose CONTAINER IS STILL RUNNING but whose beats stopped is a
# wedged worker (alive-but-mute: process up, event loop stuck, no events —
# the run sits at "working" forever and the composer can never deliver).
# Reap it only past a LONGER staleness and only while the bus is provably
# alive (a real Redis outage silences every thread at once — that's the
# mass-reap guard, not an excuse to leave zombies).
WEDGED_AFTER_SECONDS = 300.0
_BUS_ALIVE_WINDOW_SECONDS = REAPER_INTERVAL_SECONDS * 2


class HeartbeatPersister:
    def __init__(self, thread_manager=None, relay=None) -> None:
        self.redis = make_redis()
        # F1: optional — when wired, a reaped (terminal) thread goes through
        # the unified cleanup (settle cost, release + clear key). Without it
        # the reap only stamps the row and leaks the key/spend.
        self.thread_manager = thread_manager
        # Optional — when wired, a reap that orphans a run mid-work publishes
        # the INTERRUPTED stage so the UI swaps to resume immediately.
        self.relay = relay
        self._task: asyncio.Task | None = None
        self._reaper_task: asyncio.Task | None = None
        # Monotonic ts of the last heartbeat message seen on the bus (any
        # thread). The wedged-reap discriminator: bus alive + one thread
        # mute = that thread is dead; bus mute = Redis itself is down.
        self._last_beat_mono = 0.0
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
        self._reaper_task = asyncio.create_task(self._reaper_loop(), name="heartbeat-reaper")

    async def stop(self) -> None:
        for task in (self._task, self._reaper_task):
            if task:
                task.cancel()
        await self.redis.aclose()

    async def _reaper_loop(self) -> None:
        while True:
            await asyncio.sleep(REAPER_INTERVAL_SECONDS)
            try:
                await self._reap_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("liveness reaper iteration failed", error=str(exc)[:200])

    async def _reap_once(self) -> None:
        from app.sandbox.manager import sandbox_manager
        now = datetime.now(UTC)
        session = get_session()
        try:
            rows = (session.query(Thread)
                    .filter(Thread.status.in_(ACTIVE_STATUSES))
                    .all())
            # heartbeat_at is a naive-timestamp column: Postgres returns it
            # tz-less even though writers stamp aware UTC — coerce before
            # subtracting or the reaper dies every pass.
            stale = [(t.id, t.container_id, t.run_id,
                      (now - aware_utc(t.heartbeat_at)).total_seconds())
                     for t in rows
                     if t.heartbeat_at is not None
                     and (now - aware_utc(t.heartbeat_at)).total_seconds() > STALE_AFTER_SECONDS]
        finally:
            session.close()
        bus_alive = (time.monotonic() - getattr(self, "_last_beat_mono", 0.0)
                     ) < _BUS_ALIVE_WINDOW_SECONDS
        for thread_id, container_id, run_id, stale_s in stale:
            if not container_id:
                continue
            running = await asyncio.to_thread(
                sandbox_manager.container_running, container_id)
            if running:
                # Redis blip vs wedged worker: reap only when the bus is
                # provably alive (other beats landing) and the silence is
                # long past the 15s heartbeat cadence.
                if not (bus_alive and stale_s > WEDGED_AFTER_SECONDS):
                    continue
                log.warning("wedged worker reaped: container alive, heartbeat mute",
                            thread_id=thread_id, container_id=container_id[:12],
                            stale_s=int(stale_s))
                # Free the session volume and stop the zombie for real — a
                # DB-only reap would leave the container burning budget.
                await asyncio.to_thread(
                    sandbox_manager.wait_for_container_exit, container_id)
            session = get_session()
            try:
                thread = session.get(Thread, thread_id)
                if thread is None or thread.status not in ACTIVE_STATUSES:
                    continue
                thread.status = "failed"
                thread.finished_at = now
                session.commit()
                if not running:
                    log.warning("thread reaped: container gone with stale heartbeat",
                                thread_id=thread_id, container_id=container_id[:12])
            finally:
                session.close()
            # A reap can orphan the RUN at a working stage — no live threads,
            # no future events, composer accepting text into a dead session.
            # Move the run to INTERRUPTED so the UI offers resume.
            await self._interrupt_orphaned_run(run_id)
            if getattr(self, "thread_manager", None) is not None:
                # F1/F3: the reap is a terminal transition — settle spend and
                # release/clear the key like every other terminal path.
                try:
                    await self.thread_manager._cleanup_terminal(thread_id)
                except Exception:
                    log.warning("reaped-thread cleanup failed",
                                thread_id=thread_id, exc_info=True)
        # Key-leak sweep: any TERMINAL row still holding a gateway key means a
        # cleanup path crashed between delete and clear (or predates it). The
        # gateway TTL is the backstop; this sweep is the prompt fix.
        if getattr(self, "thread_manager", None) is not None:
            session = get_session()
            try:
                leaked = [t.id for t in session.query(Thread).filter(
                    Thread.status.in_(("completed", "failed", "stopped", "replaced")),
                    Thread.gateway_key.isnot(None)).limit(50).all()]
            finally:
                session.close()
            for tid in leaked:
                try:
                    await self.thread_manager._cleanup_terminal(tid)
                except Exception:
                    log.warning("key-leak sweep cleanup failed",
                                thread_id=tid, exc_info=True)

    async def _interrupt_orphaned_run(self, run_id: str) -> None:
        """If the reap took the run's last live thread while the run was
        mid-work, transition it to INTERRUPTED (resumable) and publish the
        stage — otherwise the UI never learns the session died. Runs with a
        surviving live thread, or parked on the human (awaiting_user /
        verifying / terminal), are untouched."""
        from collegium_contracts import RunStage

        from app.services.runs import transition
        session = get_session()
        available: list[str] = []
        try:
            run = session.get(Run, run_id)
            if run is None or run.stage not in (
                    RunStage.QUEUED.value, RunStage.PROVISIONING.value,
                    RunStage.INVESTIGATING.value, RunStage.PLANNING.value,
                    RunStage.DEVELOPING.value):
                return
            siblings = session.query(Thread).filter_by(run_id=run_id).all()
            if any(t.status in ACTIVE_STATUSES or t.status == "input_required"
                   for t in siblings):
                return
            transition(run, RunStage.INTERRUPTED)
            available = list(run.available_actions)
            session.commit()
            log.warning("run interrupted: last live thread reaped",
                        run_id=run_id)
        except Exception as exc:
            session.rollback()
            log.warning("orphaned-run interrupt failed", run_id=run_id,
                        error=str(exc)[:200])
            return
        finally:
            session.close()
        relay = getattr(self, "relay", None)
        if relay is not None:
            try:
                await relay.publish_run_stage(
                    run_id, RunStage.INTERRUPTED.value, available)
            except Exception:
                log.warning("orphaned-run stage publish failed", run_id=run_id)

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
            self._last_beat_mono = time.monotonic()
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
            thread.heartbeat_at = datetime.now(UTC)
            # The worker's heartbeat carries its live status; reflect it so the
            # watchdog and tiles agree with what the thread is actually doing.
            # But only while the row is ACTIVE: a beat is stale by definition
            # once the control plane has stamped a terminal status (stop_thread,
            # kill_replace, finish_thread, abandon) — a dying container's last
            # beat must never resurrect the row to "idle"/"running".
            # G8: include input_required — an approval-parked thread is ALIVE
            # and its next beat (back to running/idle after the human
            # decides) must un-park the row; otherwise the row froze at
            # input_required forever even though the engine resumed.
            if status and thread.status in (*ACTIVE_STATUSES, "input_required"):
                thread.status = status
            session.commit()
        except Exception as exc:  # a missed beat must never kill the loop
            session.rollback()
            log.warning("heartbeat persist failed", thread_id=thread_id, error=str(exc)[:200])
        finally:
            session.close()
