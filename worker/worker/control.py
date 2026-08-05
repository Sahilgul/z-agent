"""Per-thread Redis control channel listener.

Controls: interrupt (stop, immediate) | nudge (graceful interrupt + inject +
resume — queued delivery would land AFTER the work) | mode
(set_permission_mode) | kill. Messages arrive on thread:{thread_id}:control and are
applied by the runtime's control task.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass

import redis.asyncio as redis


@dataclass
class ControlMessage:
    type: str  # interrupt | nudge | mode | kill
    text: str = ""
    mode: str = ""


class ControlListener:
    def __init__(self, redis_url: str, thread_id: str) -> None:
        self.redis = redis.from_url(redis_url, decode_responses=True)
        self.channel = f"thread:{thread_id}:control"
        self.queue: asyncio.Queue[ControlMessage] = asyncio.Queue()

    async def listen(self) -> None:
        pubsub = self.redis.pubsub()
        await pubsub.subscribe(self.channel)
        try:
            async for raw in pubsub.listen():
                if raw.get("type") != "message":
                    continue
                try:
                    data = json.loads(raw["data"])
                    await self.queue.put(ControlMessage(
                        type=data.get("type", ""),
                        text=data.get("text", ""),
                        mode=data.get("mode", ""),
                    ))
                except (json.JSONDecodeError, TypeError):
                    continue
        finally:
            await pubsub.unsubscribe(self.channel)
            await pubsub.aclose()

    async def close(self) -> None:
        await self.redis.aclose()
