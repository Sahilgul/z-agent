"""Backend -> worker lane control (plan §4). Interrupt is immediate; nudge is
graceful interrupt+inject+resume on the worker side; kill_replace ends the
container and the lane manager re-stamps with the session volume mounted.
"""

from __future__ import annotations

import json

from app.core.redis_factory import make_redis


class LaneControl:
    def __init__(self) -> None:
        self.redis = make_redis()

    def _channel(self, lane_id: str) -> str:
        return f"lane:{lane_id}:control"

    async def interrupt(self, lane_id: str) -> None:
        await self.redis.publish(self._channel(lane_id), json.dumps({"type": "interrupt"}))

    async def nudge(self, lane_id: str, text: str) -> None:
        await self.redis.publish(self._channel(lane_id), json.dumps({"type": "nudge", "text": text}))

    async def set_mode(self, lane_id: str, permission_mode: str) -> None:
        await self.redis.publish(self._channel(lane_id), json.dumps({"type": "mode", "mode": permission_mode}))

    async def kill(self, lane_id: str) -> None:
        await self.redis.publish(self._channel(lane_id), json.dumps({"type": "kill"}))

    async def resolve_approval(self, approval_id: str, decision: str, reason: str = "") -> None:
        await self.redis.rpush(
            f"approval:{approval_id}:decision",
            json.dumps({"decision": decision, "reason": reason}),
        )

    async def close(self) -> None:
        await self.redis.aclose()
