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
import logging
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
from worker.engine.checkpointer import (
    DeltaChannel,
    MirroredSaver,
    ReplayJournal,
    open_checkpointer,
)
from worker.engine.compaction import Compactor, SelfTuningLimit
from worker.engine.events import EventEmitter
from worker.engine.graph import build_graph
from worker.engine.memory import EpisodicMemory, set_episodic_memory
from worker.engine.state import Autonomy, Budget, EngineState, Mode, tag_message
from worker.forwarder import Forwarder


def _permissions_from_env() -> list[dict[str, Any]]:
    """Team/repo permission rulesets: JSON list in COLLEGIUM_PERMISSIONS,
    e.g. [{"effect":"deny","tool":"terminal_exec","args":{"command":"git push *"}}].
    Malformed config fails closed to an empty ruleset (capability map only)."""
    import json
    raw = os.environ.get("COLLEGIUM_PERMISSIONS", "").strip()
    if not raw:
        return []
    try:
        rules = json.loads(raw)
        return rules if isinstance(rules, list) else []
    except json.JSONDecodeError:
        return []


log = logging.getLogger(__name__)


class EngineRunner:
    """One thread = one graph + one checkpointer + one emitter + one forwarder."""

    def __init__(self) -> None:
        self.run_id = os.environ["RUN_ID"]
        self.thread_id = os.environ["THREAD_ID"]
        self.task_prompt = os.environ["TASK_PROMPT"]
        self.mode = Mode(os.environ.get("MODE", "ask"))
        self.autonomy = Autonomy(os.environ.get("AUTONOMY", "supervised"))
        self.model = os.environ.get("MODEL", "kimi-k2.6")
        # Composer reasoning choice for this lane ("off" or an effort like
        # "max"). Empty = provider default — make_llm sends no override.
        self.reasoning_effort = os.environ.get("REASONING_EFFORT", "").strip() or None
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
        # D2: the seq store rides the durable session volume so a replaced
        # container CONTINUES the thread's seq instead of restarting at 0.
        self.emitter = EventEmitter(
            self.run_id, self.thread_id, self.context_id,
            seq_store=self.mirror_dir / f"{self.thread_id}.seq")
        self.forwarder = Forwarder(self.redis_url, self.run_id, self.thread_id)
        self.broker = ApprovalBroker(
            self.redis_url, self.run_id, self.thread_id, timeout_s=self.approval_timeout_s,
        )
        self.control = ControlListener(self.redis_url, self.thread_id)
        self.delta_channel = DeltaChannel(self.mirror_dir)
        # M-13: each runner (one per worker process) starts with a FRESH
        # spawn registry so a process reused across runs (or the spike
        # matrix running models sequentially in one process) can't inherit
        # the previous run's live spawns / watchdogs.
        from worker.engine.fanout import (
            get_registry,
            reset_registry,
            set_current_thread_id,
        )
        reset_registry()
        self._spawn_registry = get_registry()
        # M-14: scope the spawn registry's parent_thread_id to THIS runner
        # via a ContextVar (not the process-wide env), so concurrent/
        # sequential runs in one process can't cross-register spawns.
        set_current_thread_id(self.thread_id)

        self.tuning = SelfTuningLimit()
        self.compactor = Compactor()
        self.status = "running"
        self.last_activity = time.monotonic()
        self._stop = asyncio.Event()
        self._pending_nudges: asyncio.Queue[ControlMessage] = asyncio.Queue()
        # The in-flight turn (graph invocation incl. any approval wait) runs as
        # a tracked task so interrupt/kill can wake it: interrupt drains it
        # briefly, kill cancels immediately. Cancelling the task also cancels
        # the broker's BLPOP, so a pending approval never wedges a stop (G5).
        self._turn_task: asyncio.Task | None = None
        # Bounded graceful drain for interrupt (kill drains 0s). LangGraph
        # checkpoints at node boundaries, so a cancelled turn resumes safely
        # from the last checkpoint on the replacement container.
        self.interrupt_drain_s = float(os.environ.get("INTERRUPT_DRAIN_S", "30"))

    # ------------------------------------------------------------ state/config

    _IMAGE_MIME = {".png": "image/png", ".jpg": "image/jpeg",
                   ".jpeg": "image/jpeg", ".webp": "image/webp",
                   ".gif": "image/gif"}

    def _with_images(self, content: str) -> str | list[dict]:
        """Attachments -> multimodal first message for vision lanes.

        The backend stages files into the session volume and sets IMAGES_DIR
        ONLY for vision-capable lanes (blind lanes get the Kimi pre-pass
        description in the prompt text instead). The image blocks live in the
        FIRST HumanMessage, so they ride the message history and are present
        at every subsequent LLM call of the turn — "image+text at each step"
        falls out of the checkpoint, no per-call re-injection.
        """
        images_dir = os.environ.get("IMAGES_DIR", "").strip()
        if not images_dir:
            return content
        from worker.engine.llm import get_capabilities
        if not get_capabilities(self.model).vision:
            # Backend contract says this can't happen (IMAGES_DIR is only set
            # for vision lanes). Fail safe as text-only rather than 400 the
            # whole turn on a model that can't see.
            log.warning("IMAGES_DIR set for blind model %s — ignoring attachments",
                        self.model)
            return content
        import base64
        root = Path(images_dir)
        blocks: list[dict] = [{"type": "text", "text": content}]
        for path in sorted(root.iterdir()) if root.is_dir() else []:
            mime = self._IMAGE_MIME.get(path.suffix.lower())
            if mime is None:
                continue
            b64 = base64.b64encode(path.read_bytes()).decode()
            blocks.append({"type": "image_url",
                           "image_url": {"url": f"data:{mime};base64,{b64}"}})
        if len(blocks) == 1:
            log.warning("IMAGES_DIR %s had no readable images", images_dir)
            return content
        log.info("first message carries %d image(s)", len(blocks) - 1)
        return blocks

    def _initial_state(self) -> EngineState:
        # C2: PERSONA_PROMPT (persona + playbook + knowledge block, composed
        # backend-side) is injected as part of the INITIAL USER MESSAGE —
        # below the frozen system prompt so the byte-stable cache prefix is
        # untouched. Previously the backend set the env var and the engine
        # never read it: dead injection.
        persona = os.environ.get("PERSONA_PROMPT", "").strip()
        content = ""
        if persona:
            content += f"<persona>\n{persona}\n</persona>\n\n"
        content += f"Workspace root: {self.workspace}\n\n{self.task_prompt}"
        user_msg = tag_message(
            HumanMessage(content=self._with_images(content)), "user")
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
                "reasoning_effort": self.reasoning_effort,
                "emitter": self.emitter,
                "approval_broker": self.broker,
                "compactor": self.compactor,
                "tuning": self.tuning,
                "workspace": str(self.workspace),
                "event_sink": _event_sink,
                "delta_sink": _delta_sink,
                "metrics": self.metrics,
                "permissions": _permissions_from_env(),
                # K1: replay guard for non-idempotent tool calls, journaled on
                # the durable session volume so a replaced container can't
                # double-execute a crash-interrupted tools node.
                "replay_journal": ReplayJournal(self.mirror_dir),
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
        result: dict[str, Any] = {}
        started = False
        while True:
            # Service PENDING interrupts before invoking: on a container
            # restart the checkpoint already holds the approval card's
            # payload (with the approval_id the human is deciding on).
            # Invoking first would re-execute the gate node, regenerate a
            # NEW approval_id, and orphan the pending decision.
            snap = await graph.aget_state(config)
            interrupts = [i for task in snap.tasks for i in task.interrupts]
            if not interrupts:
                if started and not snap.next:
                    return result
                result = await graph.ainvoke(
                    input_or_none if not started else None, config)
                started = True
                continue
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
            started = True

    async def _emit_approval_card(self, payload: dict[str, Any]) -> None:
        """Render the interrupt payload as the approval-card StepEvent —
        dedicated APPROVAL kind with action_id pairing. ALSO bridge it to
        the backend ApprovalService (approvals:{run_id} stream) so the human
        gets the decidable card; a bridge failure degrades to the same
        deterministic timeout-deny as a lost decision channel."""
        await self.forwarder.publish_events([self.emitter.approval_card(payload, self.task_id)])
        try:
            await self.forwarder.publish_approval_request(payload)
        except Exception as exc:
            log.error("approval bridge publish failed; card will time out into a deny "
                      "(run=%s approval=%s): %s",
                      self.run_id, payload.get("approval_id"), str(exc)[:200])

    # ---------------------------------------------------------------- turn loop

    async def run(self) -> int:
        if self.canary:
            from collegium_contracts import StepKind
            await self.forwarder.publish_events([self.emitter._next(
                StepKind.STATUS, "canary: read-only thread on the custom engine",
                {"kind": "warning", "canary": True}, self.task_id, None,
            )])
        episodic = EpisodicMemory(self.mirror_dir / f"{self.thread_id}-episodes.db")
        set_episodic_memory(episodic)

        control_listener = asyncio.create_task(self.control.listen(), name="control-listen")
        control_pump = asyncio.create_task(self._control_pump(), name="control-pump")
        heartbeat = asyncio.create_task(self._heartbeat_loop(), name="heartbeat")
        watchdog = asyncio.create_task(self._idle_watchdog(), name="idle-watchdog")
        # C10: readiness ordering — the first heartbeat (the backend's
        # readiness probe) must not go out before the control channel is
        # subscribed, or a kill/nudge sent in that window is lost.
        try:
            await asyncio.wait_for(self.control.subscribed.wait(), timeout=10.0)
        except TimeoutError:
            log.warning("control subscribe slow; heartbeating anyway (run=%s thread=%s)",
                        self.run_id, self.thread_id)
        await self.forwarder.heartbeat(self.status)

        # B1: publish the engine's stable resumable identity UP FRONT, as a
        # dedicated event — the backend captures thread.session_id from the
        # detail field, never from a fragile "turn complete" title match.
        # A first-turn crash used to leave the thread unresumable because
        # identity only flowed on a successful turn boundary.
        try:
            from collegium_contracts import StepKind
            await self.forwarder.publish_events([self.emitter._next(
                StepKind.STATUS, "engine identity",
                {"kind": "engine_identity", "session_id": self.context_id,
                 "engine": "custom"},
                self.task_id, None,
            )])
        except Exception:
            log.warning("engine identity publish failed (run=%s thread=%s)",
                        self.run_id, self.thread_id)

        try:
            async with open_checkpointer() as saver:
                # Mirror every checkpoint write to the DeltaChannel JSONL —
                # the PHI-grade replay fallback when Postgres is unavailable
                # and the edit-and-resend fork source. Previously constructed
                # but never wired, so the fallback file was never written.
                saver = MirroredSaver(saver, self.delta_channel, self.thread_id, self.context_id)
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
                    await self._run_turn_guarded(graph, config, pending_input, episodic)
                elif self.status == "running":
                    # Resumed into an already-completed graph (container
                    # replacement on a finished thread): no turn runs, so
                    # nothing would ever move status off "running" — the
                    # idle watchdog only fires on "idle" and the thread
                    # would linger forever.
                    self.status = "idle"
                    await self.forwarder.heartbeat(self.status)

                # Idle turn loop: linger for nudges until kill or idle TTL.
                while not self._stop.is_set():
                    try:
                        nudge = await asyncio.wait_for(self._pending_nudges.get(), timeout=5.0)
                    except TimeoutError:
                        continue
                    await self._inject_and_run(graph, config, nudge, episodic)
        except Exception as exc:
            self.status = "failed"
            await self.forwarder.heartbeat(self.status)
            await self._emit_engine_error(str(exc))
            return 1
        finally:
            tasks = (control_listener, control_pump, heartbeat, watchdog)
            for task in tasks:
                task.cancel()
            # Cancellation only SCHEDULES CancelledError — gather the tasks
            # so their cleanup completes BEFORE the Redis connections they
            # may still be publishing on are closed underneath them.
            await asyncio.gather(*tasks, return_exceptions=True)
            # CASCADE DRAIN: the thread is stopping (kill, idle-complete,
            # SIGTERM, or failure) — stop every spawn registered under it.
            # Previously drain() existed but had no production caller, so
            # spawned threads outlived their parent contrary to the contract.
            drained = self._spawn_registry.drain(self.thread_id)
            if drained:
                try:
                    from collegium_contracts import StepKind
                    await self.forwarder.publish_events([self.emitter._next(
                        StepKind.STATUS,
                        f"cascade drain: {len(drained)} spawn(s) stopped",
                        {"kind": "cascade_drain", "drained": drained},
                        self.task_id, None,
                    )])
                except Exception:
                    log.warning("cascade-drain event publish failed (run=%s thread=%s)",
                                self.run_id, self.thread_id)
            await self.forwarder.close()
            await self.broker.close()
            await self.control.close()
            try:
                episodic.close()
            except Exception:
                pass
        return 0 if self.status != "failed" else 1

    async def _run_turn_guarded(self, graph: Any, config: dict[str, Any],
                                input_or_none: Any, episodic: EpisodicMemory) -> None:
        """Run one turn as a TRACKED task so interrupt/kill can cancel it
        (waking any pending approval BLPOP — G5). A cancelled turn is a clean
        stop when _stop is set: the checkpoint holds the last node boundary,
        so the replacement container resumes from there."""
        self._turn_task = asyncio.create_task(
            self._run_turn(graph, config, input_or_none, episodic), name="turn")
        try:
            await self._turn_task
        except asyncio.CancelledError:
            if self._stop.is_set():
                log.info("turn cancelled by stop request (run=%s thread=%s)",
                         self.run_id, self.thread_id)
                return
            raise
        finally:
            self._turn_task = None

    async def _request_stop(self, drain_s: float) -> None:
        """Shared interrupt/kill path: flip _stop FIRST (a Redis blip on the
        heartbeat must not leave the thread unstoppable), drain the in-flight
        turn for at most drain_s, then cancel it."""
        self._stop.set()
        task = self._turn_task
        if task is not None and not task.done():
            if drain_s > 0:
                try:
                    await asyncio.wait_for(asyncio.shield(task), timeout=drain_s)
                except (TimeoutError, asyncio.CancelledError, Exception):
                    pass
            if not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        self.status = "stopped"
        try:
            await self.forwarder.heartbeat(self.status)
        except Exception:
            pass

    async def _ack(self, msg: ControlMessage) -> None:
        """K14: ack a critical control so the backend can distinguish
        delivered-and-handled from lost-in-pubsub. Best-effort — the ack
        must never delay or fail the stop path."""
        if not msg.id:
            return
        try:
            await self.forwarder.redis.set(
                f"thread:{self.thread_id}:ack:{msg.id}", msg.type, ex=300)
        except Exception:
            log.warning("control ack publish failed (run=%s thread=%s msg=%s)",
                        self.run_id, self.thread_id, msg.id)

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
        try:
            self._record_episode(episodic, result)
        except Exception as exc:
            # Episodic memory is a best-effort side-effect. A SQLite blip
            # (disk full, locked db) must NEVER retroactively fail a turn
            # that already succeeded.
            log.warning("episodic record failed — memory side-effect, turn unaffected "
                        "(run=%s thread=%s): %s",
                        self.run_id, self.thread_id, exc)

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
        try:
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
        except Exception as exc:
            # M-04: a transient error on a nudge (LLM blip, tool timeout) used
            # to propagate out of _inject_and_run into the main loop's except,
            # which marked the whole thread failed and exited — killing the
            # thread and losing the idle-linger so the run could not be nudged
            # again. Fail the TURN only: emit a warning, drop back to idle, and
            # keep the thread alive for the next nudge.
            log.warning("nudge turn failed — failing turn, keeping thread "
                        "(run=%s thread=%s): %s",
                        self.run_id, self.thread_id, exc)
            self.status = "idle"
            await self.forwarder.heartbeat(self.status)
            await self._emit_engine_error(f"turn failed (thread alive): {exc}")

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
        and interrupt act immediately (both wake a pending approval wait)."""
        while not self._stop.is_set():
            try:
                msg: ControlMessage = await self.control.queue.get()
                self.last_activity = time.monotonic()
                if msg.type == "kill":
                    # Kill = immediate: no drain. Cancelling the turn task
                    # wakes the approval BLPOP (G5) so a replaced worker can
                    # never execute a late decision.
                    await self._request_stop(drain_s=0)
                    await self._ack(msg)
                    return
                if msg.type == "interrupt":
                    # A1: interrupt is a REAL stop path for the custom engine —
                    # bounded graceful drain, then cancel. Previously only kill
                    # was handled, so a UI stop depended on the gateway key
                    # being deleted to make the turn fail.
                    await self._request_stop(drain_s=self.interrupt_drain_s)
                    await self._ack(msg)
                    return
                if msg.type == "nudge":
                    # C11 (defined behavior): a nudge that arrives while the
                    # thread is parked on an approval card is QUEUED, not
                    # injected mid-wait — the human's decision lands first,
                    # the nudge runs as the next turn. Say so in the stream
                    # so the queued delivery is visible instead of silent.
                    if self.status == "input_required":
                        try:
                            from collegium_contracts import StepKind
                            await self.forwarder.publish_events([self.emitter._next(
                                StepKind.STATUS,
                                "nudge queued behind pending approval",
                                {"kind": "nudge_deferred"}, self.task_id, None,
                            )])
                        except Exception:
                            pass
                    await self._pending_nudges.put(msg)
                elif msg.type == "spawn_done":
                    # The feed signals a spawned subagent/swarm thread finished.
                    # This is the production path that moves a spawn out of
                    # "running" (besides the 2h watchdog) — without it the
                    # registry saturated permanently after SWARM_MAX_SLICES
                    # spawns and every subsequent fan-out was vetoed.
                    self._spawn_registry.finish(msg.text)
                elif msg.type == "mode":
                    try:
                        self.mode = Mode(msg.mode)
                        # Mode transitions are audited via a durable event.
                        from collegium_contracts import StepKind
                        await self.forwarder.publish_events([self.emitter._next(
                            StepKind.STATUS,
                            f"mode → {self.mode.value}",
                            {"kind": "mode_transition", "mode": self.mode.value},
                            self.task_id, None,
                        )])
                    except ValueError:
                        # C4: an invalid mode payload must fail LOUDLY — the
                        # old silent `pass` left backend and worker believing
                        # different modes with no trace.
                        log.warning("invalid mode control payload rejected: %s "
                                    "(run=%s thread=%s)", msg.mode,
                                    self.run_id, self.thread_id)
                        try:
                            from collegium_contracts import StepKind
                            await self.forwarder.publish_events([self.emitter._next(
                                StepKind.STATUS,
                                f"invalid mode ignored: {msg.mode}",
                                {"kind": "error", "error": "invalid_mode",
                                 "mode": msg.mode},
                                self.task_id, None,
                            )])
                        except Exception:
                            pass
                await self.forwarder.heartbeat(self.status)
            except asyncio.CancelledError:
                raise
            except Exception:
                # Same doctrine as the heartbeat loop (M-06): a Redis blip
                # must not kill the control pump — without it the thread is
                # permanently unresponsive to kill/nudge/mode/spawn_done.
                log.warning("control pump iteration failed — continuing "
                            "(run=%s thread=%s)",
                            self.run_id, self.thread_id, exc_info=True)
                await asyncio.sleep(0.5)

    async def _heartbeat_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self.forwarder.heartbeat(self.status)
            except Exception as exc:
                # M-06: a single Redis blip used to propagate out of this
                # background task into the main loop's except, killing the
                # whole thread. Log and keep beating — the next tick usually
                # succeeds once Redis recovers.
                log.warning("heartbeat publish failed — retrying next tick "
                            "(run=%s thread=%s): %s",
                            self.run_id, self.thread_id, exc)
            await asyncio.sleep(15)

    async def _idle_watchdog(self) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(30)
            try:
                idle_for = time.monotonic() - self.last_activity
                if self.status == "idle" and idle_for > self.idle_ttl_s:
                    self.status = "completed"
                    await self.forwarder.heartbeat(self.status)
                    self._stop.set()
                    return
            except Exception as exc:
                # M-06: a heartbeat blip on the completion publish used to
                # kill the watchdog (and the thread). Log and keep watching.
                log.warning("idle watchdog heartbeat failed — retrying next tick "
                            "(run=%s thread=%s): %s",
                            self.run_id, self.thread_id, exc)

    async def _emit_engine_error(self, error: str) -> None:
        from collegium_contracts import StepKind
        # Route through _next so seq is allocated monotonically — reading
        # emitter._seq without advancing it made the NEXT event reuse the
        # same seq, so consumers deduping on seq dropped legitimate events.
        event = self.emitter._next(
            StepKind.STATUS, "engine error", {"error": error}, self.task_id, None,
        )
        await self.forwarder.publish_events([event])


def main() -> int:
    runner = EngineRunner()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def _on_signal() -> None:
        # K13: SIGTERM mid-turn must honor the same bounded drain as a control
        # interrupt — the old handler only set _stop, so a turn (or a 900s
        # approval BLPOP) ran to completion before the loop noticed, and the
        # "stopped" heartbeat never went out. Route through _request_stop so
        # the turn is drained/cancelled and the status is published.
        loop.call_soon_threadsafe(
            lambda: asyncio.ensure_future(
                runner._request_stop(drain_s=runner.interrupt_drain_s)))

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _on_signal)
    try:
        return loop.run_until_complete(runner.run())
    finally:
        loop.close()


if __name__ == "__main__":
    raise SystemExit(main())
