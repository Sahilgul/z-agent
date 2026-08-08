"""Run manager — the ONE run-creation path.

ALL run creation (UI tap, typed intent, chip, voice, cron, webhook) flows through
runs_create(source, initiated_by, mode, context): the day a second path appears,
attribution, privacy scoping, and budget checks fork.

Also owns run lifecycle intents: stop (interrupt -> interrupted stage, full trace
retained), abandon (kill + shred workspace, WITH confirmation upstream), resume
paths, and the boot-time reconciliation sweep (no silent zombie 'working' rows).
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Coroutine
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from collegium_contracts import RunStage

from app.core.logging import get_logger
from app.core.redact import redact
from app.core.timefmt import aware_utc
from app.db.base import get_session
from app.db.models.approval import Approval
from app.db.models.event import Event
from app.db.models.mode import Mode
from app.db.models.run import Run
from app.db.models.thread import Thread
from app.events.bus import IngestConsumer
from app.events.control import LaneControl
from app.events.relay import Relay
from app.orchestrator.blueprints.base import BlueprintContext
from app.orchestrator.mode_engine import blueprint_for
from app.orchestrator.thread_manager import ThreadManager
from app.sandbox.manager import sandbox_manager
from app.services.runs import TERMINAL_STAGES, transition

log = get_logger(service="run_manager")


def _title_is_generic(task: str, work_item_id: int) -> bool:
    """A title is generic when the user tapped a ticket without typing (empty,
    the bare work-item id, or a couple of characters). Real typed titles are
    never overridden."""
    stripped = (task or "").strip()
    return len(stripped) < 4 or stripped == str(work_item_id)


class RunManager:
    def __init__(self, ingest: IngestConsumer, relay: Relay, thread_manager: ThreadManager,
                 control: LaneControl, approvals: Any | None = None,
                 spawn_bridge: Any | None = None) -> None:
        self.ingest = ingest
        self.relay = relay
        self.thread_manager = thread_manager
        self.control = control
        # C1: the SpawnBridge consuming spawn_requests:{run_id}. Registered
        # alongside the ingest stream so a run's workers can request real
        # fan-out for the run's whole lifetime.
        self.spawn_bridge = spawn_bridge
        # The ApprovalService consuming approvals:{run_id}. Optional so unit
        # tests can construct a bare manager; production wires it in main.py —
        # without it the approvals consumer idles on an empty stream set and
        # no approval card is ever created.
        self.approvals = approvals
        self._tasks: dict[str, asyncio.Task] = {}

    # ---------------------------------------------------------- lifecycle core

    async def _stop_thread_container(self, thread_id: str) -> None:
        """Verified stop for one thread: interrupt with ack, then fall back
        to container-exit verification with a force-stop on timeout (A2).
        The caller stamps the DB row AFTER this returns — the stamp is no
        longer a fiction that races the worker's actual death."""
        session = get_session()
        try:
            thread = session.get(Thread, thread_id)
            container_id = thread.container_id if thread else None
        finally:
            session.close()
        acked = await self.control.interrupt(thread_id, wait_ack=True)
        if not acked and container_id:
            exited = await asyncio.to_thread(
                sandbox_manager.wait_for_container_exit, container_id)
            if not exited:
                log.error("container survived interrupt+force-stop; stamping "
                          "stopped anyway to free the slot",
                          thread_id=thread_id, container_id=container_id[:12])

    async def shutdown(self) -> None:
        """E6: drain tracked blueprint tasks on backend shutdown so an
        in-flight run isn't stranded mid-node (its thread would linger
        until the next boot's reconcile)."""
        tasks = [t for t in self._tasks.values() if not t.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _track(self, run_id: str, coro: Coroutine[object, object, object]) -> asyncio.Task:
        # L-21: self._tasks was never pruned — completed tasks accumulated
        # (slow leak for long-lived managers). Prune already-completed
        # entries lazily on each track() call (bounded growth) rather than
        # immediately on completion via a done-callback: removing the entry
        # the moment a task finishes would break awaiters that reach a
        # just-finished task via rm._tasks[run_id] right after it completes
        # (e.g. the M-64 fire-and-forget await pattern). Only remove if
        # it's still us: a newer task for the same run_id would have
        # replaced a completed entry.
        for rid, t in list(self._tasks.items()):
            if t.done():
                del self._tasks[rid]
        task = asyncio.ensure_future(coro)
        self._tasks[run_id] = task
        return task

    # ------------------------------------------------------------ creation

    async def create_run(self, source: str, initiated_by: int, mode_name: str,
                         task: str, repo: str | None = None,
                         work_item_id: int | None = None, autonomy: str | None = None,
                         fanout: int | None = None, delivery_id: int | None = None,
                         models: list[str] | None = None,
                         reasoning: dict[str, str] | None = None,
                         idempotency_key: str | None = None) -> Run:
        models = self._validate_models(models, mode_name)
        reasoning = self._validate_reasoning(reasoning, models)
        # Deterministic title hydration (THE one place it happens):
        # a generic typed title resolves from the ADO work item so the inbox
        # card reads the ticket's real title, not "42" or an empty string.
        if work_item_id is not None and _title_is_generic(task, work_item_id):
            from app.services import hydration
            task = await hydration.hydrate_title(work_item_id, task) or task
        session = get_session()
        try:
            # POST /runs idempotency: a client retry (double-click, network
            # flap) with the same key returns the ORIGINAL run instead of
            # minting a duplicate that double-spends budget.
            if idempotency_key:
                existing = (session.query(Run)
                            .filter_by(created_by=initiated_by,
                                       idempotency_key=idempotency_key)
                            .one_or_none())
                if existing is not None:
                    return existing
            mode = session.query(Mode).filter_by(name=mode_name, enabled=True).one_or_none()
            if mode is None:
                raise ValueError(f"unknown or disabled mode '{mode_name}'")
            run = Run(
                id=str(uuid.uuid4()), created_by=initiated_by, source=source,
                mode=mode_name, autonomy=autonomy or mode.autonomy_default,
                title=task[:256], repo=repo, work_item_id=work_item_id,
                delivery_id=delivery_id, idempotency_key=idempotency_key,
                started_at=datetime.now(UTC),
            )
            transition(run, RunStage.QUEUED)
            session.add(run)
            try:
                session.commit()
            except sa.exc.IntegrityError:
                # Lost the race: another request with the same key committed
                # first (the partial unique index guarantees exactly one).
                session.rollback()
                if not idempotency_key:
                    raise
                run = (session.query(Run)
                       .filter_by(created_by=initiated_by,
                                  idempotency_key=idempotency_key)
                       .one())
                return run
        finally:
            session.close()

        self.ingest.register_run(run.id)
        if self.approvals is not None:
            self.approvals.register_run(run.id)
        if self.spawn_bridge is not None:
            self.spawn_bridge.register_run(run.id)
        await self.relay.publish_run_stage(run.id, run.stage, run.available_actions)
        artifacts_extra = {
            **({"models": models} if models else {}),
            **({"reasoning": reasoning} if reasoning else {}),
        }
        self._track(run.id, self._execute(
            run.id, task, repo, fanout,
            artifacts_extra=artifacts_extra or None))
        return run

    def _validate_models(self, models: list[str] | None, mode_name: str) -> list[str] | None:
        """Composer model selection, checked against the registry BEFORE any
        thread spawns: unknown aliases are rejected (the engine never
        substitutes a model), and multi-model compare is ask-mode only — the
        blueprint modes have their own multi-thread semantics (plan/develop/
        swarm), so "same task on N models" only has meaning for ask."""
        if not models:
            return None
        from app.core.config import get_settings
        settings = get_settings()
        known = {m.alias for m in settings.available_models}
        # Dedupe preserving order — a double-selected alias would spawn two
        # identical lanes billing twice for the same answer.
        picked = list(dict.fromkeys(models))
        unknown = [m for m in picked if m not in known]
        if unknown:
            raise ValueError(
                f"unknown model '{unknown[0]}' — pick from {sorted(known)}")
        if len(picked) > 1 and mode_name != "ask":
            raise ValueError(
                f"multi-model compare is ask-mode only — pick one model for "
                f"mode '{mode_name}'")
        return picked

    def _validate_reasoning(self, reasoning: dict[str, str] | None,
                            models: list[str] | None) -> dict[str, str] | None:
        """Per-model reasoning choice. Keys must be models the run will
        actually use (the selection, or the deployment default when nothing
        is selected); values are "off" (thinking disabled) or one of the
        model's registry reasoning_efforts. Anything else is a client bug or
        a stale dropdown — reject, never silently clamp."""
        if not reasoning:
            return None
        from app.core.config import get_settings
        settings = get_settings()
        allowed_aliases = set(models) if models else {settings.gateway_model}
        clean: dict[str, str] = {}
        for alias, effort in reasoning.items():
            if alias not in allowed_aliases:
                raise ValueError(
                    f"reasoning set for '{alias}', which this run doesn't use")
            option = settings.model_option(alias)
            if option is None:
                raise ValueError(f"unknown model '{alias}'")
            if effort == "off":
                if not option.supports_thinking_off:
                    raise ValueError(
                        f"model '{alias}' always thinks — 'off' is not offered")
            elif effort not in option.reasoning_efforts:
                raise ValueError(
                    f"model '{alias}' takes reasoning {sorted(option.reasoning_efforts)} "
                    f"or 'off' — not '{effort}'")
            clean[alias] = effort
        return clean

    async def resume_run(self, run_id: str, initiated_by: int) -> Run | None:
        """Continue the SAME run row (H-22): re-stamp from QUEUED, mount the
        prior thread's session volume, resume with its session_id. The old
        `resume` API called create_run — a fresh run with no link to the
        prior session, so the worker started a stranger every resume. Here
        we re-execute the existing run row and seed ctx.artifacts with
        resume_from_thread_id so the blueprint spawns a thread that
        inherits the prior session_id (thread_manager.spawn already wires
        the inherited session + the old session volume mount)."""
        session = get_session()
        try:
            # A3: lock the run row so a double-clicked resume serializes
            # instead of double-executing the blueprint.
            run = (session.query(Run).filter_by(id=run_id)
                   .with_for_update().one_or_none())
            if run is None or run.created_by != initiated_by:
                return None
            # A3: resume is only meaningful from a terminal stage. Resuming an
            # ACTIVE run would double-execute it — a second _execute task on
            # the same row spawning duplicate threads. Idempotent: return the
            # run unchanged when it is already in flight.
            if run.stage not in (TERMINAL_STAGES | {RunStage.INTERRUPTED.value}):
                log.warning("resume refused — run in flight",
                            run_id=run_id, stage=run.stage)
                session.expunge(run)
                return run
            last_thread = (session.query(Thread)
                          .filter_by(run_id=run_id)
                          .order_by(Thread.created_at.desc())
                          .first())
            last_thread_id = last_thread.id if last_thread is not None else None
            last_container_id = last_thread.container_id if last_thread is not None else None
            last_thread_live = (last_thread is not None
                                and last_thread.status in ("running", "idle", "queued",
                                                           "input_required"))
            transition(run, RunStage.QUEUED, allow_terminal_exit=True)  # H-41
            run.finished_at = None
            run.failure_reason = None  # back in flight — the old reason is stale
            if last_thread is not None and last_thread_live:
                last_thread.status = "stopped"
                last_thread.finished_at = datetime.now(UTC)
            session.commit()
        finally:
            session.close()
        # A3: a "terminal" run can still hold a live container (crashed
        # control plane, missed kill). Kill + verified-exit before the
        # replacement mounts the prior session volume.
        if last_thread is not None and last_thread_live:
            await self.control.kill(last_thread_id, wait_ack=True)
            if last_container_id:
                exited = await asyncio.to_thread(
                    sandbox_manager.wait_for_container_exit, last_container_id)
                if not exited:
                    raise RuntimeError(
                        f"prior container {last_container_id[:12]} survived "
                        "kill+force-stop; aborting resume to avoid a "
                        "double-mounted session volume")
            # F1: settle + release + clear on the thread being replaced by
            # the resume, not a bare key release.
            await self._cleanup_terminal(last_thread_id)
        self.ingest.register_run(run_id)
        if self.approvals is not None:
            self.approvals.register_run(run_id)
        if self.spawn_bridge is not None:
            self.spawn_bridge.register_run(run_id)
        await self.relay.publish_run_stage(run_id, run.stage, run.available_actions)
        self._track(
            run_id,
            self._execute(run_id, run.title, run.repo,
                          artifacts_extra={"resume_from_thread_id": last_thread_id}))
        return run

    async def _execute(self, run_id: str, task: str, repo: str | None,
                       fanout: int | None = None,
                       artifacts_extra: dict | None = None) -> None:
        session = get_session()
        try:
            run = session.get(Run, run_id)
            # M-49: expunge run so it's detached with loaded column attrs
            # intact (no commit here, so no expiry — but be explicit so a
            # later commit in this scope can't expire it before close).
            session.expunge(run)
        finally:
            session.close()
        blueprint = blueprint_for(run.mode)
        artifacts: dict = {"task": task, "repo": repo,
                          **({"fanout": fanout} if fanout is not None else {})}
        if artifacts_extra:
            artifacts.update(artifacts_extra)
        ctx = BlueprintContext(
            run=run,
            services={"thread_manager": self.thread_manager, "relay": self.relay,
                      "control": self.control},
            artifacts=artifacts,
        )
        await self._guarded_execute(run_id, ctx, blueprint)

    async def _guarded_execute(self, run_id: str, ctx: BlueprintContext, blueprint) -> None:
        """THE failure path for every blueprint execution — initial AND chained.
        A raised node marks the run FAILED and relays the stage; without this a
        chained blueprint (approve/reject/start_plan chains) would die silently
        and strand the run in its stage until the next boot reconciliation."""
        try:
            await blueprint.execute(ctx)
            # F3: success paths settle too — plan/debug/development blueprints
            # never called settle_cost, so their spend vanished from the run.
            await self._settle_run_costs(run_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("run failed", run_id=run_id, error=str(exc)[:300])
            # Surface the failure reason in the chat — without this the run
            # silently flips to "failed" and the user sees an empty session with
            # no explanation (the @mention "no repo targeted" error is invisible
            # otherwise). Publish the error text as a run-scoped note so the
            # event stream renders it inline.
            try:
                await self.relay.publish_note(run_id, f"run failed: {str(exc)[:500]}")
            except Exception:
                pass
            session = get_session()
            try:
                row = session.get(Run, run_id)
                # H-42: don't overwrite an already-terminal run. A chained
                # blueprint that raises AFTER the run reached COMPLETED/
                # ABANDONED used to flip it to FAILED, overwriting the
                # correct terminal state and confusing the UI/audit. Only
                # fail a run that is still in flight.
                if row.stage not in TERMINAL_STAGES:
                    transition(row, RunStage.FAILED)
                    row.finished_at = datetime.now(UTC)
                    # W-H13: persist WHY so a session opened after the failure
                    # renders the reason inline — the relay note above only
                    # reaches clients connected at failure time.
                    row.failure_reason = redact(str(exc))[:500]
                    session.commit()
            finally:
                session.close()
            if row.stage == RunStage.FAILED.value:
                await self.relay.publish_run_stage(run_id, RunStage.FAILED.value, [])
                # W-H5: a failed run's pending cards are zombies — the
                # worker is gone. Stamp + fan out so consoles drop them.
                await self._resolve_pending_approvals(run_id, "denied")
            # F1/F3: a failed run used to leave its threads "running" (capacity
            # held until the reaper), keys live, spend unsettled. Terminate
            # every live thread for real, then run the unified cleanup.
            session = get_session()
            try:
                live = [t.id for t in session.query(Thread).filter_by(run_id=run_id).all()
                        if t.status in ("running", "idle", "queued", "input_required")]
            finally:
                session.close()
            for tid in live:
                try:
                    await self._stop_thread_container(tid)
                    await self.thread_manager._mark(tid, "failed")
                    await self.relay.publish_thread_status(run_id, tid, "failed")
                except Exception:
                    log.warning("failed-run thread cleanup error",
                                run_id=run_id, thread_id=tid, exc_info=True)

    async def _settle_run_costs(self, run_id: str) -> None:
        """F3: settle every terminal-but-unsettled thread of the run and roll
        the total onto the run row. Idempotent: a cleaned-up thread has no
        stored key, so settle_cost early-returns without clobbering cost_usd."""
        session = get_session()
        try:
            threads = session.query(Thread).filter_by(run_id=run_id).all()
            # All threads with a live key — including still-live ones whose
            # blueprint never settles them (plan/debug/development): record
            # the interim spend now; the thread's terminal cleanup re-settles.
            ids = [t.id for t in threads if t.gateway_key]
        finally:
            session.close()
        for tid in ids:
            try:
                await self.thread_manager.settle_cost(tid)
            except Exception:
                log.warning("run-end cost settle failed", run_id=run_id,
                            thread_id=tid, exc_info=True)

    async def _resolve_pending_approvals(
        self, run_id: str, decision: str, thread_id: str | None = None,
    ) -> None:
        """W-H5: a terminal transition strands every pending approval card —
        the worker's BLPOP is gone, so the card would sit "waiting on you"
        forever (a zombie). Stamp the decision for the audit trail and fan
        out ``approval_resolved`` so every open console drops the card."""
        now = datetime.now(UTC)
        session = get_session()
        try:
            q = session.query(Approval).filter(
                Approval.run_id == run_id, Approval.decision.is_(None))
            if thread_id is not None:
                q = q.filter(Approval.thread_id == thread_id)
            ids = []
            for a in q.all():
                a.decision = decision
                a.decided_at = now
                ids.append(a.id)
            session.commit()
        finally:
            session.close()
        for approval_id in ids:
            try:
                await self.relay.publish_approval_resolved(run_id, approval_id, decision)
            except Exception:
                log.warning("approval_resolved fanout failed",
                            approval_id=approval_id, run_id=run_id)

    async def _cleanup_terminal(self, thread_id: str) -> None:
        """F1: unified terminal cleanup; falls back to a bare key release for
        test doubles that predate the unified path."""
        fn = getattr(self.thread_manager, "_cleanup_terminal", None)
        if fn is not None:
            await fn(thread_id)
        else:
            await self.thread_manager.release_key(thread_id)

    # ------------------------------------------------------------ lifecycle

    async def stop_run(self, run_id: str) -> None:
        """One tap, no confirmation — stopping is safe and reversible. Full trace
        retained; banner: 'Stopped by you — all work preserved.'"""
        now = datetime.now(UTC)
        session = get_session()
        try:
            run = session.get(Run, run_id)
            if run.stage in TERMINAL_STAGES:
                return  # H-41: don't resurrect a terminal run to INTERRUPTED
            threads = session.query(Thread).filter_by(run_id=run_id).all()
            # E5: include input_required — a thread parked on an approval card
            # still holds a live container the stop must reach.
            live_statuses = ("running", "idle", "queued", "input_required")
            thread_ids: list[str] = [l.id for l in threads if l.status in live_statuses]
        finally:
            session.close()
        # Verified stop BEFORE the DB stamp: the row flips only after the
        # worker acked or its container is confirmed gone (A1/A2).
        for thread_id in thread_ids:
            await self._stop_thread_container(thread_id)
        session = get_session()
        try:
            run = session.get(Run, run_id)
            if run.stage in TERMINAL_STAGES:
                return
            threads = session.query(Thread).filter_by(run_id=run_id).all()
            for l in threads:
                if l.status in live_statuses:
                    # Write the Thread DB status so the capacity semaphore
                    # releases the slot (C-15).
                    l.status = "stopped"
                    l.finished_at = now
            transition(run, RunStage.INTERRUPTED)
            session.commit()
            available = run.available_actions
        finally:
            session.close()
        for thread_id in thread_ids:
            await self.relay.publish_thread_status(run_id, thread_id, "stopped")
            # F1/F3: unified terminal cleanup — settle cost, release the key,
            # clear the stored secret — for each stopped thread.
            await self._cleanup_terminal(thread_id)
        task = self._tasks.get(run_id)
        if task and not task.done():
            task.cancel()
        await self.relay.publish_run_stage(run_id, RunStage.INTERRUPTED.value, available)
        await self._resolve_pending_approvals(run_id, "stopped")

    async def abandon_run(self, run_id: str) -> None:
        """Kill run, shred workspace. Separate overflow-menu action WITH
        confirmation — never confused with Stop."""
        session = get_session()
        try:
            threads = session.query(Thread).filter_by(run_id=run_id).all()
            thread_ids = [l.id for l in threads]
            container_ids = [l.container_id for l in threads if l.container_id]
            run = session.get(Run, run_id)
            if run.stage in TERMINAL_STAGES:
                return  # H-41: don't resurrect a terminal run to ABANDONED
            transition(run, RunStage.ABANDONED)
            run.finished_at = datetime.now(UTC)
            session.commit()
        finally:
            session.close()
        for thread_id in thread_ids:
            await self.control.kill(thread_id, wait_ack=True)
        for container_id in container_ids:
            # Verified exit (force-stop on timeout) — abandon is the shred
            # path; a live container must never survive a workspace shred.
            await asyncio.to_thread(
                sandbox_manager.wait_for_container_exit, container_id)
        # F1/F3: abandon used to leave thread rows NON-terminal (capacity leak
        # until the reaper), keys live, and spend unsettled. Stamp + clean up
        # every thread of the run through the one terminal path.
        session = get_session()
        try:
            now = datetime.now(UTC)
            for l in session.query(Thread).filter_by(run_id=run_id).all():
                if l.status not in ("completed", "failed", "stopped", "replaced"):
                    l.status = "stopped"
                    l.finished_at = now
            session.commit()
        finally:
            session.close()
        for thread_id in thread_ids:
            await self._cleanup_terminal(thread_id)
            await self.relay.publish_thread_status(run_id, thread_id, "stopped")
        task = self._tasks.get(run_id)
        if task and not task.done():
            task.cancel()
        await asyncio.to_thread(sandbox_manager.shred_workspace, run_id)
        self.ingest.unregister_run(run_id)
        if self.approvals is not None:
            self.approvals.unregister_run(run_id)
        if self.spawn_bridge is not None:
            self.spawn_bridge.unregister_run(run_id)
        await self.relay.publish_run_stage(run_id, RunStage.ABANDONED.value, [])
        await self._resolve_pending_approvals(run_id, "stopped")

    async def nudge_thread(self, run_id: str, thread_id: str, text: str) -> None:
        """Typed Lead-nudge: stays enabled while the agent works (carve-out).
        Worker semantics: graceful interrupt + inject + resume.

        Refuses to resurrect a terminal thread (stopped/replaced/timed_out/
        completed/failed): the old code set status="running" unconditionally,
        which flipped a dead thread back to "running" and stranded the run
        while leaking the capacity slot (C-16). Only ACTIVE threads are nudged;
        a missing thread stays a no-op (the control nudge goes nowhere)."""
        from app.orchestrator.semaphores import ACTIVE_STATUSES
        # input_required is NOT terminal: the worker is alive in its idle
        # nudge loop (blocked-escalation) or queued behind an approval wait —
        # refusing the nudge stranded the run with a live container waiting on
        # a control message that could never arrive. Kept OUT of
        # ACTIVE_STATUSES itself so capacity accounting and the heartbeat
        # terminal-stamp guard are untouched.
        nudgeable = (*ACTIVE_STATUSES, "input_required")
        session = get_session()
        try:
            thread = session.get(Thread, thread_id)
            if thread is None:
                # H-50: a missing thread has no worker to interrupt — nudging it
                # publishes a "running" status for a thread that doesn't exist
                # (ghost thread) and writes a control message that goes nowhere.
                log.warning("nudge refused — thread not found",
                            run_id=run_id, thread_id=thread_id)
                return
            if thread.status not in nudgeable:
                log.warning("nudge refused — thread terminal",
                            run_id=run_id, thread_id=thread_id, status=thread.status)
                return  # do NOT nudge or flip a dead thread back to "running"
            thread.status = "running"
            session.commit()
        finally:
            session.close()
        await self.control.nudge(thread_id, text)
        await self.relay.publish_thread_status(run_id, thread_id, "running")

    # ------------------------------------------------------------ thread controls
    async def stop_thread(self, run_id: str, thread_id: str) -> None:
        """Per-thread stop from the swarm view: verified interrupt (ack or
        confirmed container exit), trace kept, the rest of the swarm runs on.
        Safe + reversible — no confirmation. Every stop path (this, stop_run,
        the /threads/{id}/stop API) funnels here so bookkeeping is identical."""
        await self._stop_thread_container(thread_id)
        session = get_session()
        try:
            thread = session.get(Thread, thread_id)
            if thread and thread.run_id == run_id:
                thread.status = "stopped"
                thread.finished_at = datetime.now(UTC)
                session.commit()
        finally:
            session.close()
        await self.relay.publish_thread_status(run_id, thread_id, "stopped")
        # F1: unified terminal cleanup (settle, release, clear) for the stop.
        await self._cleanup_terminal(thread_id)
        await self._resolve_pending_approvals(run_id, "stopped", thread_id=thread_id)

    async def pin_finding(self, run_id: str, thread_id: str, note: str = "") -> None:
        """Pin a finding from a thread overlay: lands as a run event the
        knowledge flywheel's approval inbox picks up as a candidate."""
        session = get_session()
        try:
            # M-32: lock the thread row so concurrent next_seq bumps
            # serialize (no duplicate seqs across concurrent pins/messages).
            thread = (session.query(Thread)
                      .filter_by(id=thread_id).with_for_update().one())
            if thread.run_id != run_id:
                raise ValueError("thread not found in this run")
            seq = thread.next_seq
            # M-44: pin_finding used thread.next_seq but never incremented it,
            # so every pin landed on the SAME seq (colliding with the next
            # message and each other). Bump it like _persist_user_message.
            thread.next_seq = seq + 1
            session.add(Event(
                run_id=run_id, thread_id=thread_id, seq=seq,
                type="pin", title=note[:200] or f"pinned finding from {thread.persona}",
                payload={"persona": thread.persona, "note": note},
            ))
            session.commit()
        finally:
            session.close()
        # W5-L2: was publish_thread_status("pinned") — a ≤15s cosmetic flash
        # the next lanes poll reverted, since no row was ever stamped. The
        # pin ITSELF is durable (the Event row above); the announcement is
        # informational, so it goes out as a run note, not a fake status.
        await self.relay.publish_note(
            run_id, f"pinned finding from {thread.persona}: {note[:160]}",
        )

    async def kill_replace_thread(
        self, run_id: str, thread_id: str,
        extra_context_repo_names: list[str] | None = None,
    ) -> Thread:
        """Kill-and-replace: the old thread dies; a FRESH thread spawns with the
        SAME spawn context (stored at spawn — never re-derived from the
        blueprint). The session volume survives the container, so the
        replacement resumes where the killed thread left off — now actually
        true, because resume_from_thread_id mounts the old session volume and
        inherits the old session_id.

        ``extra_context_repo_names`` unions into the stored mount set: a
        turn-X @mention that names a repo not already mounted expands the
        replacement's context (Docker can't add mounts to a running container,
        so a replace-with-resume is the mechanism). Already-mounted names are
        de-duped; the writable target (repo_scope) stays first."""
        session = get_session()
        try:
            thread = session.get(Thread, thread_id)
            if thread is None or thread.run_id != run_id:
                raise ValueError("thread not found in this run")
            # H-41: don't replace an already-terminal thread — the old
            # code set status="replaced" unconditionally, re-resurrecting
            # a stopped/failed/completed thread and double-spawning a
            # replacement (orphan + capacity leak).
            if thread.status in ("replaced", "stopped", "completed", "failed"):
                raise ValueError(
                    f"thread already terminal ({thread.status}); cannot replace")
            context = dict(thread.spawn_context or {})
            persona = thread.persona
            repo_scope = thread.repo_scope
            old_container_id = thread.container_id
            # Mount set = stored context_repos (names) + extras. The stored
            # snapshot is the source of truth — re-deriving from the blueprint
            # would re-resolve @mentions against a possibly-edited task and
            # silently drop a repo the user added mid-conversation.
            stored_names: list[str] = list(context.get("context_repos") or [])
            if repo_scope and repo_scope not in stored_names:
                stored_names.insert(0, repo_scope)
            for name in (extra_context_repo_names or []):
                if name not in stored_names:
                    stored_names.append(name)
            run = session.get(Run, run_id)
        finally:
            session.close()
        # H-37 + A2 + W-H8: kill, then WAIT for the old container to actually
        # die BEFORE stamping "replaced" — the old order stamped first, so a
        # survived-container abort left the tile reading "replaced" with no
        # replacement (and its key already settled/released). False from
        # wait_for_container_exit (force-stop on timeout) aborts BEFORE any
        # stamp; ValueError surfaces to the intent API as a 422, not a 500.
        await self.control.kill(thread_id, wait_ack=True)
        if old_container_id:
            exited = await asyncio.to_thread(
                sandbox_manager.wait_for_container_exit, old_container_id,
            )
            if not exited:
                # The kill acked but the container won't die — leaving the row
                # "running" would show a live tile for a thread that can never
                # heartbeat again. Stamp it failed so the surface is honest.
                session = get_session()
                try:
                    old = session.get(Thread, thread_id)
                    if old is not None and old.status not in (
                            "completed", "failed", "stopped", "replaced"):
                        old.status = "failed"
                        old.finished_at = datetime.now(UTC)
                        session.commit()
                finally:
                    session.close()
                await self.relay.publish_thread_status(run_id, thread_id, "failed")
                raise ValueError(
                    f"old container {old_container_id[:12]} survived kill+force-stop; "
                    "aborting replace to avoid a double-mounted session volume")
        # Verified dead — now the terminal stamp, status fanout, and cleanup.
        session = get_session()
        try:
            old = session.get(Thread, thread_id)
            if old is not None:
                old.status = "replaced"
                old.finished_at = datetime.now(UTC)
                session.commit()
        finally:
            session.close()
        await self.relay.publish_thread_status(run_id, thread_id, "replaced")
        await self._resolve_pending_approvals(run_id, "stopped", thread_id=thread_id)
        # F1/F5: settle the OLD thread's spend and release/clear its key
        # (previously leaked — "replaced" wasn't in any cleanup list), then
        # carry the REMAINING budget to the replacement so repeated replaces
        # can't silently multiply the run's effective budget.
        await self._cleanup_terminal(thread_id)
        session = get_session()
        try:
            old = session.get(Thread, thread_id)
            remaining_budget = max(
                0.25, (old.budget_usd or 0.0) - (old.cost_usd or 0.0)
            ) if old else None
        finally:
            session.close()

        # Resolve the unioned names to Repo rows for the spawn call. The
        # writable target is repo_scope (the run's primary repo); the rest of
        # the union is read-only context. An unknown name here is a caller bug
        # (the intent path validates against the fleet before calling) —
        # skip it rather than crash the replace.
        repo = None
        context_repos: list = []
        if stored_names:
            session = get_session()
            try:
                from app.db.models.repo import Repo
                rows = (session.query(Repo)
                        .filter(Repo.name.in_(stored_names)).all())
                by_name = {r.name: r for r in rows}
                if repo_scope and repo_scope in by_name:
                    repo = by_name[repo_scope]
                # Preserve stored order (target first); drop unknowns.
                context_repos = [by_name[n] for n in stored_names if n in by_name]
                if repo is None and context_repos:
                    repo = context_repos[0]
            finally:
                session.close()
        replacement = await self.thread_manager.spawn(
            run, persona=persona,
            prompt=context.get("prompt", "Resume the thread's work."),
            persona_prompt=context.get("persona_prompt", ""),
            writable_repo=repo, context_repos=context_repos,
            resume_from_thread_id=thread_id,
            # A replacement keeps the original lane's model — a kill/replace
            # must never silently switch models mid-conversation.
            model=context.get("model"),
            reasoning=context.get("reasoning"),
            # K19: carry the workspace-preservation intent across the replace —
            # the spawn_context stored it, but the replay never read it, so a
            # "keep my uncommitted work" replace still re-stamped fresh.
            preserve_workspace=bool(context.get("preserve_workspace")),
            budget_usd=remaining_budget,
        )
        await self.relay.publish_thread_status(run_id, replacement.id, "running")
        return replacement

    async def remount_thread(
        self, run_id: str, thread_id: str,
        extra_repo_names: list[str],
    ) -> Thread:
        """Turn-X @mention expansion: kill+replace the thread, unioning the
        newly-mentioned repos into the mount set, then wait for the replacement
        to heartbeat before the caller nudges it. Docker can't add mounts to a
        running container, so a replace-with-resume is the only mechanism —
        the cost is a container restart per newly-mentioned repo.

        Returns the replacement thread (the caller nudges it)."""
        return await self.kill_replace_thread(
            run_id, thread_id, extra_context_repo_names=extra_repo_names)

    async def _wait_for_heartbeat(
        self, thread_id: str, timeout_s: float = 20.0,
        poll_s: float = 0.5,
    ) -> bool:
        """Poll Redis for the worker's readiness signal (``thread:{id}:heartbeat``,
        set at runner startup) so a nudge sent into a still-booting worker isn't
        lost. Returns True once the key appears, False on timeout — the caller
        nudges either way (a lost nudge is recoverable; a hung replace is not)."""
        from app.core.redis_factory import make_redis
        try:
            redis = make_redis()
            key = f"thread:{thread_id}:heartbeat"
            waited = 0.0
            while waited < timeout_s:
                if await redis.get(key):
                    await redis.aclose()
                    return True
                await asyncio.sleep(poll_s)
                waited += poll_s
            await redis.aclose()
            return False
        except Exception:
            # A Redis failure during the wait is not fatal — the nudge still
            # goes out; the worker picks it up when it subscribes.
            return False

    # ------------------------------------------------------------ plan HITL chains
    async def continue_to_development(self, run_id: str) -> None:
        """approve_plan chain: a run that approved its plan continues
        into the development blueprint. The run keeps its mode; the development
        blueprint loads the approved Plan + steps from the DB by run_id."""
        await self._run_blueprint(run_id, "development")

    async def replan(self, run_id: str, notes: str = "") -> None:
        """reject_plan chain: roll back to the plan blueprint for a fresh planner
        pass, with the critic's notes injected as task context (a Lead nudge)."""
        await self._run_blueprint(run_id, "plan", extra_artifacts={"critic_notes": notes})

    async def start_plan(self, run_id: str) -> None:
        """start_plan intent: promote a debug run's proposed fix into a plan.
        Loads the debug run's latest draft Plan and chains into the plan blueprint
        with it as a ``seed_plan`` — the planner thread is skipped (the debug proposal
        IS the draft) and the critic verifies it fresh. The run keeps its id; its
        mode stays ``debug`` but the plan blueprint runs to produce an approvable
        Plan row."""
        seed = self._latest_draft_plan(run_id)
        await self._run_blueprint(run_id, "plan",
                                  extra_artifacts={"seed_plan": seed, "critic_notes": "promoted from debug run"})

    def _latest_draft_plan(self, run_id: str) -> dict | None:
        from app.db.models.run import Plan
        session = get_session()
        try:
            plan = (
                session.query(Plan).filter_by(run_id=run_id)
                .order_by(Plan.created_at.desc(), Plan.id.desc()).first()
            )
            return plan.structured if plan is not None else None
        finally:
            session.close()

    async def _run_blueprint(self, run_id: str, blueprint_mode: str,
                             extra_artifacts: dict | None = None) -> None:
        session = get_session()
        try:
            run = session.get(Run, run_id)
            task = run.title
            repo = run.repo
        finally:
            session.close()
        blueprint = blueprint_for(blueprint_mode)
        ctx = BlueprintContext(
            run=run,
            services={"thread_manager": self.thread_manager, "relay": self.relay,
                      "control": self.control},
            artifacts={"task": task, "repo": repo, **(extra_artifacts or {})},
        )
        self._track(run_id, self._guarded_execute(run_id, ctx, blueprint))

    async def switch_mode(self, run_id: str, mode_name: str) -> None:
        """Mid-session mode switch: validate the Mode row is
        enabled, set run.mode, and relay. Deliberately does NOT touch
        in-flight work — the switch takes effect on the next send_message,
        which chains the new blueprint (respawning the thread on the prior
        session volume) instead of nudging the old thread."""
        session = get_session()
        try:
            mode = session.query(Mode).filter_by(name=mode_name, enabled=True).one_or_none()
            if mode is None:
                raise ValueError(f"unknown or disabled mode '{mode_name}'")
            run = session.get(Run, run_id)
            if run is None:
                raise ValueError("run not found")
            run.mode = mode_name
            session.commit()
        finally:
            session.close()

    # ------------------------------------------------------------ delivery HITL
    async def create_pr(self, run_id: str) -> None:
        """create_pr intent: open the evidence-gated PR, then stage the
        run at pr_ready (available_actions = review_diff + merge_pr). The thread
        workspace path is the develop thread's stamped clone, persisted on
        Run.session_volume_path when the develop thread started (deterministic
        fallback: workspaces_dir/run_id/<repo>)."""
        from app.core.config import get_settings
        from app.services import delivery
        session = get_session()
        try:
            run = session.get(Run, run_id)
            repo_name = run.repo
            workspace = run.session_volume_path or str(
                get_settings().workspaces_dir / run_id / (repo_name or ""))
        finally:
            session.close()
        link = await delivery.open_pr(run_id, repo_name, workspace)
        session = get_session()
        try:
            run = session.get(Run, run_id)
            transition(run, RunStage.PR_READY)
            session.commit()
            available = run.available_actions
        finally:
            session.close()
        await self.relay.publish_run_stage(run_id, RunStage.PR_READY.value, available)
        return link

    async def merge_pr(self, run_id: str, user_id: int) -> str | None:
        """merge_pr intent: the human's approval is the approval of record; the
        service account completes — OR, under merge_native_ui, the run stays
        pr_ready and the human completes in ADO's own UI (handoff URL returned).
        Stage completed only on a real merge."""
        from app.services import delivery
        result = await delivery.merge_pr(run_id, user_id)
        if result["handoff_url"]:
            return result["handoff_url"]
        session = get_session()
        try:
            run = session.get(Run, run_id)
            transition(run, RunStage.COMPLETED)
            run.finished_at = datetime.now(UTC)
            session.commit()
        finally:
            session.close()
        await self.relay.publish_run_stage(run_id, RunStage.COMPLETED.value, [])
        return None

    # -------------------------------------------------------- reconciliation

    async def reconcile_on_boot(self) -> int:
        """Boot-time sweep: every run whose threads are REALLY gone is marked
        interrupted with an Inbox card offering resume — no silent zombies.

        Liveness-aware (E1/E2): a run is swept only when NO thread shows a
        fresh heartbeat AND a live container — a healthy run must survive a
        backend restart. VERIFYING runs are human-parked by definition and
        are never swept."""
        session = get_session()
        reconciled: list[tuple[str, list[str], list[str], list[str]]] = []
        try:
            active = session.query(Run).filter(
                Run.stage.in_([RunStage.QUEUED.value,  # H-40: include QUEUED
                               RunStage.PROVISIONING.value, RunStage.INVESTIGATING.value,
                               RunStage.PLANNING.value, RunStage.DEVELOPING.value,
                               RunStage.VERIFYING.value])
            ).all()
            now = datetime.now(UTC)
            for run in active:
                # E2: VERIFYING means the work is done and a HUMAN is
                # reviewing — sweeping it would interrupt a parked run.
                if run.stage == RunStage.VERIFYING.value:
                    continue
                threads = session.query(Thread).filter_by(run_id=run.id).all()
                live_threads = [t for t in threads
                                if t.status in ("running", "idle", "queued",
                                                "input_required")]
                # E1: a thread with a fresh heartbeat is alive even while the
                # backend was down — its run is healthy, skip the sweep.
                def _fresh(t) -> bool:
                    # heartbeat_at round-trips tz-naive from Postgres; coerce
                    # before arithmetic against aware now.
                    return (t.heartbeat_at is not None
                            and (now - aware_utc(t.heartbeat_at)).total_seconds() < 180)
                # E1: a run with any sign of life — a fresh thread heartbeat
                # OR a still-running container — is healthy; the sweep must
                # not touch it (backend restart mid-work is not a zombie).
                container_ids = [t.container_id for t in live_threads if t.container_id]
                containers_running = False
                for cid in container_ids:
                    if await asyncio.to_thread(sandbox_manager.container_running, cid):
                        containers_running = True
                        break
                if any(_fresh(t) for t in live_threads) or containers_running:
                    # D4: the run is ALIVE but this process just booted — its
                    # ingest stream and spawn bridge are in-memory registries,
                    # empty after a restart. Re-register or the surviving
                    # workers' events pile up unconsumed (the pre-fix silent
                    # stall).
                    self.ingest.register_run(run.id)
                    if self.approvals is not None:
                        self.approvals.register_run(run.id)
                    if getattr(self, "spawn_bridge", None) is not None:
                        self.spawn_bridge.register_run(run.id)
                    continue
                # Genuinely dead: stop any leftover containers for real so the
                # sweep isn't a DB-only fiction and session volumes are free.
                for cid in container_ids:
                    await asyncio.to_thread(
                        sandbox_manager.wait_for_container_exit, cid)
                thread_ids = []
                for t in threads:
                    if t.status in ("running", "idle", "queued", "input_required"):
                        t.status = "stopped"
                        t.finished_at = now
                    thread_ids.append(t.id)
                transition(run, RunStage.INTERRUPTED)
                reconciled.append((run.id, list(run.available_actions),
                                   thread_ids, container_ids))
            session.commit()
        finally:
            session.close()
        # H-40/H-36: release keys for stopped threads and publish the stage
        # change so the UI reflects INTERRUPTED (the old code never published).
        for run_id, available, thread_ids, _container_ids in reconciled:
            for tid in thread_ids:
                # F1/F3: settle spend and clear the secret too, not just
                # release — a boot-reconciled thread's cost used to vanish.
                await self._cleanup_terminal(tid)
            await self.relay.publish_run_stage(
                run_id, RunStage.INTERRUPTED.value, available)
        return len(reconciled)
