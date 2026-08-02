"""Approval service (plan §1a/§6): consumes the worker's approvals:{run_id}
stream into Approval rows + WS cards; decisions publish back to the worker's
blocking BLPOP. Timeout = DENY + notify (Autonomous never bridges).
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone

import redis.asyncio as redis
from sqlalchemy.exc import SQLAlchemyError

from app.core.config import get_settings
from app.core.logging import get_logger
from app.core.redis_factory import in_memory, make_redis
from app.db.base import get_session
from app.db.models.approval import Approval
from app.events.control import LaneControl
from app.events.relay import Relay

log = get_logger(service="approvals")

# Idle-loop poll interval; tests monkeypatch this down to keep the suite fast.
IDLE_POLL_SECONDS = 0.5
# How often the loop looks for cards the worker has already timed out on. The
# deadline itself is approval_timeout_seconds; this is just the check cadence.
SWEEP_INTERVAL_SECONDS = 30


class ApprovalService:
    def __init__(self, relay: Relay, control: LaneControl) -> None:
        self.relay = relay
        self.control = control
        self.redis = make_redis()
        self.run_streams: set[str] = set()
        self._task: asyncio.Task | None = None
        self._last_sweep = datetime.now(timezone.utc)

    def register_run(self, run_id: str) -> None:
        self.run_streams.add(run_id)

    def unregister_run(self, run_id: str) -> None:
        self.run_streams.discard(run_id)

    async def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="approvals-consumer")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
        await self.redis.aclose()

    async def _loop(self) -> None:
        while True:
            await self._expire_stale()
            if not self.run_streams:
                await asyncio.sleep(IDLE_POLL_SECONDS)
                continue
            streams = {f"approvals:{rid}": ">" for rid in self.run_streams}
            for stream in streams:
                try:
                    await self.redis.xgroup_create(stream, "approvals", id="0", mkstream=True)
                except redis.ResponseError as exc:
                    if "BUSYGROUP" not in str(exc):
                        raise
            results = await self.redis.xreadgroup("approvals", "backend-1", streams,
                                                  count=50,
                                                  block=None if in_memory() else 1000)
            if not results and in_memory():
                await asyncio.sleep(IDLE_POLL_SECONDS)
                continue
            for stream, messages in results or []:
                run_id = stream.removeprefix("approvals:")
                for msg_id, fields in messages:
                    await self._create_card(run_id, fields)
                    await self.redis.xack(stream, "approvals", msg_id)

    async def _expire_stale(self) -> None:
        """The worker's BLPOP has already given up on these and denied the tool,
        so a card past expires_at is answering nothing. Stamp decision=timeout
        (the audit trail keeps the distinction from a human deny) and tell the
        console to drop it — otherwise it sits there all night looking live."""
        now = datetime.now(timezone.utc)
        if now - self._last_sweep < timedelta(seconds=SWEEP_INTERVAL_SECONDS):
            return
        self._last_sweep = now
        session = get_session()
        try:
            stale = (session.query(Approval)
                     .filter(Approval.decision.is_(None),
                             Approval.expires_at.isnot(None),
                             Approval.expires_at <= now)
                     .all())
            if not stale:
                return
            resolved = [(a.id, a.run_id) for a in stale]
            for approval in stale:
                approval.decision = "timeout"
                approval.decided_at = now
            session.commit()
        except SQLAlchemyError as exc:  # a sweep hiccup must not kill the consumer
            log.warning("approval sweep failed", error=str(exc)[:200])
            return
        finally:
            session.close()
        for approval_id, run_id in resolved:
            log.info("approval timed out", approval_id=approval_id, run_id=run_id)
            await self.relay._fanout(run_id, {
                "type": "approval_resolved", "approval_id": approval_id, "decision": "timeout",
            })

    async def _create_card(self, run_id: str, fields: dict) -> None:
        ttl = get_settings().approval_timeout_seconds
        approval = Approval(
            id=fields.get("approval_id", str(uuid.uuid4())),
            run_id=run_id,
            lane_id=fields.get("lane_id"),
            kind=fields.get("kind", "tool"),
            payload=json.loads(fields.get("payload", "{}")),
            expires_at=datetime.now(timezone.utc) + timedelta(seconds=ttl),
        )
        session = get_session()
        try:
            session.add(approval)
            session.commit()
        finally:
            session.close()
        # Approval cards ALWAYS break through (§1a): even while the agent works,
        # Supervised autonomy surfaces tool-permission cards.
        await self.relay.publish_run_stage(run_id, "awaiting_user",
                                           ["allow_once", "always_allow", "deny_tool"])
        await self.relay._fanout(run_id, {"type": "approval_card", "approval": {
            "id": approval.id, "kind": approval.kind, "payload": approval.payload,
            "lane_id": approval.lane_id,
        }})
        self._push_to_owner(run_id, approval)

    @staticmethod
    def _push_to_owner(run_id: str, approval: Approval) -> None:
        """Well-timed ask (plan Phase 4): the push deep-links THIS card, and a
        push failure must never delay the approval flow."""
        try:
            from app.services import push
            from app.db.models.run import Run
            session = get_session()
            try:
                run = session.get(Run, run_id)
                owner = run.created_by if run else None
                title = run.title if run else run_id
            finally:
                session.close()
            if owner:
                push.send_to_user(owner, "Approval needed",
                                  f"{approval.kind}: {title[:80]}",
                                  push.approval_deep_link(run_id, approval.id))
        except Exception:  # noqa: BLE001 — notify never fails the ask
            pass

    async def decide(self, approval_id: str, decision: str, decided_by: int,
                     reason: str = "") -> Approval:
        session = get_session()
        try:
            approval = session.get(Approval, approval_id)
            if approval is None or approval.decision is not None:
                raise ValueError("approval not found or already decided")
            approval.decision = decision
            approval.decided_by = decided_by
            approval.decided_at = datetime.now(timezone.utc)
            session.commit()
        finally:
            session.close()
        await self.control.resolve_approval(approval_id, decision, reason)
        await self.relay._fanout(approval.run_id, {
            "type": "approval_resolved", "approval_id": approval_id, "decision": decision,
        })
        return approval
