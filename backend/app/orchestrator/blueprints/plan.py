"""Plan mode blueprint: hydrate -> draft -> critique -> present.

hydrate  (deterministic): resolve repo scope; if run.work_item_id fetch the ADO
           work item into artifacts; compute blast_radius from the fleet graph.
draft    (agentic planner thread): structured output target = contracts.Plan JSON
           schema in the persona prompt.
critique (agentic critic thread, FRESH thread/session): gets the draft Plan JSON +
           instructions to verify file/symbol claims via read-only grep on the
           mounted golden repos.
present  (deterministic): parse+validate the Plan JSON with contracts.Plan, lint
           every file/symbol citation through zagent_maps lint against the golden
           repo dir (drifted citations flagged in the Plan row's structured
           payload, never crash on lint failure), persist Plan + PlanStep rows,
           transition the run to awaiting_approval.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone

from zagent_contracts import RunStage

from app.ado.client import AdoClient
from app.core.fleet import get_fleet_config
from app.db.base import get_session
from app.db.models.event import Event
from app.db.models.mode import Mode
from app.db.models.repo import Repo
from app.db.models.run import Plan, PlanStep, Run
from app.orchestrator.blueprints.base import Blueprint, BlueprintContext, Node
from app.services.runs import transition

PLAN_SCHEMA_HINT = (
    "\n\n--- Structured output contract ---\n"
    "Return ONLY a JSON object matching this schema (no prose outside the JSON):\n"
    "{\n"
    '  "schema_version": 1,\n'
    '  "title": "<short title>",\n'
    '  "summary": "<2-3 sentence plan summary>",\n'
    '  "steps": [\n'
    '    {"index": 0, "title": "<step title>", "description": "<what to do>", '
    '"repo": "<repo name>", "files": ["path:to:touch"], "success_criterion": "<verifiable>", '
    '"status": "pending"}\n'
    '  ],\n'
    '  "blast_radius": ["<downstream service>", ...],\n'
    '  "risks": ["<risk>", ...],\n'
    '  "evidence_contract": ["tests_pass", "diff_summary"]\n'
    "}\n"
    "Cite every file/symbol claim as path:line, verified by read-only grep on the "
    "mounted golden repo. Flag anything you could NOT verify in risks."
)

CITATION_RE = re.compile(r"(\S+\.\w+:\d+(?:-\d+)?)")


def _playbook_block(mode_name: str) -> str:
    """WU6: the mode's playbooks ride the persona prompt into every thread."""
    from app.services.playbooks import playbooks_prompt_for_mode
    block = playbooks_prompt_for_mode(mode_name)
    return ("\n\n" + block) if block else ""


class PlanBlueprint(Blueprint):
    name = "plan"

    def nodes(self) -> list[Node]:
        return [
            Node("hydrate", self._hydrate, deterministic=True, stage=RunStage.PROVISIONING),
            Node("draft", self._draft, deterministic=False, stage=RunStage.PLANNING),
            Node("critique", self._critique, deterministic=False, stage=RunStage.PLANNING),
            Node("present", self._present, deterministic=True, stage=RunStage.AWAITING_USER),
        ]

    # --------------------------------------------------------------- hydrate
    async def _hydrate(self, ctx: BlueprintContext) -> None:
        session = get_session()
        try:
            target = ctx.artifacts.get("repo") or ctx.run.repo or "ServerApp"
            repo = session.query(Repo).filter_by(name=target).one_or_none()
            if repo is None:
                raise RuntimeError(f"repo '{target}' not registered")
        finally:
            session.close()

        work_item = None
        if ctx.run.work_item_id:
            try:
                client = ctx.services.get("ado_client") or AdoClient()
                work_item = await client.get_work_item(ctx.run.work_item_id)
            except Exception:
                work_item = None

        blast_radius: list[str] = []
        try:
            _repos, graph = get_fleet_config()
            if graph is not None:
                blast_radius = graph.blast_radius_for(repo.name)
        except Exception:
            blast_radius = []

        ctx.artifacts["repo_row"] = repo
        ctx.artifacts["work_item"] = work_item
        ctx.artifacts["blast_radius"] = blast_radius

    # --------------------------------------------------------------- draft
    async def _draft(self, ctx: BlueprintContext) -> None:
        # start_plan promotion (WU4): when a seed_plan is present (promoted from a
        # debug run), skip the planner thread — the debug proposal IS the draft — and
        # let the critic verify it fresh. The seed must still parse+validate as a
        # contracts.Plan, which _present enforces.
        seed = ctx.artifacts.get("seed_plan")
        if isinstance(seed, dict):
            ctx.artifacts["draft_plan"] = seed
            ctx.artifacts["draft_text"] = json.dumps(seed)
            ctx.artifacts["draft_thread_id"] = None
            return
        thread_manager = ctx.services["thread_manager"]
        session = get_session()
        try:
            mode = session.query(Mode).filter_by(name="plan").one_or_none()
            persona_prompt = (mode.persona_prompt if mode else "") + PLAN_SCHEMA_HINT
        finally:
            session.close()
        persona_prompt += _playbook_block("plan")
        repo: Repo = ctx.artifacts["repo_row"]
        prompt = self._compose_planner_prompt(ctx, repo)
        thread = await thread_manager.spawn(
            ctx.run, persona="planner", prompt=prompt, persona_prompt=persona_prompt,
            writable_repo=None, context_repos=[repo],
            resume_from_thread_id=ctx.artifacts.get("resume_from_thread_id"),
        )
        ctx.artifacts["draft_thread_id"] = thread.id
        await self._await_thread(thread.id)
        ctx.artifacts["draft_text"] = self._last_message_text(thread.id)

    # --------------------------------------------------------------- critique
    async def _critique(self, ctx: BlueprintContext) -> None:
        thread_manager = ctx.services["thread_manager"]
        draft_json = self._parse_json(ctx.artifacts.get("draft_text") or "")
        if draft_json is None:
            ctx.artifacts["critique_notes"] = "planner produced no parseable Plan JSON"
            return
        ctx.artifacts["draft_plan"] = draft_json
        session = get_session()
        try:
            mode = session.query(Mode).filter_by(name="plan").one_or_none()
            persona_prompt = (mode.persona_prompt if mode else "") + (
                "\n\nYou are the CRITIC. A planner produced the Plan JSON below. "
                "Verify every file/symbol claim by read-only grep on the mounted "
                "golden repo. Report drift as a Notebook: findings, evidence "
                "(file:line), confidence, open_questions. Be specific about which "
                "citations are stale.\n\n--- Draft Plan ---\n"
                + json.dumps(draft_json, indent=2)
            )
        finally:
            session.close()
        persona_prompt += _playbook_block("plan")
        repo: Repo = ctx.artifacts["repo_row"]
        thread = await thread_manager.spawn(
            ctx.run, persona="critic", prompt="Critique the plan above.",
            persona_prompt=persona_prompt, writable_repo=None, context_repos=[repo],
        )
        ctx.artifacts["critique_thread_id"] = thread.id
        await self._await_thread(thread.id)
        ctx.artifacts["critique_notes"] = self._last_message_text(thread.id) or ""

    # --------------------------------------------------------------- present
    async def _present(self, ctx: BlueprintContext) -> None:
        draft_json = ctx.artifacts.get("draft_plan") or self._parse_json(
            ctx.artifacts.get("draft_text") or "")
        if draft_json is None:
            raise RuntimeError("plan draft did not produce parseable Plan JSON")
        repo: Repo = ctx.artifacts["repo_row"]
        citations = self._collect_citations(draft_json)
        lint_report = self._lint_citations(repo.name, citations)
        from zagent_contracts import Plan as PlanContract
        plan_contract = PlanContract.model_validate(draft_json)
        structured = dict(draft_json)
        structured["blast_radius"] = ctx.artifacts.get("blast_radius", structured.get("blast_radius", []))
        if lint_report:
            structured["citation_lint"] = lint_report
        # M-45: replan injects the prior plan's rejection history as
        # ctx.artifacts["critic_notes"] (a str), while _critique sets
        # ctx.artifacts["critique_notes"] (this round's critic thread text).
        # _present used to read ONLY critique_notes, so the replan-injected
        # rejection history was silently dropped. Merge both (replan history
        # first, then this round's critic) into the persistent list field.
        notes: list[str] = []
        replan_notes = ctx.artifacts.get("critic_notes")
        if replan_notes:
            notes.append(replan_notes)
        critique_notes = ctx.artifacts.get("critique_notes")
        if critique_notes:
            notes.append(critique_notes)
        if notes:
            # Always a LIST: reject_plan appends per rejection round, and the UI
            # reads one shape (C1 — never str-here/list-there again).
            structured["critic_notes"] = notes

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
            run.last_active_at = datetime.now(timezone.utc)
            session.commit()
            ctx.run = run
        finally:
            session.close()

    # --------------------------------------------------------------- helpers
    def _compose_planner_prompt(self, ctx: BlueprintContext, repo: Repo) -> str:
        parts = [ctx.artifacts.get("task") or ctx.run.title]
        wi = ctx.artifacts.get("work_item")
        if wi:
            title = (wi.get("fields") or {}).get("System.Title", "")
            parts.append(f"\n--- ADO work item {ctx.run.work_item_id} ---\n{title}")
        br = ctx.artifacts.get("blast_radius") or []
        if br:
            parts.append("\n--- Fleet blast radius ---\n" + ", ".join(br))
        parts.append(f"\nTarget repo: {repo.name}")
        return "\n".join(parts)

    async def _await_thread(self, thread_id: str, poll_seconds: float = 2.0) -> None:
        import asyncio
        from app.db.models.thread import Thread
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
        fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
        candidate = fenced.group(1) if fenced else text
        start = candidate.find("{")
        end = candidate.rfind("}")
        if start == -1 or end == -1 or end < start:
            return None
        try:
            obj = json.loads(candidate[start:end + 1])
            return obj if isinstance(obj, dict) else None
        except json.JSONDecodeError:
            return None

    @staticmethod
    def _collect_citations(plan_json: dict) -> list[str]:
        seen: list[str] = []
        texts: list[str] = [plan_json.get("summary", ""), plan_json.get("title", "")]
        for s in plan_json.get("steps", []):
            texts.append(s.get("description", ""))
            texts.append(s.get("success_criterion", ""))
            texts.extend(s.get("files", []))
        for r in plan_json.get("risks", []):
            texts.append(str(r))
        for blob in texts:
            for m in CITATION_RE.finditer(blob or ""):
                c = m.group(1).rstrip(".,;)]")
                if c not in seen:
                    seen.append(c)
        return seen

    @staticmethod
    def _lint_citations(repo_name: str, citations: list[str]) -> dict | None:
        if not citations:
            return None
        try:
            from app.core.config import get_settings
            repo_root = get_settings().golden_dir / repo_name
            # Check the golden dir BEFORE importing zagent_maps — the skip
            # path (golden repo not on disk) must not depend on the maps
            # package being installed, and the heavy import shouldn't run
            # when there's nothing to lint against.
            if not repo_root.exists():
                return {"repo": repo_name, "skipped": "golden repo not on disk", "total": len(citations)}
            from zagent_maps.lint import lint
            report = lint(str(repo_root), citations)
            return report.as_dict()
        except Exception as exc:
            return {"repo": repo_name, "error": str(exc)[:200], "total": len(citations)}
