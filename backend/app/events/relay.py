"""Redis -> WebSocket relay (monotonic ordering rule): StepEvents carry monotonic
per-thread seq; the relay delivers and the UI renders STRICTLY by seq. Transient
deltas carry no seq and never enter history — forwarded on a separate WS message
type so the UI can render the growing in-progress step then replace it.

Privacy: WS subscriptions are per-user; the relay only fans a run's events
out to sockets authenticated as run.created_by (enforced in ws/events.py).
"""

from __future__ import annotations

import asyncio
import json

from collegium_contracts import StepEvent

from app.core.redis_factory import in_memory, make_redis

# M-53: sentinel pushed onto a slow consumer's queue when the relay evicts
# it. Without this the consumer's queue.get() blocked forever (the queue was
# discarded from subscribers but never signaled), so the WS socket hung.
# The consumer sees the sentinel and closes cleanly (client resyncs on
# reconnect; steps are durable in the DB).
DROP_SENTINEL = object()


class Relay:
    def __init__(self) -> None:
        self.redis = make_redis()
        # run_id -> set of asyncio.Queue, one per subscribed socket
        self.subscribers: dict[str, set[asyncio.Queue]] = {}
        self._delta_tasks: dict[str, asyncio.Task] = {}

    def subscribe(self, run_id: str) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=2000)
        self.subscribers.setdefault(run_id, set()).add(queue)
        if run_id not in self._delta_tasks:
            self._delta_tasks[run_id] = asyncio.create_task(self._delta_loop(run_id))
        return queue

    def unsubscribe(self, run_id: str, queue: asyncio.Queue) -> None:
        self.subscribers.get(run_id, set()).discard(queue)
        if not self.subscribers.get(run_id):
            task = self._delta_tasks.pop(run_id, None)
            if task:
                task.cancel()

    async def publish_step(self, run_id: str, event: StepEvent) -> None:
        await self._fanout(run_id, {"type": "step", "event": event.model_dump(mode="json")})

    async def publish_thread_status(self, run_id: str, thread_id: str, status: str) -> None:
        await self._fanout(run_id, {"type": "thread_status", "thread_id": thread_id, "status": status})

    async def publish_note(self, run_id: str, text: str) -> None:
        # L-22: a run-scoped informational note (e.g. swarm capped a
        # fanout request). Distinct from publish_thread_status, which is
        # per-thread and requires a real thread_id + a valid status enum —
        # misusing it with a fake thread id and a free-text sentence was
        # silently dropped by the UI (no thread matched) and conflated a
        # sentence with a status. This is the proper channel for run notes.
        await self._fanout(run_id, {"type": "note", "text": text})

    async def publish_run_stage(self, run_id: str, stage: str, available_actions: list[str]) -> None:
        await self._fanout(run_id, {
            "type": "run_stage", "stage": stage, "available_actions": available_actions,
        })

    async def _fanout(self, run_id: str, message: dict) -> None:
        for queue in list(self.subscribers.get(run_id, set())):
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:
                # Slow consumer: drop transient headroom by evicting nothing —
                # steps are durable in the DB; the client resyncs on reconnect.
                # M-53: ALSO push a sentinel so the consumer's queue.get()
                # unblocks and the WS closes cleanly — the old code only
                # discarded the queue, leaving the socket hanging forever.
                self.subscribers.get(run_id, set()).discard(queue)
                self._send_drop_sentinel(queue)

    @staticmethod
    def _send_drop_sentinel(queue: asyncio.Queue) -> None:
        try:
            queue.get_nowait()  # make room (drop the oldest buffered message)
        except asyncio.QueueEmpty:
            pass
        try:
            queue.put_nowait(DROP_SENTINEL)
        except asyncio.QueueFull:
            pass  # couldn't make room; consumer will time out on its own

    async def _delta_loop(self, run_id: str) -> None:
        pubsub = self.redis.pubsub()
        await pubsub.subscribe(f"deltas:{run_id}")
        try:
            if in_memory():
                # fakeredis listen() waits on a thread condition — that would
                # freeze the dev server's one asyncio loop. Poll instead.
                while True:
                    raw = await pubsub.get_message(ignore_subscribe_messages=True, timeout=0)
                    if raw is None:
                        await asyncio.sleep(0.05)
                        continue
                    await self._handle_delta(run_id, raw)
            else:
                async for raw in pubsub.listen():
                    await self._handle_delta(run_id, raw)
        finally:
            await pubsub.unsubscribe(f"deltas:{run_id}")
            await pubsub.aclose()

    async def _handle_delta(self, run_id: str, raw: dict) -> None:
        if raw.get("type") != "message":
            return
        try:
            delta = json.loads(raw["data"])
        except (json.JSONDecodeError, TypeError):
            return
        await self._fanout(run_id, {"type": "delta", "delta": delta})

    # repo_added WS event: invalidates the repo-list query, no refresh.
    async def publish_global(self, message: dict) -> None:
        for run_id in list(self.subscribers):
            await self._fanout(run_id, message)

    async def close(self) -> None:
        for task in self._delta_tasks.values():
            task.cancel()
        await self.redis.aclose()
