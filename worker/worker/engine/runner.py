"""Engine runner — the real turn loop.

This is what the worker container invokes once the seam is cut. It owns:

  - the compiled graph WITH a checkpointer (Postgres default via
    open_checkpointer — MemorySaver only when DATABASE_URL is unset, dev/test);
  - the interrupt-driven approval transport (Redis driver): the gate
    node interrupt()s -> the runner emits the card -> awaits the decision on
    Redis (deny-on-timeout) -> Command(resume=decision). The decision crosses
    the checkpoint boundary, so approvals survive container replacement;
  - the turn loop: initial prompt -> turn -> idle (lingers for nudges) ->
    nudge/kill/mode via the Redis control channel -> next turn, until idle
    TTL completes the thread;
  - turn bookkeeping: turn-boundary StepEvents, episodic-memory recording,
    heartbeats, budget status.

Env:
  RUN_ID, THREAD_ID, TASK_PROMPT, MODE, AUTONOMY, BUDGET_USD,
  MODEL (gateway alias), LITELLM_BASE_URL, LITELLM_API_KEY,
  REDIS_URL, WORKSPACE_DIR, DATABASE_URL (checkpointer; unset = MemorySaver),
  RESUME_CONTEXT_ID (optional), CHECKPOINT_MIRROR_DIR (optional),
  IDLE_TTL_SECONDS (optional, default 900), APPROVAL_TIMEOUT_S (optional, 900).

Nudge posture (v1, documented): nudges are injected at the TURN BOUNDARY — a
running turn finishes, then the nudge is appended as a NUDGE-tagged message
and the next turn starts. Kill is immediate (process exit; the checkpoint
preserves everything). Mid-turn graceful interruption lands with the RE
hardening fixtures.
"""

from __future__ import annotations

import asyncio
import os
import signal
import time
import uuid
from pathlib import Path
from typing import Any

from langchain_core.messages import HumanMessage
from langgraph.types import Command

from worker.control import ControlListener, ControlMessage
from worker.engine.approvals import ApprovalBroker
from worker.engine.checkpointer import DeltaChannel, open_checkpointer
from worker.engine.compaction import Compactor, SelfTuningLimit
from worker.engine.events import EventEmitter
from worker.engine.graph import build_graph
from worker.engine.memory import EpisodicMemory, set_episodic_memory
from worker.engine.state import Autonomy, Budget, EngineState, Mode, tag_message
from worker.forwarder import Forwarder


def _permissions_from_env() -> list[dict[str, Any]]:
    """Team/repo permission rulesets: JSON list in ZAGENT_PERMISSIONS,
    e.g. [{"effect":"deny","tool":"terminal_exec","args":{"command":"git push *"}}].
    Malformed config fails closed to an empty ruleset (capability map only)."""
    import json
    raw = os.environ.get("ZAGENT_PERMISSIONS", "").strip()
    if not raw:
        return []
    try:
        rules = json.loads(raw)
        return rules if isinstance(rules, list) else []
    except json.JSONDecodeError:
        return []


class EngineRunner:
    """One thread = one graph + one checkpointer + one emitter + one forwarder."""

    def __init__(self) -> None:
        self.run_id = os.environ["RUN_ID"]
        self.thread_id = os.environ["THREAD_ID"]
        self.task_prompt = os.environ["TASK_PROMPT"]
        self.mode = Mode(os.environ.get("MODE", "ask"))
        self.autonomy = Autonomy(os.environ.get("AUTONOMY", "supervised"))
        self.model = os.environ.get("MODEL", "kimi-foundry")
        self.budget = Budget(cap=float(os.environ.get("BUDGET_USD", "5.0")))
        self.redis_url = os.environ["REDIS_URL"]
        self.workspace = Path(os.environ.get("WORKSPACE_DIR", "/workspace"))
        self.resume_context_id = os.environ.get("RESUME_CONTEXT_ID")
        self.mirror_dir = Path(os.environ.get("CHECKPOINT_MIRROR_DIR", "./checkpoints"))
        self.idle_ttl_s = float(os.environ.get("IDLE_TTL_SECONDS", "900"))
        self.approval_timeout_s = int(os.environ.get("APPROVAL_TIMEOUT_S", "900"))
        # Canary mode: the custom engine serves READ-ONLY
        # production threads before the flag flip — ask-mode tools only,
        # supervised autonomy, regardless of what the request asked for.
        self.canary = os.environ.get("CANARY", "").strip().lower() in ("1", "true", "yes")
        if self.canary:
            self.mode = Mode.ASK
            self.autonomy = Autonomy.SUPERVISED

        self.context_id = self.resume_context_id or self.thread_id
        self.task_id = str(uuid.uuid4())
        self.emitter = EventEmitter(self.run_id, self.thread_id, self.context_id)
        self.forwarder = Forwarder(self.redis_url, self.run_id, self.thread_id)
        self.broker = ApprovalBroker(
            self.redis_url, self.run_id, self.thread_id, timeout_s=self.approval_timeout_s,
        )
        self.control = ControlListener(self.redis_url, self.thread_id)
        self.delta_channel = DeltaChannel(self.mirror_dir)

        self.tuning = SelfTuningLimit()
        self.compactor = Compactor()
        self.status = "running"
        self.last_activity = time.monotonic()
        self._stop = asyncio.Event()
        self._pending_nudges: asyncio.Queue[ControlMessage] = asyncio.Queue()

    # ------------------------------------------------------------ state/config

    def _initial_state(self) -> EngineState:
        user_msg = tag_message(
            HumanMessage(content=f"Workspace root: {self.workspace}\n\n{self.task_prompt}"),
            "user",
        )
        return {
            "run_id": self.run_id,
            "thread_id": self.thread_id,
            "context_id": self.context_id,
            "task_id": self.task_id,
            "mode": self.mode,
            "autonomy": self.autonomy,
            # dict form — msgpack-safe across the Postgres serde (graph.py
            # accepts both shapes on read).
            "budget": {"used": self.budget.used, "cap": self.budget.cap},
            "messages": [user_msg],
            "done": False,
            "error": None,
            "approved_calls": {},
            "denial_streak": 0,
            "tool_streak": {},
            "turn_count": 0,
            "compaction_count": 0,
            "compaction_retries": 0,
        }

    def _config(self) -> dict[str, Any]:
        async def _event_sink(events: list) -> None:
            await self.forwarder.publish_events(events)  # type: ignore[arg-type]

        async def _delta_sink(delta) -> None:
            await self.forwarder.publish_deltas([delta])

        from worker.engine.metrics import MetricsRegistry
        self.metrics = MetricsRegistry(self.run_id, self.thread_id)
        return {
            "configurable": {
                "thread_id": self.context_id,  # LangGraph checkpointer key
                "model": self.model,
                "emitter": self.emitter,
                "approval_broker": self.broker,
                "compactor": self.compactor,
                "tuning": self.tuning,
                "workspace": str(self.workspace),
                "event_sink": _event_sink,
                "delta_sink": _delta_sink,
                "metrics": self.metrics,
                "permissions": _permissions_from_env(),
            },
            "recursion_limit": 80,
        }

    # ---------------------------------------------------------- interrupt pump

    async def _invoke_with_approvals(self, graph: Any, config: dict[str, Any],
                                     input_or_none: Any) -> dict[str, Any]:
        """Run the graph to completion, servicing approval interrupts.

        Each pending interrupt is an approval card: emit it, wait for the
        human (deny-on-timeout inside the broker), resume with the decision.
        """
        result = await graph.ainvoke(input_or_none, config)
        while True:
            snap = await graph.aget_state(config)
            interrupts = [i for task in snap.tasks for i in task.interrupts]
            if not interrupts:
                return result or {}
            payload = interrupts[0].value
            await self._emit_approval_card(payload)
            self.status = "input_required"
            await self.forwarder.heartbeat(self.status)
            decision = await self.broker.wait_decision(payload["approval_id"])
            self.status = "running"
            await self.forwarder.heartbeat(self.status)
            # The paired decision event (same action_id as the card).
            decision = {**decision, "tool": payload.get("tool")}
            await self.forwarder.publish_events([
                self.emitter.approval_decision(payload["approval_id"], decision, self.task_id)])
            result = await graph.ainvoke(Command(resume=decision), config)

    async def _emit_approval_card(self, payload: dict[str, Any]) -> None:
        """Render the interrupt payload as the approval-card StepEvent —
        dedicated APPROVAL kind with action_id pairing."""
        await self.forwarder.publish_events([self.emitter.approval_card(payload, self.task_id)])

    # ---------------------------------------------------------------- turn loop

    async def run(self) -> int:
        if self.canary:
            from zagent_contracts import StepKind
            await self.forwarder.publish_events([self.emitter._next(
                StepKind.STATUS, "canary: read-only thread on the custom engine",
                {"kind": "warning", "canary": True}, self.task_id, None,
            )])
        await self.forwarder.heartbeat(self.status)
        episodic = EpisodicMemory(self.mirror_dir / f"{self.thread_id}-episodes.db")
        set_episodic_memory(episodic)

        control_listener = asyncio.create_task(self.control.listen(), name="control-listen")
        control_pump = asyncio.create_task(self._control_pump(), name="control-pump")
        heartbeat = asyncio.create_task(self._heartbeat_loop(), name="heartbeat")
        watchdog = asyncio.create_task(self._idle_watchdog(), name="idle-watchdog")

        try:
            async with open_checkpointer() as saver:
                graph = build_graph(checkpointer=saver)
                config = self._config()

                # Resume-on-restart: a fresh checkpoint is seeded with the
                # initial state; an existing one (container replacement) is
                # continued from where it stopped — pending approvals included.
                snap = await graph.aget_state(config)
                fresh = not snap.values
                pending_input: Any = self._initial_state() if fresh else None

                # First turn (or continuation of an in-flight one).
                if fresh or snap.next:
                    await self._run_turn(graph, config, pending_input, episodic)

                # Idle turn loop: linger for nudges until kill or idle TTL.
                while not self._stop.is_set():
                    try:
                        nudge = await asyncio.wait_for(self._pending_nudges.get(), timeout=5.0)
                    except TimeoutError:
                        continue
                    await self._inject_and_run(graph, config, nudge, episodic)
        except Exception as exc:  # noqa: BLE001
            self.status = "failed"
            await self.forwarder.heartbeat(self.status)
            await self._emit_engine_error(str(exc))
            return 1
        finally:
            for task in (control_listener, control_pump, heartbeat, watchdog):
                task.cancel()
            await self.forwarder.close()
            await self.broker.close()
            await self.control.close()
        return 0 if self.status != "failed" else 1

    async def _run_turn(self, graph: Any, config: dict[str, Any], input_or_none: Any,
                        episodic: EpisodicMemory) -> None:
        """One turn: run the graph (servicing approvals), then bookkeeping."""
        turn_start = time.monotonic()
        self.status = "running"
        await self.forwarder.heartbeat(self.status)
        result = await self._invoke_with_approvals(graph, config, input_or_none)
        self.last_activity = time.monotonic()

        err = result.get("error")
        duration_ms = int((time.monotonic() - turn_start) * 1000)
        boundary = self.emitter.turn_boundary(
            self.task_id, num_turns=result.get("turn_count", 1),
            duration_ms=duration_ms, is_error=bool(err),
            usage=result.get("last_usage"),
        )
        await self.forwarder.publish_events([boundary])
        self._record_episode(episodic, result)

        if err:
            self.status = "failed"
            await self.forwarder.heartbeat(self.status)
            raise RuntimeError(err)
        if result.get("blocked_reason"):
            self.status = "input_required"  # blocked-escalation: a human must act
        else:
            self.status = "idle"
        await self.forwarder.heartbeat(self.status)

    async def _inject_and_run(self, graph: Any, config: dict[str, Any],
                              nudge: ControlMessage, episodic: EpisodicMemory) -> None:
        """Append the nudge as a NUDGE-tagged message and run the next turn.

        A completed graph re-enters from START when invoked with a state
        delta (ainvoke(None) would be a no-op on a finished turn).
        """
        self.task_id = str(uuid.uuid4())
        snap = await graph.aget_state(config)
        messages = list(snap.values.get("messages", []))
        messages.append(tag_message(HumanMessage(content=nudge.text), "nudge"))
        await self._run_turn(graph, config, {
            "messages": messages,
            "done": False,
            "error": None,
            "task_id": self.task_id,
            "needs_compaction": False,
            # H-03: a control-channel mode change updated self.mode but never
            # the graph state, so the agent kept the old mode for every
            # subsequent turn. Carry the live mode into the state delta so
            # the next turn runs under the mode the user switched to.
            "mode": self.mode,
        }, episodic)

    def _record_episode(self, episodic: EpisodicMemory, result: dict[str, Any]) -> None:
        """One episode per turn (the memory.search substrate)."""
        messages = result.get("messages", [])
        last_text = ""
        for msg in reversed(messages):
            content = getattr(msg, "content", None)
            if isinstance(content, str) and content.strip():
                last_text = content
                break
        title = last_text.splitlines()[0][:120] if last_text else f"turn {result.get('turn_count', '?')}"
        episodic.record(
            run_id=self.run_id, thread_id=self.thread_id, task_id=self.task_id,
            turn=result.get("turn_count", 0), kind="turn",
            title=title, summary=last_text[:1000],
        )

    # ------------------------------------------------------- background tasks

    async def _control_pump(self) -> None:
        """Drain the control channel. Nudges queue for the turn boundary; kill
        and interrupt act immediately."""
        while not self._stop.is_set():
            msg: ControlMessage = await self.control.queue.get()
            self.last_activity = time.monotonic()
            if msg.type == "kill":
                self.status = "stopped"
                await self.forwarder.heartbeat(self.status)
                self._stop.set()
                return
            if msg.type == "nudge":
                await self._pending_nudges.put(msg)
            elif msg.type == "mode":
                try:
                    self.mode = Mode(msg.mode)
                    # Mode transitions are audited via a durable event.
                    from zagent_contracts import StepKind
                    await self.forwarder.publish_events([self.emitter._next(
                        StepKind.STATUS,
                        f"mode → {self.mode.value}",
                        {"kind": "mode_transition", "mode": self.mode.value},
                        self.task_id, None,
                    )])
                except ValueError:
                    pass
            await self.forwarder.heartbeat(self.status)

    async def _heartbeat_loop(self) -> None:
        while not self._stop.is_set():
            await self.forwarder.heartbeat(self.status)
            await asyncio.sleep(15)

    async def _idle_watchdog(self) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(30)
            idle_for = time.monotonic() - self.last_activity
            if self.status == "idle" and idle_for > self.idle_ttl_s:
                self.status = "completed"
                await self.forwarder.heartbeat(self.status)
                self._stop.set()
                return

    async def _emit_engine_error(self, error: str) -> None:
        from zagent_contracts import StepEvent, StepKind
        event = StepEvent(
            run_id=self.run_id, thread_id=self.thread_id, context_id=self.context_id,
            task_id=self.task_id, seq=self.emitter._seq,
            kind=StepKind.STATUS, title="engine error",
            detail={"error": error},
        )
        await self.forwarder.publish_events([event])


def main() -> int:
    runner = EngineRunner()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, runner._stop.set)
    try:
        return loop.run_until_complete(runner.run())
    finally:
        loop.close()


if __name__ == "__main__":
    raise SystemExit(main())
