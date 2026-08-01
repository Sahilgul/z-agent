"""Ask mode blueprint (Phase 1 first milestone): single read-only lane.

hydrate (deterministic) -> investigate (agentic: one lane, live-grep ground
truth, hand-written ServerApp AGENTS.md seed until maps arrive in Phase 2) ->
complete (deterministic: cost readback + trajectory summary).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

from zagent_contracts import RunStage

from app.db.base import get_session
from app.db.models.lane import Lane
from app.db.models.mode import Mode
from app.db.models.repo import Repo
from app.db.models.trajectory import TrajectorySummary
from app.orchestrator.blueprints.base import Blueprint, BlueprintContext, Node
from app.services.runs import transition

GUIDEBOOK_SEED = Path(__file__).parent / "assets" / "ServerApp.AGENTS.md"


class AskBlueprint(Blueprint):
    name = "ask"

    def nodes(self) -> list[Node]:
        return [
            Node("hydrate", self._hydrate, deterministic=True, stage=RunStage.PROVISIONING),
            Node("investigate", self._investigate, deterministic=False, stage=RunStage.INVESTIGATING),
            Node("complete", self._complete, deterministic=True, stage=RunStage.COMPLETED),
        ]

    async def _hydrate(self, ctx: BlueprintContext) -> None:
        """Deterministic pre-run hydration (plan §8 ado/hydrate grows from here):
        resolve target repo, load guidebook seed, compose the lane prompt."""
        session = get_session()
        try:
            target = ctx.artifacts.get("repo") or ctx.run.repo or "ServerApp"
            repo = session.query(Repo).filter_by(name=target).one_or_none()
            if repo is None:
                raise RuntimeError(f"repo '{target}' not registered")
            guidebook = GUIDEBOOK_SEED.read_text(encoding="utf-8") if GUIDEBOOK_SEED.exists() else ""
        finally:
            session.close()
        ctx.artifacts["repo_row"] = repo
        ctx.artifacts["guidebook"] = guidebook

    async def _investigate(self, ctx: BlueprintContext) -> None:
        lane_manager = ctx.services["lane_manager"]
        session = get_session()
        try:
            mode = session.query(Mode).filter_by(name="ask").one_or_none()
        finally:
            session.close()
        persona_prompt = (mode.persona_prompt if mode else "") + (
            f"\n\n--- Repo guidebook (curated) ---\n{ctx.artifacts['guidebook']}"
            "\n\nNavigation protocol: orient in the guidebook, then grep/glob/read on the "
            "mounted tree as ground truth. Answer with file:line citations. "
            "Everything is mounted READ-ONLY — do not modify anything."
        )
        repo: Repo = ctx.artifacts["repo_row"]
        task = ctx.artifacts.get("task") or ctx.run.title
        lane = await lane_manager.spawn(
            ctx.run, persona="researcher", prompt=task, persona_prompt=persona_prompt,
            writable_repo=None, context_repos=[repo],
        )
        ctx.artifacts["lane_id"] = lane.id
        # Phase 1: block until the lane's turn ends (idle/completed/failed).
        await self._await_lane(lane.id)

    async def _await_lane(self, lane_id: str, poll_seconds: float = 2.0) -> None:
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

    async def _complete(self, ctx: BlueprintContext) -> None:
        lane_manager = ctx.services["lane_manager"]
        session = get_session()
        try:
            run = ctx.run
            lane_id = ctx.artifacts.get("lane_id")
            if lane_id:
                lane = session.get(Lane, lane_id)
                if lane and lane.status == "failed":
                    transition(run, RunStage.FAILED)
                    session.commit()
                    return
                # Trajectory summaries written FROM DAY ONE (distiller history).
                session.add(TrajectorySummary(
                    run_id=run.id, lane_id=lane_id, user_id=run.created_by,
                    summary=run.auto_summary or f"Ask run on {run.repo or 'repo'}: {run.title[:200]}",
                ))
            run.finished_at = datetime.now(timezone.utc)
            session.commit()
        finally:
            session.close()
        if ctx.artifacts.get("lane_id"):
            await lane_manager.settle_cost(ctx.artifacts["lane_id"])
