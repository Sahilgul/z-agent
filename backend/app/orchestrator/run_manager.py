"""Run manager — the ONE run-creation path (plan §8 standing rule).

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
from datetime import datetime, timezone

from zagent_contracts import RunStage

from app.core.logging import get_logger
from app.db.base import get_session
from app.db.models.event import Event
from app.db.models.lane import Lane
from app.db.models.mode import Mode
from app.db.models.run import Run
from app.events.bus import IngestConsumer
from app.events.control import LaneControl
from app.events.relay import Relay
from app.orchestrator.blueprints.base import BlueprintContext
from app.orchestrator.lane_manager import LaneManager
from app.orchestrator.mode_engine import blueprint_for
from app.sandbox.manager import sandbox_manager
from app.services.runs import transition

log = get_logger(service="run_manager")


def _title_is_generic(task: str, work_item_id: int) -> bool:
    """A title is generic when the user tapped a ticket without typing (empty,
    the bare work-item id, or a couple of characters). Real typed titles are
    never overridden."""
    stripped = (task or "").strip()
    return len(stripped) < 4 or stripped == str(work_item_id)


class RunManager:
    def __init__(self, ingest: IngestConsumer, relay: Relay, lane_manager: LaneManager,
                 control: LaneControl) -> None:
        self.ingest = ingest
        self.relay = relay
        self.lane_manager = lane_manager
        self.control = control
        self._tasks: dict[str, asyncio.Task] = {}

    # ------------------------------------------------------------ creation

    async def create_run(self, source: str, initiated_by: int, mode_name: str,
                         task: str, repo: str | None = None,
                         work_item_id: int | None = None, autonomy: str | None = None,
                         fanout: int | None = None, delivery_id: int | None = None) -> Run:
        # Deterministic title hydration (plan §2/WU5, THE one place it happens):
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
        await self.relay.publish_run_stage(run.id, run.stage, run.available_actions)
        self._tasks[run.id] = asyncio.create_task(self._execute(run.id, task, repo, fanout))
        return run

    async def _execute(self, run_id: str, task: str, repo: str | None,
                       fanout: int | None = None) -> None:
        session = get_session()
        try:
            run = session.get(Run, run_id)
        finally:
            session.close()
        blueprint = blueprint_for(run.mode)
        ctx = BlueprintContext(
            run=run,
            services={"lane_manager": self.lane_manager, "relay": self.relay,
                      "control": self.control},
            artifacts={"task": task, "repo": repo,
                       **({"fanout": fanout} if fanout is not None else {})},
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
            session = get_session()
            try:
                row = session.get(Run, run_id)
                transition(row, RunStage.FAILED)
                row.finished_at = datetime.now(timezone.utc)
                session.commit()
            finally:
                session.close()
            await self.relay.publish_run_stage(run_id, RunStage.FAILED.value, [])

    # ------------------------------------------------------------ lifecycle

    async def stop_run(self, run_id: str) -> None:
        """One tap, no confirmation — stopping is safe and reversible. Full trace
        retained; banner: 'Stopped by you — all work preserved.'"""
        session = get_session()
        try:
            run = session.get(Run, run_id)
            lanes = session.query(Lane).filter_by(run_id=run_id).all()
            lane_ids = [l.id for l in lanes if l.status in ("running", "idle", "queued")]
            transition(run, RunStage.INTERRUPTED)
            session.commit()
            available = run.available_actions
        finally:
            session.close()
        for lane_id in lane_ids:
            await self.control.interrupt(lane_id)
            await self.relay.publish_lane_status(run_id, lane_id, "stopped")
        task = self._tasks.get(run_id)
        if task and not task.done():
            task.cancel()
        await self.relay.publish_run_stage(run_id, RunStage.INTERRUPTED.value, available)

    async def abandon_run(self, run_id: str) -> None:
        """Kill run, shred workspace. Separate overflow-menu action WITH
        confirmation — never confused with Stop."""
        session = get_session()
        try:
            lanes = session.query(Lane).filter_by(run_id=run_id).all()
            lane_ids = [l.id for l in lanes]
            container_ids = [l.container_id for l in lanes if l.container_id]
            run = session.get(Run, run_id)
            transition(run, RunStage.ABANDONED)
            run.finished_at = datetime.now(timezone.utc)
            session.commit()
        finally:
            session.close()
        for lane_id in lane_ids:
            await self.control.kill(lane_id)
        for container_id in container_ids:
            await asyncio.to_thread(sandbox_manager.stop_container, container_id)
        task = self._tasks.get(run_id)
        if task and not task.done():
            task.cancel()
        await asyncio.to_thread(sandbox_manager.shred_workspace, run_id)
        self.ingest.unregister_run(run_id)
        await self.relay.publish_run_stage(run_id, RunStage.ABANDONED.value, [])

    async def nudge_lane(self, run_id: str, lane_id: str, text: str) -> None:
        """Typed Lead-nudge: stays enabled while the agent works (§1a carve-out).
        Worker semantics: graceful interrupt + inject + resume."""
        await self.control.nudge(lane_id, text)
        session = get_session()
        try:
            lane = session.get(Lane, lane_id)
            if lane:
                lane.status = "running"
                session.commit()
        finally:
            session.close()
        await self.relay.publish_lane_status(run_id, lane_id, "running")

    # ------------------------------------------------------------ lane controls (§4)
    async def stop_lane(self, run_id: str, lane_id: str) -> None:
        """Per-lane stop from the swarm view: immediate interrupt, trace kept,
        the rest of the swarm runs on. Safe + reversible — no confirmation."""
        await self.control.interrupt(lane_id)
        session = get_session()
        try:
            lane = session.get(Lane, lane_id)
            if lane and lane.run_id == run_id:
                lane.status = "stopped"
                lane.finished_at = datetime.now(timezone.utc)
                session.commit()
        finally:
            session.close()
        await self.relay.publish_lane_status(run_id, lane_id, "stopped")

    async def pin_finding(self, run_id: str, lane_id: str, note: str = "") -> None:
        """Pin a finding from a lane overlay (§4): lands as a run event the
        knowledge flywheel's approval inbox (Phase 3) picks up as a candidate."""
        session = get_session()
        try:
            lane = session.get(Lane, lane_id)
            if lane is None or lane.run_id != run_id:
                raise ValueError("lane not found in this run")
            session.add(Event(
                run_id=run_id, lane_id=lane_id, seq=lane.next_seq,
                type="pin", title=note[:200] or f"pinned finding from {lane.persona}",
                payload={"persona": lane.persona, "note": note},
            ))
            session.commit()
        finally:
            session.close()
        await self.relay.publish_lane_status(run_id, lane_id, "pinned")

    async def kill_replace_lane(self, run_id: str, lane_id: str) -> Lane:
        """Kill-and-replace (§4): the old lane dies; a FRESH lane spawns with the
        SAME spawn context (stored at spawn — never re-derived from the
        blueprint). The session volume survives the container, so the
        replacement resumes where the killed lane left off."""
        session = get_session()
        try:
            lane = session.get(Lane, lane_id)
            if lane is None or lane.run_id != run_id:
                raise ValueError("lane not found in this run")
            context = dict(lane.spawn_context or {})
            persona = lane.persona
            repo_scope = lane.repo_scope
            lane.status = "replaced"
            lane.finished_at = datetime.now(timezone.utc)
            run = session.get(Run, run_id)
            session.commit()
        finally:
            session.close()
        await self.control.kill(lane_id)
        await self.relay.publish_lane_status(run_id, lane_id, "replaced")

        repo = None
        if repo_scope:
            session = get_session()
            try:
                from app.db.models.repo import Repo
                repo = session.query(Repo).filter_by(name=repo_scope).one_or_none()
            finally:
                session.close()
        replacement = await self.lane_manager.spawn(
            run, persona=persona,
            prompt=context.get("prompt", "Resume the lane's work."),
            persona_prompt=context.get("persona_prompt", ""),
            writable_repo=repo, context_repos=[repo] if repo else [],
        )
        await self.relay.publish_lane_status(run_id, replacement.id, "running")
        return replacement

    # ------------------------------------------------------------ plan HITL chains
    async def continue_to_development(self, run_id: str) -> None:
        """approve_plan chain (plan §2/§9): a run that approved its plan continues
        into the development blueprint. The run keeps its mode; the development
        blueprint loads the approved Plan + steps from the DB by run_id."""
        await self._run_blueprint(run_id, "development")

    async def replan(self, run_id: str, notes: str = "") -> None:
        """reject_plan chain: roll back to the plan blueprint for a fresh planner
        pass, with the critic's notes injected as task context (a Lead nudge)."""
        await self._run_blueprint(run_id, "plan", extra_artifacts={"critic_notes": notes})

    async def start_plan(self, run_id: str) -> None:
        """start_plan intent (WU4): promote a debug run's proposed fix into a plan.
        Loads the debug run's latest draft Plan and chains into the plan blueprint
        with it as a ``seed_plan`` — the planner lane is skipped (the debug proposal
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
            services={"lane_manager": self.lane_manager, "relay": self.relay,
                      "control": self.control},
            artifacts={"task": task, "repo": repo, **(extra_artifacts or {})},
        )
        self._tasks[run_id] = asyncio.create_task(self._guarded_execute(run_id, ctx, blueprint))

    # ------------------------------------------------------------ delivery HITL
    async def create_pr(self, run_id: str) -> None:
        """create_pr intent (plan §9): open the evidence-gated PR, then stage the
        run at pr_ready (available_actions = review_diff + merge_pr). The lane
        workspace path is the develop lane's stamped clone, persisted on
        Run.session_volume_path when the develop lane started (deterministic
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
        """Boot-time sweep (plan §4): every run whose lanes are gone is marked
        interrupted with an Inbox card offering resume — no silent zombies."""
        session = get_session()
        try:
            active = session.query(Run).filter(
                Run.stage.in_([RunStage.PROVISIONING.value, RunStage.INVESTIGATING.value,
                               RunStage.PLANNING.value, RunStage.DEVELOPING.value,
                               RunStage.VERIFYING.value])
            ).all()
            count = 0
            for run in active:
                transition(run, RunStage.INTERRUPTED)
                count += 1
            session.commit()
            return count
        finally:
            session.close()
