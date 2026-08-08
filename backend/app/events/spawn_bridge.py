"""SpawnBridge — backend-orchestrated swarm/subagent execution (C1, I1-I5).

The worker's spawn_agent / spawn_swarm tools no longer spawn anything
themselves (they never did — phantom registrations). They publish vetted
spawn REQUESTS to ``spawn_requests:{run_id}``; this bridge is the only
consumer and the only spawn authority:

  * threads are created through ThreadManager.spawn — capacity, the per-repo
    write lock, gateway keys, and containers all go through the one path;
  * fan-out is CLAMPED server-side (H4): requests beyond the global cap are
    rejected deterministically, never silently queued past 100;
  * when a child thread terminates, the bridge publishes ``spawn_done`` to
    the PARENT thread's control channel so the worker's registry frees the
    slot (the old code never sent it — the registry saturated permanently);
  * the 2h spawn timeout is REAL here (I3): a child exceeding it is killed
    via control.kill + verified container exit, not just relabeled.

Stream order is FIFO (XREADGROUP), so same-run spawn requests are processed
in arrival order (H6); a waiting request's queue position is its stream
rank, inspectable via XRANGE/XPENDING.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

from app.core.config import get_settings
from app.core.logging import get_logger
from app.core.redis_factory import in_memory, make_redis
from app.db.base import get_session
from app.db.models.repo import Repo
from app.db.models.run import Run
from app.db.models.thread import Thread

log = get_logger(service="spawn_bridge")

GROUP = "spawn-bridge"
CONSUMER = "backend-1"
STREAM_PREFIX = "spawn_requests:"
IDLE_POLL_SECONDS = 0.5
SPAWN_TIMEOUT_S = 2 * 60 * 60  # I3: 2h hard cap, enforced by termination
WATCH_POLL_S = 5.0

TERMINAL = ("completed", "failed", "stopped", "replaced")


class SpawnBridge:
    def __init__(self, thread_manager, control, relay) -> None:
        self.settings = get_settings()
        self.redis = make_redis()
        self.thread_manager = thread_manager
        self.control = control
        self.relay = relay
        self.run_streams: set[str] = set()
        self._task: asyncio.Task | None = None
        self._watchers: set[asyncio.Task] = set()

    def register_run(self, run_id: str) -> None:
        self.run_streams.add(run_id if run_id.startswith(STREAM_PREFIX)
                             else f"{STREAM_PREFIX}{run_id}")

    def unregister_run(self, run_id: str) -> None:
        self.run_streams.discard(run_id if run_id.startswith(STREAM_PREFIX)
                                 else f"{STREAM_PREFIX}{run_id}")

    async def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="spawn-bridge")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        for w in list(self._watchers):
            w.cancel()
        if self._watchers:
            await asyncio.gather(*self._watchers, return_exceptions=True)
        await self.redis.aclose()

    async def _ensure_group(self, stream: str) -> None:
        import redis.asyncio as redis
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
                    GROUP, CONSUMER, streams, count=50,
                    block=None if in_memory() else 1000)
            except Exception:
                await asyncio.sleep(IDLE_POLL_SECONDS)
                continue
            if not results and in_memory():
                await asyncio.sleep(IDLE_POLL_SECONDS)
                continue
            for stream, messages in results or []:
                run_id = stream.removeprefix(STREAM_PREFIX)
                for msg_id, fields in messages:
                    try:
                        await self._process(fields, run_id)
                    except Exception as exc:
                        log.error("spawn request failed",
                                  stream=stream, error=str(exc)[:200])
                    finally:
                        try:
                            await self.redis.xack(stream, GROUP, msg_id)
                        except Exception:
                            pass

    async def _process(self, fields: dict, run_id: str) -> None:
        req = json.loads(fields["payload"])
        spawn_id = req["spawn_id"]
        parent_id = req["parent_thread_id"]
        kind = req.get("kind", "agent")
        prompt = req.get("prompt", "")
        repo_name = req.get("repo") or None

        session = get_session()
        try:
            run = session.get(Run, run_id)
            if run is None or run.stage in ("completed", "failed", "abandoned"):
                await self.control.spawn_done(parent_id, spawn_id, "vetoed")
                return
            repo = (session.query(Repo).filter_by(name=repo_name).one_or_none()
                    if repo_name else None)
        finally:
            session.close()

        persona = "worker" if kind == "agent" else "swarm-slice"
        persona_prompt = (
            f"You are a spawned {persona} under thread {parent_id}. "
            "Do exactly the task in the prompt; keep changes minimal.")
        try:
            thread = await self.thread_manager.spawn(
                run, persona=persona, prompt=prompt, persona_prompt=persona_prompt,
                writable_repo=repo, context_repos=[repo] if repo else [],
            )
        except Exception as exc:
            # H4: over-cap / lock-conflict requests are a deterministic veto,
            # reported back so the parent agent sees the refusal.
            log.warning("spawn request vetoed", spawn_id=spawn_id,
                        reason=str(exc)[:200])
            await self.control.spawn_done(parent_id, spawn_id, "vetoed")
            return

        self._track(asyncio.ensure_future(
            self._watch_child(run_id, thread.id, parent_id, spawn_id)))

    def _track(self, task: asyncio.Task) -> None:
        self._watchers.add(task)
        task.add_done_callback(self._watchers.discard)

    async def _watch_child(self, run_id: str, thread_id: str,
                           parent_id: str, spawn_id: str) -> None:
        """Wait for the child to terminate, then notify the parent EXACTLY
        once (spawn_done). I3: a child still alive at the 2h cap is killed
        for real (control + container), then reported as timed_out."""
        deadline = asyncio.get_event_loop().time() + SPAWN_TIMEOUT_S
        status = "completed"
        while True:
            await asyncio.sleep(WATCH_POLL_S)
            session = get_session()
            try:
                thread = session.get(Thread, thread_id)
                state = thread.status if thread else "failed"
                container_id = thread.container_id if thread else None
            finally:
                session.close()
            if state in TERMINAL:
                status = state
                break
            if asyncio.get_event_loop().time() > deadline:
                # Real termination, not a relabel.
                await self.control.kill(thread_id, wait_ack=True)
                if container_id:
                    from app.sandbox.manager import sandbox_manager
                    exited = await asyncio.to_thread(
                        sandbox_manager.wait_for_container_exit, container_id)
                    if not exited:
                        log.error("spawn timeout: child survived force-stop",
                                  thread_id=thread_id)
                session = get_session()
                try:
                    thread = session.get(Thread, thread_id)
                    if thread and thread.status not in TERMINAL:
                        thread.status = "failed"
                        thread.finished_at = datetime.now(UTC)
                        session.commit()
                finally:
                    session.close()
                status = "timed_out"
                break
        # Exactly-once spawn_done: a duplicate watcher (backend restart
        # re-attaching, test double-drive) must not double-report — the
        # worker registry's finish() is idempotent, but a second message
        # would still wake the parent's turn loop spuriously.
        try:
            claimed = await self.redis.set(
                f"spawn_done:{spawn_id}", status, nx=True, ex=7 * 24 * 3600)
            if not claimed:
                return
            await self.control.spawn_done(parent_id, spawn_id, status)
        except Exception as exc:
            log.warning("spawn_done publish failed", spawn_id=spawn_id,
                        error=str(exc)[:120])
