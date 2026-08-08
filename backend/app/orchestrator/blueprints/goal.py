"""Goal mode blueprint: PRD -> PR, zero interruption.

hydrate  (deterministic): resolve repo + the mode's writable scope, compute
           branch/workspace, compute blast_radius from the fleet graph.
explore  (agentic, read-only): context gathering — ONE researcher thread by
           default, or a capped fan-out of explorers when the run carries a
           fanout param (the user picks width; both shapes are read-only).
plan     (agentic planner thread): draft contracts.Plan JSON from the PRD +
           the explore summary.
refine   (agentic, EXACTLY 3 rounds): critic (FRESH read-only thread — it
           cannot inherit the planner's rationalizations) -> reviser (fresh
           thread, applies blocking findings). The plan hardens back and
           forth 3 times before it can ship.
present  (deterministic): validate contracts.Plan, lint citations against the
           golden repo, persist Plan (status "approved" — in goal mode the
           pipeline IS the approval: the run was created with autonomous
           autonomy, and the audit trail records auto_approved + the critique
           history) + PlanStep rows.
develop  (agentic, writable): one developer thread implements every step and
           commits on the run branch; the control plane never trusts its
           self-report.
verify   (deterministic green gate): evidence.verify_suite — tests + ruff +
           npm lint/build + a bounded dev-server boot smoke, all control-plane.
           Red -> bounded fix loop (2 rounds: fixer thread with the failure
           tails + re-gate; the fixer's workspace is PRESERVED, never
           re-stamped). Still red -> the run FAILS. Zero interruption never
           means shipping red.
ship     (deterministic): commit_pending safety net -> evidence-gated
           open_pr -> COMPLETED. The human merges in ADO; the pipeline never
           merges its own work.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

import sqlalchemy as sa
from collegium_contracts import RunStage

from app.core.config import get_settings
from app.core.logging import get_logger
from app.db.base import get_session
from app.db.models.event import Event
from app.db.models.mode import Mode
from app.db.models.repo import Repo, RepoStatus
from app.db.models.run import Plan, PlanStep, Run
from app.db.models.thread import Thread
from app.db.models.trajectory import TrajectorySummary
from app.orchestrator.blueprints.base import Blueprint, BlueprintContext, Node, lane_override
from app.orchestrator.blueprints.plan import PLAN_SCHEMA_HINT, PlanBlueprint
from app.services import delivery, evidence
from app.services.runs import transition

log = get_logger(service="blueprint_goal")

CRITIQUE_ROUNDS = 3
MAX_FIX_ROUNDS = 2

# Swarm-explore angles: distinct, non-overlapping slices (never arithmetic
# clones). The first N are used when the run requests a fan-out of N.
EXPLORE_ANGLES = [
    "change surface: which files/modules this story touches and why",
    "verification: existing tests, lint/CI setup, and how to prove the change",
    "dependencies & integrations: upstream/downstream code constraining the story",
    "conventions: similar existing implementations the plan should mirror",
]

EXPLORER_HINT = (
    "\n\nYou are the CONTEXT EXPLORER in a zero-interruption goal pipeline. "
    "Read-only investigation ONLY — no writes. Map the change surface for the "
    "feature story below and report: relevant files (path:line), existing "
    "patterns to reuse, test/verification points, and risks. Your summary feeds "
    "the planner directly; precise beats exhaustive."
)

PLANNER_HINT = (
    "\n\nYou are the PLANNER in a zero-interruption goal pipeline. Turn the "
    "feature story + the explorers' context into a structured Plan (the JSON "
    "schema below). Steps must be ordered, complete for the story, and each "
    "success_criterion must be verifiable by the deterministic gate (tests, "
    "ruff/lint, build). You are read-only: you never modify files."
)

CRITIC_HINT = (
    "\n\nYou are the PLAN CRITIC (round {round_no} of {rounds}). Review the "
    "draft Plan JSON below with FRESH eyes — you did not write it. Verify "
    "every file/symbol claim by read-only grep on the mounted repo. Check: "
    "steps are complete for the story and correctly ordered; every "
    "success_criterion is verifiable by the deterministic gate; scope stays "
    "inside the story; risks are honest. Report BLOCKING findings precisely "
    "(step index + finding + evidence). If the plan is solid, say exactly: "
    "'no blocking findings'."
)

REVISER_HINT = (
    "\n\nYou are the PLAN REVISER. The critic's blocking findings below are "
    "authoritative review comments: resolve each one precisely — never argue, "
    "never route around. Apply them to the draft and return the FULL revised "
    "Plan JSON (same schema, nothing outside the JSON). You are read-only."
)

DEVELOPER_HINT = (
    "\n\nYou are the DEVELOPER in a zero-interruption goal pipeline. Implement "
    "every plan step in the writable clone, following the repo's AGENTS.md and "
    "existing conventions. First `git checkout -B {branch}` inside the clone, "
    "then COMMIT each completed step on that branch — the control plane pushes "
    "and opens the PR; you never push. A step is done only when its "
    "success_criterion is met. Run the repo's tests as you go: after you, the "
    "control plane re-runs the full verification gate (tests, ruff/lint, "
    "build, dev boot) — leave the tree green."
)

FIXER_HINT = (
    "\n\nYou are the FIXER in a zero-interruption goal pipeline. The "
    "deterministic verification gate is RED — the failing checks' real output "
    "is below. Fix ONLY what the failures report: no scope creep, no "
    "refactors. The previous thread's implementation is already in this "
    "workspace; keep committing on branch {branch}. Make the gate green."
)


class GoalBlueprint(Blueprint):
    name = "goal"

    def nodes(self) -> list[Node]:
        return [
            Node("hydrate", self._hydrate, deterministic=True, stage=RunStage.PROVISIONING),
            Node("explore", self._explore, deterministic=False, stage=RunStage.INVESTIGATING),
            Node("plan", self._plan, deterministic=False, stage=RunStage.PLANNING),
            # refine/present stay at PLANNING: the plan isn't real until it's
            # persisted, and the rail shouldn't flash develop before then.
            Node("refine", self._refine, deterministic=False, stage=None),
            Node("present", self._present, deterministic=True, stage=None),
            Node("develop", self._develop, deterministic=False, stage=RunStage.DEVELOPING),
            Node("verify", self._verify, deterministic=True, stage=RunStage.VERIFYING),
            Node("ship", self._ship, deterministic=True, stage=RunStage.PR_READY),
        ]

    # --------------------------------------------------------------- hydrate
    async def _hydrate(self, ctx: BlueprintContext) -> None:
        from app.services.mentions import resolve_run_repos
        session = get_session()
        try:
            run = session.get(Run, ctx.run.id)
            # Goal is a fleet mode: ALL usable repos are mounted read-only as
            # workspace context, no @mention required. The explicit repo (or a
            # user @mention) still pins the primary writable target — kept on
            # run.repo for backward-compat (evidence, dashboard grouping) and
            # used to stamp the workspace path.
            target, mentioned, unknown = resolve_run_repos(
                session, ctx.artifacts.get("repo") or (run.repo if run else None),
                ctx.artifacts.get("task") or ctx.run.title)
            if unknown:
                raise RuntimeError(
                    f"repo '{unknown[0]}' not registered — mention a registered repo with `@Name`")
            fleet = session.query(Repo).filter(Repo.status.in_(RepoStatus.USABLE)).all()
            # An explicit target is honored regardless of fleet status (the
            # user asked for it); it heads the context and is the writable
            # primary. With no target, the usable fleet IS the context — and
            # an empty fleet means there's nothing to run goal mode against.
            # (no default repo anywhere in the system).
            if target is None:
                if not fleet:
                    raise RuntimeError(
                        "no usable repos in the fleet — register a repo to run goal mode")
                target = fleet[0]
                context = list(fleet)
            else:
                context = [target] + [r for r in fleet if r.id != target.id]
            repo = target
            mode = session.query(Mode).filter_by(name=run.mode).one_or_none()
            permissions = (mode.permissions if mode and mode.permissions else {}) or {}
            test_cmds = list(repo.profile.test_cmds) if repo.profile and repo.profile.test_cmds else None
            branch = delivery.branch_name_for(run)
            workspace = str(_workspaces_root()) + "/" + run.id + "/" + (repo.name or "")
            run.session_volume_path = workspace
            if not run.repo:
                run.repo = repo.name
            run.last_active_at = datetime.now(UTC)
            session.commit()
            ctx.artifacts["repo_row"] = repo
            ctx.artifacts["context_repos"] = context
            ctx.artifacts["mentioned_repos"] = mentioned  # hints for the planner
            ctx.artifacts["permissions"] = permissions
            ctx.artifacts["branch"] = branch
            ctx.artifacts["workspace"] = workspace
            ctx.artifacts["test_cmds"] = test_cmds
            ctx.artifacts["thread_ids"] = []
            ctx.run = run
        finally:
            session.close()

        blast_radius: list[str] = []
        try:
            from app.core.fleet import get_fleet_config
            _repos, graph = get_fleet_config()
            if graph is not None:
                blast_radius = graph.blast_radius_for(repo.name)
        except Exception:
            blast_radius = []
        ctx.artifacts["blast_radius"] = blast_radius

    # --------------------------------------------------------------- explore
    async def _explore(self, ctx: BlueprintContext) -> None:
        """Context gathering. fanout>=2 -> a capped swarm of explorers with
        distinct angles; otherwise a single researcher. Both read-only."""
        thread_manager = ctx.services["thread_manager"]
        repo: Repo = ctx.artifacts["repo_row"]
        context: list[Repo] = ctx.artifacts.get("context_repos") or [repo]
        task = ctx.artifacts.get("task") or ctx.run.title
        cap = get_settings().global_thread_cap
        requested = max(1, min(int(ctx.artifacts.get("fanout") or 1), cap))
        persona_prompt = self._persona(ctx, EXPLORER_HINT)

        model, reasoning = lane_override(ctx)
        if requested == 1:
            prompt = (f"Feature story:\n{task}\n\nTarget repo: {repo.name}\n"
                      f"Angle: {EXPLORE_ANGLES[0]} (but follow the evidence wherever it leads)")
            thread = await thread_manager.spawn(
                ctx.run, persona="researcher", prompt=prompt, persona_prompt=persona_prompt,
                writable_repo=None, context_repos=context,
                resume_from_thread_id=ctx.artifacts.get("resume_from_thread_id"),
                model=model, reasoning=reasoning,
            )
            ctx.artifacts["thread_ids"].append(thread.id)
            await _await_thread(thread.id)
            # Node-end: kill the lingering idle container NOW — it would hold
            # its capacity slot for the full idle TTL otherwise (review C4).
            await thread_manager.finish_thread(thread.id)
            summaries = [(f"--- explorer ({EXPLORE_ANGLES[0]}) ---\n"
                          f"{_last_message_text(thread.id) or '(no summary)'}")]
        else:
            specs = [
                {"persona": "explorer",
                 "prompt": (f"Feature story:\n{task}\n\nTarget repo: {repo.name}\n"
                            f"Your slice: {EXPLORE_ANGLES[i % len(EXPLORE_ANGLES)]}"),
                 "persona_prompt": persona_prompt,
                 "thread_hint": f"explorer-{i}",
                 **({"model": model, "reasoning": reasoning} if model else {})}
                for i in range(requested)
            ]
            threads = await thread_manager.spawn_many(ctx.run, specs, context)
            ctx.artifacts["thread_ids"].extend(l.id for l in threads)
            await asyncio.gather(*(_await_thread(l.id) for l in threads))
            await asyncio.gather(*(thread_manager.finish_thread(l.id) for l in threads))
            summaries = [
                f"--- explorer {i} ({_slice_label(l)}) ---\n"
                f"{_last_message_text(l.id) or '(no summary)'}"
                for i, l in enumerate(threads)
            ]
        ctx.artifacts["explore_summary"] = "\n\n".join(summaries)

    # --------------------------------------------------------------- plan
    async def _plan(self, ctx: BlueprintContext) -> None:
        thread_manager = ctx.services["thread_manager"]
        repo: Repo = ctx.artifacts["repo_row"]
        context: list[Repo] = ctx.artifacts.get("context_repos") or [repo]
        prompt = self._compose_planner_prompt(ctx, repo)
        persona_prompt = self._persona(ctx, PLANNER_HINT + PLAN_SCHEMA_HINT)
        model, reasoning = lane_override(ctx)
        thread = await thread_manager.spawn(
            ctx.run, persona="planner", prompt=prompt, persona_prompt=persona_prompt,
            writable_repo=None, context_repos=context,
            model=model, reasoning=reasoning,
        )
        ctx.artifacts["thread_ids"].append(thread.id)
        await _await_thread(thread.id)
        await thread_manager.finish_thread(thread.id)
        draft_text = _last_message_text(thread.id)
        draft = PlanBlueprint._parse_json(draft_text or "")
        if draft is None:
            # Without a parseable plan there is nothing to critique or ship —
            # fail the run honestly here rather than three rounds later.
            raise RuntimeError("goal planner produced no parseable Plan JSON")
        ctx.artifacts["draft_plan"] = draft

    # --------------------------------------------------------------- refine
    async def _refine(self, ctx: BlueprintContext) -> None:
        """Exactly CRITIQUE_ROUNDS rounds of critic -> revise. Each round's
        threads are FRESH: the critic can't inherit the planner's framing, the
        reviser can't argue with a stale conversation — it gets the findings."""
        thread_manager = ctx.services["thread_manager"]
        repo: Repo = ctx.artifacts["repo_row"]
        context: list[Repo] = ctx.artifacts.get("context_repos") or [repo]
        draft: dict = ctx.artifacts["draft_plan"]
        notes: list[str] = []
        for round_no in range(1, CRITIQUE_ROUNDS + 1):
            critic_prompt = ("Critique the draft plan (round "
                             f"{round_no}/{CRITIQUE_ROUNDS}).\n\n--- Draft Plan ---\n"
                             + json.dumps(draft, indent=2))
            model, reasoning = lane_override(ctx)
            critic = await thread_manager.spawn(
                ctx.run, persona="critic", prompt=critic_prompt,
                persona_prompt=self._persona(
                    ctx, CRITIC_HINT.format(round_no=round_no, rounds=CRITIQUE_ROUNDS)),
                writable_repo=None, context_repos=context,
                model=model, reasoning=reasoning,
            )
            ctx.artifacts["thread_ids"].append(critic.id)
            await _await_thread(critic.id)
            await thread_manager.finish_thread(critic.id)
            critique = _last_message_text(critic.id) or ""
            notes.append(f"round {round_no} critic: {critique[:1500]}")

            reviser_prompt = (
                "Apply every blocking finding and return the full revised Plan JSON.\n\n"
                "--- Draft Plan ---\n" + json.dumps(draft, indent=2)
                + "\n\n--- Critique (round " + str(round_no) + ") ---\n" + critique)
            reviser = await thread_manager.spawn(
                ctx.run, persona="reviser", prompt=reviser_prompt,
                persona_prompt=self._persona(ctx, REVISER_HINT + PLAN_SCHEMA_HINT),
                writable_repo=None, context_repos=context,
                model=model, reasoning=reasoning,
            )
            ctx.artifacts["thread_ids"].append(reviser.id)
            await _await_thread(reviser.id)
            await thread_manager.finish_thread(reviser.id)
            revised = PlanBlueprint._parse_json(_last_message_text(reviser.id) or "")
            if revised is not None:
                draft = revised
            else:
                log.warning("reviser output unparseable — keeping prior draft",
                            run_id=ctx.run.id, round=round_no)
                notes.append(f"round {round_no} reviser: unparseable output, prior draft kept")
        ctx.artifacts["draft_plan"] = draft
        ctx.artifacts["critique_notes"] = notes

    # --------------------------------------------------------------- present
    async def _present(self, ctx: BlueprintContext) -> None:
        """Persist the hardened plan as APPROVED — in goal mode the pipeline is
        the approval (the run opted into autonomous execution at creation), and
        the structured payload records the critique history for audit."""
        draft_json = ctx.artifacts.get("draft_plan")
        if draft_json is None:
            raise RuntimeError("goal refine produced no Plan JSON")
        repo: Repo = ctx.artifacts["repo_row"]
        citations = PlanBlueprint._collect_citations(draft_json)
        lint_report = PlanBlueprint._lint_citations(repo.name, citations)
        from collegium_contracts import Plan as PlanContract
        plan_contract = PlanContract.model_validate(draft_json)
        structured = dict(draft_json)
        structured["blast_radius"] = ctx.artifacts.get("blast_radius", structured.get("blast_radius", []))
        structured["auto_approved"] = True  # zero-interruption: no human gate
        if lint_report:
            structured["citation_lint"] = lint_report
        notes = ctx.artifacts.get("critique_notes") or []
        if notes:
            structured["critic_notes"] = notes  # always a list (C1 shape)

        session = get_session()
        try:
            run = session.get(Run, ctx.run.id)
            plan = Plan(run_id=run.id, structured=structured, status="approved")
            session.add(plan)
            session.flush()
            for step in plan_contract.steps:
                session.add(PlanStep(
                    plan_id=plan.id, index=step.index, title=step.title,
                    description=step.description, repo=step.repo, files=step.files,
                    success_criterion=step.success_criterion, status="pending",
                ))
            run.last_active_at = datetime.now(UTC)
            session.commit()
            ctx.artifacts["plan_row_id"] = plan.id
            ctx.artifacts["plan_steps"] = plan_contract.steps
            ctx.run = run
        finally:
            session.close()

    # --------------------------------------------------------------- develop
    async def _develop(self, ctx: BlueprintContext) -> None:
        thread_manager = ctx.services["thread_manager"]
        repo: Repo = ctx.artifacts["repo_row"]
        context: list[Repo] = ctx.artifacts.get("context_repos") or [repo]
        writable = self._writable_repo(repo, ctx.artifacts.get("permissions") or {})
        branch = ctx.artifacts["branch"]
        prompt = self._compose_developer_prompt(ctx)
        persona_prompt = self._persona(ctx, DEVELOPER_HINT.format(branch=branch))
        model, reasoning = lane_override(ctx)
        thread = await thread_manager.spawn(
            ctx.run, persona="developer", prompt=prompt, persona_prompt=persona_prompt,
            writable_repo=writable, context_repos=context,
            model=model, reasoning=reasoning,
        )
        ctx.artifacts["thread_ids"].append(thread.id)
        ctx.artifacts["develop_thread_id"] = thread.id
        await _await_thread(thread.id)
        # Node-end BEFORE verify: the developer's idle container would hold the
        # per-repo write lock for the idle TTL and the fix loop's spawns would
        # be rejected (review C2). Finish also stamps "completed", which the PR
        # evidence gate requires (review C3).
        await thread_manager.finish_thread(thread.id)
        # Deterministic fallback (same contract as development mode): thread
        # step reports win when present; otherwise completion implies done.
        self._mark_steps(ctx, target_status="done")

    # --------------------------------------------------------------- verify
    async def _verify(self, ctx: BlueprintContext) -> None:
        """The deterministic green gate + bounded fix loop. A red gate spawns
        a fixer thread with the real failure tails (workspace PRESERVED — a
        re-stamp would rmtree the implementation), then re-runs the suite.
        Still red after MAX_FIX_ROUNDS -> raise: the run fails, no PR ships."""
        repo: Repo = ctx.artifacts["repo_row"]
        workspace = ctx.artifacts["workspace"]
        test_cmds = ctx.artifacts.get("test_cmds")
        suite = await evidence.verify_suite(workspace, repo.name, test_cmds)
        self._persist_verify_event(ctx, suite)
        rounds = 0
        while not suite["passed"] and rounds < MAX_FIX_ROUNDS:
            rounds += 1
            await self._fix_round(ctx, suite, rounds)
            suite = await evidence.verify_suite(workspace, repo.name, test_cmds)
            self._persist_verify_event(ctx, suite, fix_round=rounds)
        ctx.artifacts["verify_suite"] = suite
        ctx.artifacts["fix_rounds"] = rounds
        if not suite["passed"]:
            failed = [c["name"] for c in suite["checks"]
                      if not c.get("skipped") and not c["passed"]]
            raise RuntimeError(
                f"verification gate still red after {rounds} fix round(s): "
                + ", ".join(failed))

    # --------------------------------------------------------------- ship
    async def _ship(self, ctx: BlueprintContext) -> None:
        """commit safety net -> evidence-gated PR -> COMPLETED. open_pr's
        evidence gate (plan steps done + a completed thread) is the final
        backstop; the human merges in ADO — the pipeline never merges itself."""
        repo: Repo = ctx.artifacts["repo_row"]
        workspace = ctx.artifacts["workspace"]
        branch = ctx.artifacts["branch"]
        committed = await delivery.commit_pending(ctx.run.id, workspace, branch)
        if committed:
            log.info("ship: safety-net commit created", run_id=ctx.run.id)
        link = await delivery.open_pr(ctx.run.id, repo.name, workspace)

        suite = ctx.artifacts.get("verify_suite") or {}
        session = get_session()
        try:
            run = session.get(Run, ctx.run.id)
            transition(run, RunStage.COMPLETED)
            run.finished_at = datetime.now(UTC)
            run.auto_summary = (
                f"Goal shipped: PR #{link.ado_pr_id} opened on {repo.name} "
                f"({branch}). Verification gate: "
                f"{'green' if suite.get('passed') else 'red'}; "
                f"fix rounds: {ctx.artifacts.get('fix_rounds', 0)}."
            )[:2000]
            session.add(TrajectorySummary(
                run_id=run.id, thread_id=ctx.artifacts.get("develop_thread_id")
                or "control-plane", user_id=run.created_by,
                summary=run.auto_summary[:500],
            ))
            session.commit()
            available = run.available_actions
        finally:
            session.close()
        relay = ctx.services.get("relay")
        if relay:
            await relay.publish_run_stage(ctx.run.id, RunStage.COMPLETED.value, available)
        for thread_id in ctx.artifacts.get("thread_ids", []):
            await ctx.services["thread_manager"].settle_cost(thread_id)

    # --------------------------------------------------------------- helpers
    async def _fix_round(self, ctx: BlueprintContext, suite: dict, round_no: int) -> None:
        thread_manager = ctx.services["thread_manager"]
        repo: Repo = ctx.artifacts["repo_row"]
        context: list[Repo] = ctx.artifacts.get("context_repos") or [repo]
        writable = self._writable_repo(repo, ctx.artifacts.get("permissions") or {})
        branch = ctx.artifacts["branch"]
        failures: list[str] = []
        for c in suite["checks"]:
            if c.get("skipped") or c["passed"]:
                continue
            failures.append(
                f"### {c['name']} (exit {c['returncode']})\n"
                f"stdout tail:\n{c.get('stdout', '')[-800:]}\n"
                f"stderr tail:\n{c.get('stderr', '')[-800:]}")
        prompt = (f"Verification gate round {round_no}/{MAX_FIX_ROUNDS} is RED.\n"
                  f"Workspace: {ctx.artifacts['workspace']}\nBranch: {branch}\n\n"
                  + "\n\n".join(failures))
        model, reasoning = lane_override(ctx)
        thread = await thread_manager.spawn(
            ctx.run, persona="fixer", prompt=prompt,
            persona_prompt=self._persona(ctx, FIXER_HINT.format(branch=branch)),
            writable_repo=writable, context_repos=context,
            preserve_workspace=True,  # re-stamping would wipe the implementation
            model=model, reasoning=reasoning,
        )
        ctx.artifacts["thread_ids"].append(thread.id)
        await _await_thread(thread.id)
        # Free the write lock for the next fix round (and for ship's push).
        await thread_manager.finish_thread(thread.id)

    def _writable_repo(self, repo: Repo, permissions: dict) -> Repo | None:
        writable = bool(permissions.get("writable"))
        if not writable:
            return None
        allowed = permissions.get("repos") or []
        if allowed and repo.name not in allowed:
            return None
        return repo

    def _persona(self, ctx: BlueprintContext, role_hint: str) -> str:
        """Mode persona + playbooks + this thread's role (same composition as
        the development blueprint, with goal's role-specific hint)."""
        session = get_session()
        try:
            mode = session.query(Mode).filter_by(name=ctx.run.mode).one_or_none()
            base = (mode.persona_prompt if mode else "") or ""
        finally:
            session.close()
        from app.services.playbooks import playbooks_prompt_for_mode
        playbooks = playbooks_prompt_for_mode(ctx.run.mode)
        parts = [p for p in (base, playbooks, role_hint) if p]
        return "\n\n".join(parts).strip()

    def _compose_planner_prompt(self, ctx: BlueprintContext, repo: Repo) -> str:
        parts = ["--- Feature story (PRD) ---", ctx.artifacts.get("task") or ctx.run.title]
        explore = ctx.artifacts.get("explore_summary")
        if explore:
            parts.append("\n--- Exploration context ---\n" + explore[:12000])
        br = ctx.artifacts.get("blast_radius") or []
        if br:
            parts.append("\n--- Fleet blast radius ---\n" + ", ".join(br))
        parts.append(f"\nTarget repo: {repo.name}")
        return "\n".join(parts)

    def _compose_developer_prompt(self, ctx: BlueprintContext) -> str:
        steps = ctx.artifacts.get("plan_steps") or []
        lines = [(f"Implement the {len(steps)} plan step(s) below — the plan survived "
                  f"{CRITIQUE_ROUNDS} critique rounds; stay inside its scope."),
                 f"Goal: {ctx.artifacts.get('task') or ctx.run.title}",
                 f"Workspace: {ctx.artifacts.get('workspace')}",
                 f"Branch: {ctx.artifacts.get('branch')}", ""]
        for s in steps:
            lines.append(f"- [{getattr(s, 'index', 0)}] {getattr(s, 'title', '')}: "
                         f"{getattr(s, 'success_criterion', '')}")
        return "\n".join(lines)

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

    def _persist_verify_event(self, ctx: BlueprintContext, suite: dict,
                              fix_round: int = 0) -> None:
        """test_run Event rows are what the evidence package reads at PR time —
        the gate's verdict must be in the DB, never only in artifacts."""
        develop_thread_id = ctx.artifacts.get("develop_thread_id") or "control-plane"
        suffix = f" (fix round {fix_round})" if fix_round else ""
        session = get_session()
        try:
            thread = session.get(Thread, develop_thread_id)
            if thread is not None:
                seq = thread.next_seq
            else:
                # Control-plane fallback thread may not exist as a row — with
                # the D1 unique (run_id, thread_id, seq) constraint a fixed
                # seq=0 collides on the second fix round. Allocate above the
                # current max instead.
                max_seq = (session.query(sa.func.max(Event.seq))
                           .filter_by(run_id=ctx.run.id,
                                      thread_id=develop_thread_id)
                           .scalar())
                seq = (max_seq if max_seq is not None else -1) + 1
            session.add(Event(
                run_id=ctx.run.id, thread_id=develop_thread_id, seq=seq,
                type="test_run",
                title=f"verify {'passed' if suite['passed'] else 'failed'}{suffix}",
                payload={"passed": suite["passed"],
                         "checks": [{k: c.get(k) for k in
                                     ("name", "skipped", "passed", "returncode",
                                      "reason", "stdout", "stderr")}
                                    for c in suite["checks"]]},
            ))
            if thread:
                thread.next_seq = seq + 1
            session.commit()
        finally:
            session.close()


def _workspaces_root():
    return get_settings().workspaces_dir


def _slice_label(thread: Thread) -> str:
    """The explorer's angle, read back from the thread's OWN spawn context —
    never from the spec index (spawn_many drops failed spawns, so spec[i] and
    thread[i] can misalign)."""
    prompt = (thread.spawn_context or {}).get("prompt", "")
    return prompt.rsplit("Your slice: ", 1)[-1] if "Your slice: " in prompt else "slice"


# A wedged thread must not hang an unattended pipeline forever — fail the run
# with a clear reason instead (review C6). Generous bound: goal threads do
# long turns, but never 45-minute ones.
THREAD_MAX_WAIT_S = 2700.0


async def _await_thread(thread_id: str, poll_seconds: float = 2.0,
                        max_wait_s: float = THREAD_MAX_WAIT_S) -> None:
    waited = 0.0
    while True:
        session = get_session()
        try:
            thread = session.get(Thread, thread_id)
            status = thread.status if thread else "failed"
        finally:
            session.close()
        if status in ("idle", "completed", "failed", "stopped",
                      "interrupted", "replaced",
                      "input_required"):  # H-38 + A4: approval-parked threads
                      # end the blueprint await — the run stage tracks the
                      # human wait; polling forever wedged the run.
            return
        if waited >= max_wait_s:
            raise RuntimeError(
                f"thread {thread_id} wedged in '{status}' for {max_wait_s:.0f}s")
        await asyncio.sleep(poll_seconds)
        waited += poll_seconds


def _last_message_text(thread_id: str) -> str | None:
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
