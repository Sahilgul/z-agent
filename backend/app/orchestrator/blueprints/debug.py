"""Debug mode blueprint: hydrate -> reproduce -> diagnose -> propose -> present.

hydrate    (deterministic): resolve the repo (read-only), load the failing repro
             signal from the run title / ADO work item.
reproduce  (deterministic): run the repro (the repo's test suite) to CONFIRM the
             failure — the control plane runs it, so the failure signal is tamper-proof.
diagnose   (agentic debug thread, read-only): investigate the root cause from the
             repro signal with live-grep ground truth.
propose    (agentic fix-proposal thread): propose a structured fix (contracts.Plan JSON).
present    (deterministic): parse+validate the proposal, persist a draft Plan + steps,
             and stage the run at awaiting_user with available_actions = review_plan +
             start_plan (the debug-specific promotion path — start_plan chains into the
             plan blueprint with the proposal as a seed_plan).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from zagent_contracts import RunStage

from app.db.base import get_session
from app.db.models.event import Event
from app.db.models.thread import Thread
from app.db.models.mode import Mode
from app.db.models.repo import Repo
from app.db.models.run import Plan, PlanStep, Run
from app.orchestrator.blueprints.base import Blueprint, BlueprintContext, Node
from app.services import evidence
from app.services.runs import transition

PROPOSAL_SCHEMA_HINT = (
    "\n\nStructured output contract: respond with a contracts.Plan JSON object — "
    '{"schema_version":1,"title","summary","steps":[{"index","title","description",'
    '"repo","files","success_criterion","status"}],"blast_radius","risks",'
    '"evidence_contract"}.'
)


class DebugBlueprint(Blueprint):
    name = "debug"

    def nodes(self) -> list[Node]:
        return [
            Node("hydrate", self._hydrate, deterministic=True, stage=RunStage.PROVISIONING),
            Node("reproduce", self._reproduce, deterministic=True, stage=RunStage.INVESTIGATING),
            Node("diagnose", self._diagnose, deterministic=False, stage=RunStage.INVESTIGATING),
            Node("propose", self._propose, deterministic=False, stage=RunStage.PLANNING),
            # present runs its own transition so it can set the debug-specific
            # available_actions (review_plan + start_plan) and publish once.
            Node("present", self._present, deterministic=True, stage=None),
        ]

    # --------------------------------------------------------------- hydrate
    async def _hydrate(self, ctx: BlueprintContext) -> None:
        session = get_session()
        try:
            run = session.get(Run, ctx.run.id)
            repo_name = (run.repo if run else None) or ctx.artifacts.get("repo") or "ServerApp"
            repo = session.query(Repo).filter_by(name=repo_name).one_or_none()
            if repo is None:
                raise RuntimeError(f"repo '{repo_name}' not registered")
            # Read the profile's test_cmds while the session is open (profile is
            # a lazy relationship; reproduce runs after this session closes).
            test_cmds = list(repo.profile.test_cmds) if repo.profile and repo.profile.test_cmds else None
            # The repro signal is the run title (or the ADO work item title when present).
            repro = run.title or ctx.artifacts.get("task") or "failing test"
            ctx.artifacts["repo_row"] = repo
            ctx.artifacts["repro_signal"] = repro
            ctx.artifacts["test_cmds"] = test_cmds
            ctx.run = run
        finally:
            session.close()

    # --------------------------------------------------------------- reproduce
    async def _reproduce(self, ctx: BlueprintContext) -> None:
        """Run the repo's tests deterministically to CONFIRM the failure. The
        control plane runs them — the agent never self-reports the repro. Debug
        stamps no writable clone, so the repro runs against the golden repo
        (the same tree the read-only threads mount); the result is persisted as a
        test_run event so the evidence trail keeps the repro signal."""
        repo: Repo = ctx.artifacts["repo_row"]
        workspace = ctx.artifacts.get("workspace") or str(_golden_root() / repo.name)
        signal = await evidence.run_test_commands(workspace, repo.name,
                                                  ctx.artifacts.get("test_cmds"))
        ctx.artifacts["repro_result"] = signal
        ctx.artifacts["failure_confirmed"] = not signal["passed"]
        session = get_session()
        try:
            # "control-plane" pseudo-thread: no agent thread exists at repro time and
            # events.thread_id is NOT NULL — the control plane owns this event.
            session.add(Event(
                run_id=ctx.run.id, thread_id="control-plane", seq=0,
                type="test_run",
                title=f"repro: tests {'passed' if signal['passed'] else 'failed'}",
                payload={"passed": signal["passed"], "repo": repo.name,
                         "returncode": signal["returncode"],
                         "stdout": signal["stdout"], "stderr": signal["stderr"]},
            ))
            session.commit()
        finally:
            session.close()

    # --------------------------------------------------------------- diagnose
    async def _diagnose(self, ctx: BlueprintContext) -> None:
        thread_manager = ctx.services["thread_manager"]
        repo: Repo = ctx.artifacts["repo_row"]
        repro = ctx.artifacts.get("repro_signal", "")
        confirmed = ctx.artifacts.get("failure_confirmed")
        prompt = (f"Repro signal: {repro}\nFailure confirmed by control plane: {confirmed}.\n"
                  "Investigate the root cause with read-only grep on the mounted repo. "
                  "Report findings with file:line evidence.")
        persona_prompt = self._persona(ctx, "You are the DEBUGGER. Reproduce-first: confirm "
                                          "the failure, then isolate the root cause.")
        thread = await thread_manager.spawn(
            ctx.run, persona="debugger", prompt=prompt, persona_prompt=persona_prompt,
            writable_repo=None, context_repos=[repo],
            resume_from_thread_id=ctx.artifacts.get("resume_from_thread_id"),
        )
        ctx.artifacts["diagnose_thread_id"] = thread.id
        await self._await_thread(thread.id)
        ctx.artifacts["diagnosis"] = self._last_message_text(thread.id) or ""
        # Lint the diagnosis's file:line citations against the golden repo (same
        # drift check plan mode applies) — stale citations get flagged, never crash.
        from app.orchestrator.blueprints.plan import PlanBlueprint
        citations = PlanBlueprint._collect_citations(
            {"summary": ctx.artifacts["diagnosis"], "title": "", "steps": [], "risks": []})
        lint_report = PlanBlueprint._lint_citations(repo.name, citations)
        if lint_report:
            ctx.artifacts["diagnosis_lint"] = lint_report

    # --------------------------------------------------------------- propose
    async def _propose(self, ctx: BlueprintContext) -> None:
        thread_manager = ctx.services["thread_manager"]
        repo: Repo = ctx.artifacts["repo_row"]
        diagnosis = ctx.artifacts.get("diagnosis", "")
        prompt = (f"Root-cause diagnosis:\n{diagnosis}\n\nPropose a fix as a structured "
                  "Plan (one or two steps) that the human can promote to a plan run.")
        persona_prompt = self._persona(ctx, "You are the FIX PROPOSER. Propose the minimal "
                                            "fix." + PROPOSAL_SCHEMA_HINT)
        thread = await thread_manager.spawn(
            ctx.run, persona="fixer", prompt=prompt, persona_prompt=persona_prompt,
            writable_repo=None, context_repos=[repo],
        )
        ctx.artifacts["propose_thread_id"] = thread.id
        await self._await_thread(thread.id)
        ctx.artifacts["proposal_text"] = self._last_message_text(thread.id) or ""

    # --------------------------------------------------------------- present
    async def _present(self, ctx: BlueprintContext) -> None:
        proposal = self._parse_json(ctx.artifacts.get("proposal_text") or "")
        if proposal is None:
            raise RuntimeError("debug proposal did not produce parseable Plan JSON")
        from zagent_contracts import Plan as PlanContract
        plan_contract = PlanContract.model_validate(proposal)
        structured = dict(proposal)
        structured["diagnosis"] = ctx.artifacts.get("diagnosis", "")
        structured["failure_confirmed"] = ctx.artifacts.get("failure_confirmed", False)
        if ctx.artifacts.get("diagnosis_lint"):
            structured["diagnosis_lint"] = ctx.artifacts["diagnosis_lint"]
        relay = ctx.services.get("relay")
        session = get_session()
        try:
            run = session.get(Run, ctx.run.id)
            plan = Plan(run_id=run.id, structured=structured, status="draft")
            session.add(plan)
            session.flush()
            for step in plan_contract.steps:
                session.add(PlanStep(
                    plan_id=plan.id, index=step.index, title=step.title,
                    description=step.description, repo=step.repo, files=step.files,
                    success_criterion=step.success_criterion, status="pending",
                ))
            transition(run, RunStage.AWAITING_USER)
            # Handoff summary: the inbox card shows the diagnosis, not
            # a blank run — auto_summary is the human's first read of the debug.
            diagnosis = (ctx.artifacts.get("diagnosis") or "").strip()
            confirmed = ctx.artifacts.get("failure_confirmed")
            prefix = "Failure reproduced. " if confirmed else "Failure NOT reproduced. "
            run.auto_summary = (prefix + diagnosis)[:500]
            # Debug-specific promotion surface: review the proposed fix, or start_plan
            # to promote it into a plan run (the plan blueprint runs with this as the seed).
            run.available_actions = ["review_plan", "start_plan"]
            run.last_active_at = datetime.now(timezone.utc)
            session.commit()
            ctx.run = run
        finally:
            session.close()
        if relay:
            await relay.publish_run_stage(run.id, RunStage.AWAITING_USER.value,
                                            run.available_actions)

    # --------------------------------------------------------------- helpers
    def _persona(self, ctx: BlueprintContext, fallback: str) -> str:
        session = get_session()
        try:
            mode = session.query(Mode).filter_by(name=ctx.run.mode).one_or_none()
            base = (mode.persona_prompt if mode else "") or ""
        finally:
            session.close()
        from app.services.playbooks import playbooks_prompt_for_mode
        playbooks = playbooks_prompt_for_mode(ctx.run.mode)
        parts = [p for p in (base, playbooks, fallback) if p]
        return "\n\n".join(parts).strip()

    def _last_message_text(self, thread_id: str) -> str | None:
        session = get_session()
        try:
            row = (
                session.query(Event)
                .filter_by(thread_id=thread_id, type="message")
                .order_by(Event.seq.desc())
                .first()
            )
            if row is None:
                return None
            payload = row.payload or {}
            return payload.get("text") if isinstance(payload, dict) else None
        finally:
            session.close()

    @staticmethod
    def _parse_json(text: str | None) -> dict | None:
        if not text:
            return None
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end < start:
            return None
        try:
            obj = json.loads(text[start:end + 1])
            return obj if isinstance(obj, dict) else None
        except json.JSONDecodeError:
            return None

    async def _await_thread(self, thread_id: str, poll_seconds: float = 2.0) -> None:
        import asyncio
        while True:
            session = get_session()
            try:
                thread = session.get(Thread, thread_id)
                status = thread.status if thread else "failed"
            finally:
                session.close()
            if status in ("idle", "completed", "failed", "stopped"):
                return
            await asyncio.sleep(poll_seconds)


def _golden_root():
    from app.core.config import get_settings
    return get_settings().golden_dir
