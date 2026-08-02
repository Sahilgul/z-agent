"""Width-swarm blueprint (plan §4 + Phase 3 — Agent-R&D topology).

hydrate (deterministic: repo scope + requested fan-out count, capped and said
so) -> decompose (agentic Lead: N DISTINCT slices + per-lane prompts, never
arithmetic clones; counter-proposal recorded) -> fanout (deterministic: one
read-only Explorer lane per slice, spawned CONCURRENTLY — parallel stamping
falls out of spawn_many; over-cap requests queue deterministically) -> collect
(deterministic: await all lanes, gather notebooks from the event stream —
never from self-report at collect time) -> synthesize (agentic Lead: rollup
into one answer; run.auto_summary) -> complete (deterministic: per-lane
trajectories + gateway-metered cost settle).

All lanes are READ-ONLY (§4: width swarms never write; the per-repo write lock
is not engaged). The user talks only to the Lead — Explorer lanes report
Notebook contracts, never chat.
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timezone

from zagent_contracts import Decomposition, RunStage

from app.core.config import get_settings
from app.core.logging import get_logger
from app.db.base import get_session
from app.db.models.event import Event
from app.db.models.lane import Lane
from app.db.models.mode import Mode
from app.db.models.repo import Repo, RepoStatus
from app.db.models.run import Run
from app.db.models.trajectory import TrajectorySummary
from app.orchestrator.blueprints.base import Blueprint, BlueprintContext, Node
from app.orchestrator.blueprints.plan import PlanBlueprint
from app.services.runs import transition

log = get_logger(service="swarm_blueprint")

DEFAULT_FANOUT = 3
FANOUT_RE = re.compile(r"spawn\s+(\d+)", re.IGNORECASE)

NOTEBOOK_HINT = (
    "\n\nYou are an EXPLORER lane in a width swarm. Work your assigned slice ONLY "
    "(your angle is what makes it distinct — do not duplicate sibling lanes). "
    "Everything is mounted READ-ONLY. Report ONE notebook as your final message, "
    "a fenced ```json block matching the Notebook contract: "
    '{"findings": [...], "evidence": [{"file": "...", "line": 0, "note": "..."}], '
    '"confidence": "high|medium|low", "open_questions": [...]}. '
    "Every finding carries file:line evidence — grep/read ground truth only."
)

DECOMPOSE_HINT = (
    "\n\nDivide the task into the requested number of DISTINCT, non-overlapping "
    "slices (by angle/module/concern — equal in count, never arithmetic clones of "
    "one prompt) and author each lane's prompt. If the requested count is wasteful, "
    "produce fewer slices and explain in rationale (the user may override). Reply "
    "with ONE fenced ```json block matching the Decomposition contract: "
    '{"slices": [{"title": "...", "prompt": "...", "repo": null, "angle": "..."}], '
    '"counter_proposal": null, "rationale": "..."}.'
)


class SwarmBlueprint(Blueprint):
    name = "width-swarm"

    def nodes(self) -> list[Node]:
        return [
            Node("hydrate", self._hydrate, deterministic=True, stage=RunStage.PROVISIONING),
            Node("decompose", self._decompose, deterministic=False, stage=RunStage.INVESTIGATING),
            Node("fanout", self._fanout, deterministic=True, stage=None),
            Node("collect", self._collect, deterministic=True, stage=None),
            Node("synthesize", self._synthesize, deterministic=False, stage=None),
            Node("complete", self._complete, deterministic=True, stage=RunStage.COMPLETED),
        ]

    # --------------------------------------------------------------- hydrate
    async def _hydrate(self, ctx: BlueprintContext) -> None:
        cap = get_settings().global_lane_cap
        session = get_session()
        try:
            target = ctx.artifacts.get("repo") or ctx.run.repo
            repo = None
            if target:
                repo = session.query(Repo).filter_by(name=target).one_or_none()
                if repo is None:
                    raise RuntimeError(f"repo '{target}' not registered")
            context = ([repo] if repo
                       else session.query(Repo).filter(Repo.status.in_(RepoStatus.USABLE)).all())
            mode = session.query(Mode).filter_by(name=ctx.run.mode).one_or_none()
            persona_prompt = mode.persona_prompt if mode else ""
        finally:
            session.close()

        requested = ctx.artifacts.get("fanout")
        if requested is None:
            task = ctx.artifacts.get("task") or ctx.run.title or ""
            m = FANOUT_RE.search(task)
            requested = int(m.group(1)) if m else DEFAULT_FANOUT
        requested = max(1, min(int(requested), cap))

        ctx.artifacts["repo_row"] = repo
        ctx.artifacts["context_repos"] = context
        ctx.artifacts["mode_persona"] = persona_prompt
        ctx.artifacts["requested_fanout"] = requested
        relay = ctx.services.get("relay")
        if relay and ctx.artifacts.get("fanout") and ctx.artifacts["fanout"] > requested:
            # §4: requests beyond the cap queue deterministically AND THE UI SAYS
            # SO — a swarm-scoped lane_status note, not a fake available_action.
            await relay.publish_lane_status(
                ctx.run.id, "swarm",
                f"queued: requested {ctx.artifacts['fanout']} lanes, cap {cap} — running {requested}")

    # --------------------------------------------------------------- decompose
    async def _decompose(self, ctx: BlueprintContext) -> None:
        lane_manager = ctx.services["lane_manager"]
        n = ctx.artifacts["requested_fanout"]
        task = ctx.artifacts.get("task") or ctx.run.title
        persona_prompt = ctx.artifacts["mode_persona"] + DECOMPOSE_HINT
        prompt = (
            f"Task: {task}\nRequested slices: {n}\n"
            f"Target repo: {(ctx.artifacts['repo_row'].name if ctx.artifacts['repo_row'] else 'fleet-wide')}"
        )
        lane = await lane_manager.spawn(
            ctx.run, persona="lead", prompt=prompt, persona_prompt=persona_prompt,
            writable_repo=None, context_repos=ctx.artifacts["context_repos"],
        )
        ctx.artifacts["decompose_lane_id"] = lane.id
        await _await_lane(lane.id)

        parsed = PlanBlueprint._parse_json(_last_message_text(lane.id))
        decomposition: Decomposition | None = None
        if parsed:
            try:
                decomposition = Decomposition.model_validate(parsed)
            except Exception:
                log.info("decompose output failed validation; degrading", run_id=ctx.run.id)
        if decomposition is None or not decomposition.slices:
            # Degrade to single-lane behavior instead of dying — a swarm whose
            # Lead can't decompose is still an answerable question.
            decomposition = Decomposition(
                slices=[{"title": task[:80], "prompt": task,
                         "repo": ctx.artifacts["repo_row"].name if ctx.artifacts["repo_row"] else None,
                         "angle": "whole task"}],
                rationale="decompose lane output unparsable — fell back to one slice",
            )
        ctx.artifacts["decomposition"] = decomposition

    # --------------------------------------------------------------- fanout
    async def _fanout(self, ctx: BlueprintContext) -> None:
        lane_manager = ctx.services["lane_manager"]
        decomposition: Decomposition = ctx.artifacts["decomposition"]
        specs = [
            {"persona": "explorer", "prompt": s.prompt,
             "persona_prompt": ctx.artifacts["mode_persona"] + NOTEBOOK_HINT,
             "lane_hint": f"explorer-{i}"}
            for i, s in enumerate(decomposition.slices)
        ]
        lanes = await lane_manager.spawn_many(ctx.run, specs, ctx.artifacts["context_repos"])
        ctx.artifacts["explorer_lane_ids"] = [l.id for l in lanes]
        ctx.artifacts["fanout_shortfall"] = len(decomposition.slices) - len(lanes)

    # --------------------------------------------------------------- collect
    async def _collect(self, ctx: BlueprintContext) -> None:
        lane_ids = ctx.artifacts["explorer_lane_ids"]
        await asyncio.gather(*(_await_lane(lid) for lid in lane_ids))
        notebooks: list[dict] = []
        for lid in lane_ids:
            notebooks.append({"lane_id": lid, "notebook": _notebook_for(lid),
                              "fallback_text": _last_message_text(lid)})
        ctx.artifacts["notebooks"] = notebooks

    # --------------------------------------------------------------- synthesize
    async def _synthesize(self, ctx: BlueprintContext) -> None:
        lane_manager = ctx.services["lane_manager"]
        notebooks = ctx.artifacts["notebooks"]
        task = ctx.artifacts.get("task") or ctx.run.title
        if not notebooks:
            ctx.artifacts["synthesis"] = "swarm produced no lanes to synthesize"
        else:
            persona_prompt = ctx.artifacts["mode_persona"] + (
                "\n\nYou are the LEAD synthesizing your Explorer lanes' notebooks "
                "into ONE answer for the user — consensus, disagreements, open "
                "questions. Cite file:line from their evidence. Plain prose, no JSON."
            )
            prompt = (f"Task: {task}\n\nExplorer notebooks (JSON):\n"
                      + json.dumps(notebooks, default=str)[:12000])
            lane = await lane_manager.spawn(
                ctx.run, persona="lead", prompt=prompt, persona_prompt=persona_prompt,
                writable_repo=None, context_repos=ctx.artifacts["context_repos"],
            )
            ctx.artifacts["synthesis_lane_id"] = lane.id
            await _await_lane(lane.id)
            ctx.artifacts["synthesis"] = _last_message_text(lane.id) or ""

        session = get_session()
        try:
            run = session.get(Run, ctx.run.id)
            decomp: Decomposition = ctx.artifacts["decomposition"]
            notes = []
            if decomp.counter_proposal:
                notes.append(f"Lead counter-proposed {decomp.counter_proposal} lanes: {decomp.rationale}")
            if ctx.artifacts.get("fanout_shortfall"):
                notes.append(f"{ctx.artifacts['fanout_shortfall']} lane(s) failed to spawn")
            run.auto_summary = (ctx.artifacts["synthesis"] or "")[:2000]
            if notes:
                run.auto_summary = (run.auto_summary + "\n\n" + " · ".join(notes))[:2000]
            session.commit()
        finally:
            session.close()

    # --------------------------------------------------------------- complete
    async def _complete(self, ctx: BlueprintContext) -> None:
        lane_manager = ctx.services["lane_manager"]
        lane_ids = ctx.artifacts.get("explorer_lane_ids", [])
        session = get_session()
        try:
            lanes = [session.get(Lane, lid) for lid in lane_ids]
            failed = [l for l in lanes if l and l.status == "failed"]
            run = session.get(Run, ctx.run.id)
            if lane_ids and len(failed) == len(lane_ids):
                transition(run, RunStage.FAILED)
                session.commit()
                return
            for lane in lanes:
                if lane is None:
                    continue
                session.add(TrajectorySummary(
                    run_id=run.id, lane_id=lane.id, user_id=run.created_by,
                    summary=f"explorer slice on {lane.repo_scope or ctx.run.repo or 'fleet'}",
                ))
            if failed:
                run.auto_summary = ((run.auto_summary or "")
                                    + f"\n\n{len(failed)} explorer lane(s) failed")[:2000]
            run.finished_at = datetime.now(timezone.utc)
            session.commit()
        finally:
            session.close()
        for lid in lane_ids:
            await lane_manager.settle_cost(lid)


# ------------------------------------------------------------------- helpers
async def _await_lane(lane_id: str, poll_seconds: float = 2.0) -> None:
    while True:
        session = get_session()
        try:
            lane = session.get(Lane, lane_id)
            status = lane.status if lane else "failed"
        finally:
            session.close()
        if status in ("idle", "completed", "failed", "stopped"):
            return
        await asyncio.sleep(poll_seconds)


def _last_message_text(lane_id: str) -> str | None:
    session = get_session()
    try:
        row = (
            session.query(Event)
            .filter_by(lane_id=lane_id, type="message")
            .order_by(Event.seq.desc())
            .first()
        )
        if row is None:
            return None
        payload = row.payload or {}
        return payload.get("text") if isinstance(payload, dict) else None
    finally:
        session.close()


def _notebook_for(lane_id: str) -> dict | None:
    """The lane's Notebook contract as stored by the ingest — collect reads the
    EVENT STREAM, never the agent's say-so at collect time (tamper-proof)."""
    session = get_session()
    try:
        row = (
            session.query(Event)
            .filter_by(lane_id=lane_id, type="notebook")
            .order_by(Event.seq.desc())
            .first()
        )
        return row.payload if row and isinstance(row.payload, dict) else None
    finally:
        session.close()
