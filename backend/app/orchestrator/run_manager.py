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
from datetime import datetime, timezone
from typing import Any

from zagent_contracts import RunStage

from app.core.logging import get_logger
from app.db.base import get_session
from app.db.models.event import Event
from app.db.models.thread import Thread
from app.db.models.mode import Mode
from app.db.models.run import Run
from app.events.bus import IngestConsumer
from app.events.control import LaneControl
from app.events.relay import Relay
from app.orchestrator.blueprints.base import BlueprintContext
from app.orchestrator.thread_manager import ThreadManager
from app.orchestrator.mode_engine import blueprint_for
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
                 control: LaneControl, approvals: Any | None = None) -> None:
        self.ingest = ingest
        self.relay = relay
        self.thread_manager = thread_manager
        self.control = control
        # The ApprovalService consuming approvals:{run_id}. Optional so unit
        # tests can construct a bare manager; production wires it in main.py —
        # without it the approvals consumer idles on an empty stream set and
        # no approval card is ever created.
        self.approvals = approvals
        self._tasks: dict[str, asyncio.Task] = {}

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
                         fanout: int | None = None, delivery_id: int | None = None) -> Run:
        # Deterministic title hydration (THE one place it happens):
        # a generic typed title resolves from the ADO work item so the inbox
        # card reads the ticket's real title, not "42" or an empty string.
        if work_item_id is not None and _title_is_generic(task, work_item_id):
            from app.services import hydration
            task = await hydration.hydrate_title(work_item_id, task) or task
        session = get_session()
        try:
            mode = session.query(Mode).filter_by(name=mode_name, enabled=True).one_or_none()
            if mode is None:
                raise ValueError(f"unknown or disabled mode '{mode_name}'")
            run = Run(
                id=str(uuid.uuid4()), created_by=initiated_by, source=source,
                mode=mode_name, autonomy=autonomy or mode.autonomy_default,
                title=task[:256], repo=repo, work_item_id=work_item_id,
                delivery_id=delivery_id,
                started_at=datetime.now(timezone.utc),
            )
            transition(run, RunStage.QUEUED)
            session.add(run)
            session.commit()
        finally:
            session.close()

        self.ingest.register_run(run.id)
        if self.approvals is not None:
            self.approvals.register_run(run.id)
        await self.relay.publish_run_stage(run.id, run.stage, run.available_actions)
        self._track(run.id, self._execute(run.id, task, repo, fanout))
        return run

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
            run = session.get(Run, run_id)
            if run is None or run.created_by != initiated_by:
                return None
            last_thread = (session.query(Thread)
                          .filter_by(run_id=run_id)
                          .order_by(Thread.created_at.desc())
                          .first())
            last_thread_id = last_thread.id if last_thread is not None else None
            transition(run, RunStage.QUEUED, allow_terminal_exit=True)  # H-41
            run.finished_at = None
            session.commit()
        finally:
            session.close()
        self.ingest.register_run(run_id)
        if self.approvals is not None:
            self.approvals.register_run(run_id)
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
                    row.finished_at = datetime.now(timezone.utc)
                    session.commit()
            finally:
                session.close()
            if row.stage == RunStage.FAILED.value:
                await self.relay.publish_run_stage(run_id, RunStage.FAILED.value, [])

    # ------------------------------------------------------------ lifecycle

    async def stop_run(self, run_id: str) -> None:
        """One tap, no confirmation — stopping is safe and reversible. Full trace
        retained; banner: 'Stopped by you — all work preserved.'"""
        now = datetime.now(timezone.utc)
        session = get_session()
        try:
            run = session.get(Run, run_id)
            if run.stage in TERMINAL_STAGES:
                return  # H-41: don't resurrect a terminal run to INTERRUPTED
            threads = session.query(Thread).filter_by(run_id=run_id).all()
            thread_ids: list[str] = []
            for l in threads:
                if l.status in ("running", "idle", "queued"):
                    thread_ids.append(l.id)
                    # Write the Thread DB status so the capacity semaphore
                    # releases the slot. The relay-only "stopped" publish
                    # left the row at "running" and the slot leaked forever
                    # (C-15) — stop_thread already did this, stop_run didn't.
                    l.status = "stopped"
                    l.finished_at = now
            transition(run, RunStage.INTERRUPTED)
            session.commit()
            available = run.available_actions
        finally:
            session.close()
        for thread_id in thread_ids:
            await self.control.interrupt(thread_id)
            await self.relay.publish_thread_status(run_id, thread_id, "stopped")
            # H-36: release the minted gateway key for each stopped thread.
            await self.thread_manager.release_key(thread_id)
        task = self._tasks.get(run_id)
        if task and not task.done():
            task.cancel()
        await self.relay.publish_run_stage(run_id, RunStage.INTERRUPTED.value, available)

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
            run.finished_at = datetime.now(timezone.utc)
            session.commit()
        finally:
            session.close()
        for thread_id in thread_ids:
            await self.control.kill(thread_id)
        for container_id in container_ids:
            await asyncio.to_thread(sandbox_manager.stop_container, container_id)
        task = self._tasks.get(run_id)
        if task and not task.done():
            task.cancel()
        await asyncio.to_thread(sandbox_manager.shred_workspace, run_id)
        self.ingest.unregister_run(run_id)
        if self.approvals is not None:
            self.approvals.unregister_run(run_id)
        await self.relay.publish_run_stage(run_id, RunStage.ABANDONED.value, [])

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
        nudgeable = ACTIVE_STATUSES + ("input_required",)
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
        """Per-thread stop from the swarm view: immediate interrupt, trace kept,
        the rest of the swarm runs on. Safe + reversible — no confirmation."""
        await self.control.interrupt(thread_id)
        session = get_session()
        try:
            thread = session.get(Thread, thread_id)
            if thread and thread.run_id == run_id:
                thread.status = "stopped"
                thread.finished_at = datetime.now(timezone.utc)
                session.commit()
        finally:
            session.close()
        await self.relay.publish_thread_status(run_id, thread_id, "stopped")
        # H-36: release the minted gateway key for the stopped thread.
        await self.thread_manager.release_key(thread_id)

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
        await self.relay.publish_thread_status(run_id, thread_id, "pinned")

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
            # Mount set = stored context_repos (names) ∪ extras. The stored
            # snapshot is the source of truth — re-deriving from the blueprint
            # would re-resolve @mentions against a possibly-edited task and
            # silently drop a repo the user added mid-conversation.
            stored_names: list[str] = list(context.get("context_repos") or [])
            if repo_scope and repo_scope not in stored_names:
                stored_names.insert(0, repo_scope)
            for name in (extra_context_repo_names or []):
                if name not in stored_names:
                    stored_names.append(name)
            thread.status = "replaced"
            thread.finished_at = datetime.now(timezone.utc)
            run = session.get(Run, run_id)
            session.commit()
        finally:
            session.close()
        await self.control.kill(thread_id)
        await self.relay.publish_thread_status(run_id, thread_id, "replaced")
        # H-37: WAIT for the old container to actually die before spawning
        # the replacement. The old control.kill just published a kill message
        # and returned; the replacement then mounted the old session volume
        # (resume_from_thread_id) while the old container was still alive and
        # writing to it — two containers on one session volume = corruption.
        # Poll the old container until it's gone (or timeout) before spawning.
        if old_container_id:
            await asyncio.to_thread(
                sandbox_manager.wait_for_container_exit, old_container_id,
            )

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
        from app.services import delivery
        from app.core.config import get_settings
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
            run.finished_at = datetime.now(timezone.utc)
            session.commit()
        finally:
            session.close()
        await self.relay.publish_run_stage(run_id, RunStage.COMPLETED.value, [])
        return None

    # -------------------------------------------------------- reconciliation

    async def reconcile_on_boot(self) -> int:
        """Boot-time sweep: every run whose threads are gone is marked
        interrupted with an Inbox card offering resume — no silent zombies."""
        session = get_session()
        reconciled: list[tuple[str, list[str], list[str]]] = []
        try:
            active = session.query(Run).filter(
                Run.stage.in_([RunStage.QUEUED.value,  # H-40: include QUEUED
                               RunStage.PROVISIONING.value, RunStage.INVESTIGATING.value,
                               RunStage.PLANNING.value, RunStage.DEVELOPING.value,
                               RunStage.VERIFYING.value])
            ).all()
            now = datetime.now(timezone.utc)
            for run in active:
                # H-40: mark the run's threads stopped so the capacity
                # semaphore releases their slots. The old code only
                # transitioned the RUN, leaving threads "running"/"queued"
                # -> zombie threads + capacity leak. Collect their ids for
                # key release (H-36).
                threads = session.query(Thread).filter_by(run_id=run.id).all()
                thread_ids = []
                for t in threads:
                    if t.status in ("running", "idle", "queued"):
                        t.status = "stopped"
                        t.finished_at = now
                    thread_ids.append(t.id)
                transition(run, RunStage.INTERRUPTED)
                reconciled.append((run.id, list(run.available_actions), thread_ids))
            session.commit()
        finally:
            session.close()
        # H-40/H-36: release keys for stopped threads and publish the stage
        # change so the UI reflects INTERRUPTED (the old code never published).
        for run_id, available, thread_ids in reconciled:
            for tid in thread_ids:
                await self.thread_manager.release_key(tid)
            await self.relay.publish_run_stage(
                run_id, RunStage.INTERRUPTED.value, available)
        return len(reconciled)
