"""Development mode blueprint: hydrate -> develop -> stamp -> evaluate.

hydrate   (deterministic): load the approved Plan + steps from the DB by run_id;
            resolve the repo + the mode's writable/repos scope (modes-as-data);
            stamp a writable clone path on Run.session_volume_path and compute the
            deterministic branch name.
develop   (agentic developer thread): one developer thread with the approved plan steps
            as the prompt and a writable clone when the mode permits; implements
            the steps. Plan steps roll forward pending -> in_progress -> done as the
            thread reports (deterministic fallback marks them done on thread completion).
stamp     (deterministic): the control plane runs the tests and drives Playwright MCP
            for screenshots (tamper-proof evidence), persists test_run/screenshot
            Event rows, and stages the run at verifying (available_actions =
            review_evidence + create_pr). Step statuses are NOT touched here.
evaluate  (agentic evaluator thread, FRESH context): a SEPARATE thread/session — never
            the develop thread's session — that re-reads the workspace from scratch
            and verifies each step's success_criterion AGAINST the stored stamp
            evidence (the test signal), never against developer self-report. Fresh
            context is the whole point: the evaluator cannot inherit the developer's
            rationalizations. Its verdict is the final authority on step status —
            it runs LAST so nothing can roll a "failed" step back to "done". The
            create_pr intent (run_manager) then opens the evidence-gated PR.
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
from app.db.models.trajectory import TrajectorySummary
from app.orchestrator.blueprints.base import Blueprint, BlueprintContext, Node
from app.services import delivery, evidence


class DevelopmentBlueprint(Blueprint):
    name = "development"

    def nodes(self) -> list[Node]:
        return [
            Node("hydrate", self._hydrate, deterministic=True, stage=RunStage.PROVISIONING),
            Node("develop", self._develop, deterministic=False, stage=RunStage.DEVELOPING),
            Node("stamp", self._stamp, deterministic=True, stage=RunStage.VERIFYING),
            # evaluate runs LAST (stage stays verifying): its verdict is the final
            # authority on step status — no later node may roll "failed" back.
            Node("evaluate", self._evaluate, deterministic=False, stage=None),
        ]

    # --------------------------------------------------------------- hydrate
    async def _hydrate(self, ctx: BlueprintContext) -> None:
        """Load the approved plan + steps, resolve writable scope from the mode row,
        stamp the workspace path on the run, and compute the branch name."""
        session = get_session()
        try:
            run = session.get(Run, ctx.run.id)
            repo_name = (run.repo if run else None) or ctx.artifacts.get("repo") or "ServerApp"
            repo = session.query(Repo).filter_by(name=repo_name).one_or_none()
            if repo is None:
                raise RuntimeError(f"repo '{repo_name}' not registered")
            plan = (
                session.query(Plan)
                .filter_by(run_id=run.id, status="approved")
                .order_by(Plan.created_at.desc(), Plan.id.desc())
                .first()
            )
            if plan is None:
                # Strict gate: development only ever runs on a plan the
                # human approved — never silently on a draft. The approval of
                # record is what makes the evidence trail auditable.
                raise RuntimeError("no approved plan to develop")
            mode = session.query(Mode).filter_by(name=run.mode).one_or_none()
            permissions = (mode.permissions if mode and mode.permissions else {}) or {}
            # Read the profile's test_cmds while the session is open (profile is a
            # lazy relationship; the stamp node runs after this session closes).
            test_cmds = list(repo.profile.test_cmds) if repo.profile and repo.profile.test_cmds else None
            branch = delivery.branch_name_for(run)
            workspace = str(_workspaces_root()) + "/" + run.id + "/" + (repo_name or "")
            run.session_volume_path = workspace
            run.last_active_at = datetime.now(timezone.utc)
            session.commit()
            ctx.artifacts["repo_row"] = repo
            ctx.artifacts["plan_row_id"] = plan.id
            ctx.artifacts["plan_steps"] = [s for s in plan.steps]
            ctx.artifacts["permissions"] = permissions
            ctx.artifacts["branch"] = branch
            ctx.artifacts["workspace"] = workspace
            ctx.artifacts["test_cmds"] = test_cmds
            ctx.run = run
        finally:
            session.close()

    # --------------------------------------------------------------- develop
    async def _develop(self, ctx: BlueprintContext) -> None:
        thread_manager = ctx.services["thread_manager"]
        repo: Repo = ctx.artifacts["repo_row"]
        permissions: dict = ctx.artifacts.get("permissions") or {}
        writable = self._writable_repo(repo, permissions)
        prompt = self._compose_developer_prompt(ctx)
        persona_prompt = self._persona(ctx, "developer",
                                        "You are the DEVELOPER. Implement the approved plan "
                                        "steps in the writable clone. Mark each step done only "
                                        "when its success_criterion is met.")
        thread = await thread_manager.spawn(
            ctx.run, persona="developer", prompt=prompt, persona_prompt=persona_prompt,
            writable_repo=writable, context_repos=[repo],
            resume_from_thread_id=ctx.artifacts.get("resume_from_thread_id"),
        )
        ctx.artifacts["develop_thread_id"] = thread.id
        await self._await_thread(thread.id)
        # Deterministic fallback: roll every pending/in_progress step to done once
        # the developer thread completes. (The thread's own step reports override this
        # when present; this guarantees forward progress for the evidence package.)
        self._mark_steps(ctx, target_status="done")

    # --------------------------------------------------------------- evaluate
    async def _evaluate(self, ctx: BlueprintContext) -> None:
        """Fresh-context evaluator: a brand-new thread/session that re-reads the
        workspace from scratch and verifies each step's success_criterion AGAINST
        the stored stamp evidence. It must NOT share the developer thread's session —
        fresh context is the whole point. Runs LAST so its verdict is final."""
        thread_manager = ctx.services["thread_manager"]
        repo: Repo = ctx.artifacts["repo_row"]
        prompt = self._compose_evaluator_prompt(ctx)
        persona_prompt = self._persona(ctx, "evaluator",
                                        "You are the EVALUATOR. You have FRESH context — you "
                                        "did NOT write this code. Re-read the workspace from "
                                        "scratch and verify each plan step's success_criterion "
                                        "against the control-plane test evidence below. "
                                        "Report a verdict (pass/fail) per step with evidence "
                                        "(file:line). Do not trust the developer's claims.")
        # writable_repo=None: the evaluator is read-only — it verifies, never edits.
        thread = await thread_manager.spawn(
            ctx.run, persona="evaluator", prompt=prompt, persona_prompt=persona_prompt,
            writable_repo=None, context_repos=[repo],
        )
        ctx.artifacts["evaluator_thread_id"] = thread.id
        await self._await_thread(thread.id)
        ctx.artifacts["evaluator_notes"] = self._last_message_text(thread.id) or ""
        # If the evaluator reported a failure, roll the flagged steps back to failed
        # so the evidence package records the gap (the human reviews before create_pr).
        self._apply_evaluator_verdict(ctx)
        self._persist_trajectory(ctx)

    # --------------------------------------------------------------- stamp
    async def _stamp(self, ctx: BlueprintContext) -> None:
        """Deterministic evidence: the control plane runs tests + Playwright
        screenshots and persists test_run/screenshot Event rows. Step statuses
        are deliberately NOT touched — the evaluator (next node) is the authority
        on step failure; rolling them here would erase its verdict."""
        workspace = ctx.artifacts.get("workspace") or ""
        repo: Repo = ctx.artifacts["repo_row"]
        develop_thread_id = ctx.artifacts.get("develop_thread_id") or ctx.run.id
        test_cmds = ctx.artifacts.get("test_cmds")
        test_signal = await evidence.run_test_commands(workspace, repo.name, test_cmds)
        screenshots = await evidence.stamp_screenshots(
            ctx.run.id, workspace, self._screenshot_routes(ctx))
        ctx.artifacts["test_signal"] = test_signal
        session = get_session()
        try:
            run = session.get(Run, ctx.run.id)
            thread = session.get(Thread, develop_thread_id)
            seq = (thread.next_seq if thread else 0)
            session.add(Event(
                run_id=run.id, thread_id=develop_thread_id, seq=seq,
                type="test_run", title=f"tests {'passed' if test_signal['passed'] else 'failed'}",
                payload={"passed": test_signal["passed"], "repo": repo.name,
                         "returncode": test_signal["returncode"],
                         "stdout": test_signal["stdout"], "stderr": test_signal["stderr"]},
            ))
            if screenshots:
                session.add(Event(
                    run_id=run.id, thread_id=develop_thread_id, seq=seq + 1,
                    type="screenshot", title="playwright screenshots",
                    payload={"routes": screenshots},
                ))
            if thread:
                thread.next_seq = seq + (2 if screenshots else 1)
            run.last_active_at = datetime.now(timezone.utc)
            session.commit()
            ctx.run = run
        finally:
            session.close()

    # --------------------------------------------------------------- helpers
    def _writable_repo(self, repo: Repo, permissions: dict) -> Repo | None:
        writable = bool(permissions.get("writable"))
        if not writable:
            return None
        allowed = permissions.get("repos") or []
        if allowed and repo.name not in allowed:
            return None
        return repo

    def _persona(self, ctx: BlueprintContext, role: str, fallback: str) -> str:
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

    def _compose_developer_prompt(self, ctx: BlueprintContext) -> str:
        steps = ctx.artifacts.get("plan_steps") or []
        lines = [f"Implement the {len(steps)} approved plan step(s) below.",
                 f"Workspace: {ctx.artifacts.get('workspace')}",
                 f"Branch: {ctx.artifacts.get('branch')}", ""]
        for s in steps:
            lines.append(f"- [{getattr(s, 'index', 0)}] {getattr(s, 'title', '')}: "
                         f"{getattr(s, 'success_criterion', '')}")
        return "\n".join(lines)

    def _compose_evaluator_prompt(self, ctx: BlueprintContext) -> str:
        steps = ctx.artifacts.get("plan_steps") or []
        lines = ["Verify the implemented plan steps against their success criteria.",
                 f"Workspace: {ctx.artifacts.get('workspace')}", ""]
        signal = ctx.artifacts.get("test_signal") or {}
        if signal:
            lines.append("--- Control-plane evidence (authoritative) ---")
            lines.append(f"Test run: passed={signal.get('passed')} "
                         f"(exit {signal.get('returncode')}).")
            tail = (signal.get("stdout") or "")[-500:]
            if tail:
                lines.append(f"Test output tail:\n{tail}")
            lines.append("")
        for s in steps:
            lines.append(f"- [{getattr(s, 'index', 0)}] {getattr(s, 'title', '')} -> "
                         f"success_criterion: {getattr(s, 'success_criterion', '')}")
        lines.append("")
        lines.append('Respond with a JSON verdict: {"verdict": "pass"|"fail", '
                     '"steps": [{"index": 0, "status": "pass"|"fail", "note": "..."}]}')
        return "\n".join(lines)

    def _screenshot_routes(self, ctx: BlueprintContext) -> list[str]:
        """Derive UI routes to screenshot from plan steps whose files look like
        ClientApp/UI paths. Empty list when no UI files -> screenshots skipped."""
        routes: list[str] = []
        for s in ctx.artifacts.get("plan_steps") or []:
            for f in (getattr(s, "files", None) or []):
                if "ClientApp" in f or f.endswith((".tsx", ".jsx", ".vue")):
                    if "/" not in routes:
                        routes.append("/")
                    break
        return routes

    def _mark_steps(self, ctx: BlueprintContext, target_status: str) -> None:
        plan_id = ctx.artifacts.get("plan_row_id")
        if plan_id is None:
            return
        session = get_session()
        try:
            for step in session.query(PlanStep).filter_by(plan_id=plan_id).all():
                if step.status in ("pending", "in_progress"):
                    step.status = target_status
            session.commit()
        finally:
            session.close()

    def _apply_evaluator_verdict(self, ctx: BlueprintContext) -> None:
        notes = ctx.artifacts.get("evaluator_notes") or ""
        if not notes:
            return
        parsed = self._parse_json(notes)
        if not parsed or parsed.get("verdict") == "pass":
            return
        plan_id = ctx.artifacts.get("plan_row_id")
        if plan_id is None:
            return
        failed_indices: set[int] = set()
        for entry in parsed.get("steps", []):
            if isinstance(entry, dict) and entry.get("status") == "fail":
                failed_indices.add(entry.get("index"))
        if not failed_indices:
            return
        session = get_session()
        try:
            for step in session.query(PlanStep).filter_by(plan_id=plan_id).all():
                if step.index in failed_indices:
                    step.status = "failed"
            session.commit()
        finally:
            session.close()

    def _persist_trajectory(self, ctx: BlueprintContext) -> None:
        """The run's trajectory summary rides the evaluator's verdict (it runs
        last, so the summary reflects the final state of the evidence)."""
        repo: Repo = ctx.artifacts["repo_row"]
        develop_thread_id = ctx.artifacts.get("develop_thread_id") or ctx.run.id
        session = get_session()
        try:
            run = session.get(Run, ctx.run.id)
            session.add(TrajectorySummary(
                run_id=run.id, thread_id=develop_thread_id, user_id=run.created_by,
                summary=(ctx.artifacts.get("evaluator_notes") or "")[:500]
                        or f"Development run on {repo.name}: {run.title[:200]}",
            ))
            session.commit()
        finally:
            session.close()

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
            if status in ("idle", "completed", "failed", "stopped",
                          "interrupted", "replaced"):  # H-38
                return
            await asyncio.sleep(poll_seconds)


def _workspaces_root():
    from app.core.config import get_settings
    return get_settings().workspaces_dir
