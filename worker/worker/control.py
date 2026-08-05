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
        """Subscribe and feed the queue — forever, across reconnects.

        pubsub.listen() raises ConnectionError when Redis drops the
        connection (blip, restart, keepalive timeout). Without a reconnect
        loop the listener task dies silently while consumers keep blocking
        on the queue — the thread becomes permanently unresponsive to
        kill/nudge. Treat a dropped connection as transient: clean up, back
        off, resubscribe. Only cancellation stops the loop."""
        backoff = 0.5
        while True:
            pubsub = self.redis.pubsub()
            try:
                await pubsub.subscribe(self.channel)
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
                        backoff = 0.5  # healthy message flow resets the backoff
                    except (json.JSONDecodeError, TypeError):
                        continue
            except asyncio.CancelledError:
                raise
            except Exception:
                # Transient Redis failure — back off and resubscribe.
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 5.0)
            finally:
                # Each cleanup step guarded so a failure in one never skips
                # the other (and a re-delivered cancel can't leak the pubsub).
                try:
                    await pubsub.unsubscribe(self.channel)
                except Exception:
                    pass
                try:
                    await pubsub.aclose()
                except Exception:
                    pass

    async def close(self) -> None:
        await self.redis.aclose()
