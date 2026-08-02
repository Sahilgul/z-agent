"""Worker runtime (plan §8 — the load-bearing shape).

THREE concurrent asyncio tasks around receive_messages() (the long-lived
iterator) — event pump, Redis control listener, heartbeat — with explicit
turn-boundary bookkeeping via ResultMessage. receive_response() is the
single-response convenience iterator and CANNOT deliver nudge-while-working.

Nudge semantics (decided): graceful interrupt() + inject + resume — one SDK turn
is the entire agentic loop, so queued delivery would land after the work is done.

Config arrives via env at container start (plan §10: API keys never baked in):
  RUN_ID, LANE_ID, PERSONA_PROMPT, TASK_PROMPT, PERMISSION_MODE, BUDGET_USD,
  REDIS_URL, WORKSPACE_DIR, RESUME_SESSION_ID (optional), MODEL (optional),
  ANTHROPIC_BASE_URL + ANTHROPIC_AUTH_TOKEN (gateway + per-lane virtual key),
  IDLE_TTL_SECONDS (lane lingers for nudges after its turn ends).
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, ResultMessage

from worker.approvals import ApprovalBridge
from worker.control import ControlListener, ControlMessage
from worker.forwarder import Forwarder
from worker.normalize import Normalizer


class LaneConfig:
    def __init__(self) -> None:
        self.run_id = os.environ["RUN_ID"]
        self.lane_id = os.environ["LANE_ID"]
        self.persona_prompt = os.environ.get("PERSONA_PROMPT", "")
        self.task_prompt = os.environ["TASK_PROMPT"]
        self.permission_mode = os.environ.get("PERMISSION_MODE", "default")
        self.budget_usd = float(os.environ.get("BUDGET_USD", "5.0"))
        self.redis_url = os.environ["REDIS_URL"]
        self.workspace_dir = os.environ.get("WORKSPACE_DIR", "/workspace")
        self.resume_session_id = os.environ.get("RESUME_SESSION_ID") or None
        self.model = os.environ.get("MODEL") or None
        self.idle_ttl_seconds = int(os.environ.get("IDLE_TTL_SECONDS", "600"))


class LaneRuntime:
    """One lane = one worker container = one ClaudeSDKClient session."""

    def __init__(self, config: LaneConfig) -> None:
        self.cfg = config
        self.normalizer = Normalizer(config.run_id, config.lane_id)
        self.forwarder = Forwarder(config.redis_url, config.run_id, config.lane_id)
        self.control = ControlListener(config.redis_url, config.lane_id)
        self.approvals = ApprovalBridge(config.redis_url, config.run_id, config.lane_id)
        self.status = "starting"
        self.last_activity = asyncio.get_event_loop().time()
        self._stop = asyncio.Event()
        self._pump_done = asyncio.Event()
        self._pump_task: asyncio.Task | None = None

    def _options(self) -> ClaudeAgentOptions:
        kwargs: dict = {
            "permission_mode": self.cfg.permission_mode,
            "cwd": self.cfg.workspace_dir,
            "max_budget_usd": self.cfg.budget_usd,  # backstop; gateway per-key budget is authoritative
            "can_use_tool": self.approvals.ask,
        }
        if self.cfg.persona_prompt:
            kwargs["system_prompt"] = self.cfg.persona_prompt
        if self.cfg.resume_session_id:
            kwargs["resume"] = self.cfg.resume_session_id
        if self.cfg.model:
            kwargs["model"] = self.cfg.model
        return ClaudeAgentOptions(**kwargs)

    async def run(self) -> int:
        self.status = "running"
        await self.forwarder.heartbeat(self.status)
        async with ClaudeSDKClient(options=self._options()) as client:
            self._pump_task = asyncio.create_task(self._pump(client), name="event-pump")
            control = asyncio.create_task(self._control_loop(client), name="control")
            heartbeat = asyncio.create_task(self._heartbeat_loop(), name="heartbeat")
            watchdog = asyncio.create_task(self._idle_watchdog(), name="idle-watchdog")
            await client.query(self.cfg.task_prompt)
            try:
                # Wait for the CONTROL loop, not the pump: the pump legitimately
                # ends after every turn and is re-armed by nudges; control ends
                # only on kill. A pump exception is the lane's death, though.
                while not self._stop.is_set():
                    if control.done():
                        break
                    if self._pump_task.done() and (exc := self._pump_task.exception()):
                        # Gateway-down failure story: lane FAILS SAFE — the session
                        # volume makes it resumable (plan §10).
                        self.status = "failed"
                        await self.forwarder.heartbeat(self.status)
                        raise exc
                    await asyncio.sleep(0.25)
            finally:
                for task in (self._pump_task, control, heartbeat, watchdog):
                    task.cancel()
        return 0 if self.status != "failed" else 1

    # ---------------------------------------------------------- task 1: pump

    async def _pump(self, client: ClaudeSDKClient) -> None:
        """One receive_messages() call per turn. The iterator ENDS at each
        ResultMessage — draining it here and re-arming on the next query is
        what keeps the SDK session (and the agent's memory of the conversation)
        alive across nudges instead of restarting a stranger every message."""
        async for msg in client.receive_messages():
            self.last_activity = asyncio.get_event_loop().time()
            events, deltas = self.normalizer.handle(msg)
            await self.forwarder.publish_deltas(deltas)
            await self.forwarder.publish_events(events)
            if isinstance(msg, ResultMessage):
                # Turn-boundary bookkeeping: one SDK turn = the entire agentic
                # loop; after it ends the lane idles for nudges until idle TTL.
                self.status = "idle" if not msg.is_error else "failed"
                await self.forwarder.heartbeat(self.status)
        self._pump_done.set()

    # ------------------------------------------------------- task 2: control

    async def _control_loop(self, client: ClaudeSDKClient) -> None:
        listener = asyncio.create_task(self.control.listen())
        try:
            while not self._stop.is_set():
                msg: ControlMessage = await self.control.queue.get()
                self.last_activity = asyncio.get_event_loop().time()
                if msg.type == "interrupt":
                    await client.interrupt()
                    self.status = "interrupted"
                elif msg.type == "nudge":
                    # graceful interrupt + inject + resume (plan §8 decided semantics)
                    if self.status == "running":
                        await client.interrupt()
                    await client.query(msg.text)
                    self.status = "running"
                    # The previous turn's pump drained and exited at the result
                    # boundary; re-arm so this turn's messages are seen.
                    if self._pump_done.is_set():
                        self._pump_done.clear()
                        self._pump_task = asyncio.create_task(self._pump(client), name="event-pump")
                elif msg.type == "mode":
                    await client.set_permission_mode(msg.mode)
                elif msg.type == "kill":
                    self.status = "stopped"
                    self._stop.set()
                    return
                await self.forwarder.heartbeat(self.status)
        finally:
            listener.cancel()

    # ------------------------------------------------------ task 3: heartbeat

    async def _heartbeat_loop(self) -> None:
        while not self._stop.is_set():
            await self.forwarder.heartbeat(self.status)
            await asyncio.sleep(15)

    async def _idle_watchdog(self) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(30)
            idle_for = asyncio.get_event_loop().time() - self.last_activity
            if self.status == "idle" and idle_for > self.cfg.idle_ttl_seconds:
                self.status = "completed"
                await self.forwarder.heartbeat(self.status)
                self._stop.set()
                return

    async def close(self) -> None:
        await self.forwarder.close()
        await self.control.close()
        await self.approvals.close()


def main() -> int:
    config = LaneConfig()
    runtime = LaneRuntime(config)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, runtime._stop.set)
    try:
        return loop.run_until_complete(runtime.run())
    finally:
        loop.run_until_complete(runtime.close())
        loop.close()


if __name__ == "__main__":
    sys.exit(main())
