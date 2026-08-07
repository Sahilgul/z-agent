"""Ask mode blueprint: single read-only thread.

hydrate (deterministic) -> investigate (agentic: one thread, live-grep ground
truth, hand-written ServerApp AGENTS.md seed until maps arrive) ->
complete (deterministic: cost readback + trajectory summary).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

from collegium_contracts import RunStage

from app.db.base import get_session
from app.db.models.thread import Thread
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
        """Deterministic pre-run hydration (ado/hydrate grows from here):
        resolve target + context repos from the explicit field and the task's
        @mentions, load guidebook seed, compose the thread prompt.

        No default repo: a repo-less ask run is a general-assistant chat — the
        agent answers questions, explains concepts, discusses architecture
        without file access. The old `or "ServerApp"` fallback silently scoped
        every repo-less ask run to ServerApp, hiding the fleet from the agent.
        An @mention (or the API's run.repo) opts INTO file access."""
        from app.services.mentions import resolve_run_repos
        session = get_session()
        try:
            target, context, unknown = resolve_run_repos(
                session, ctx.artifacts.get("repo") or ctx.run.repo,
                ctx.artifacts.get("task") or ctx.run.title)
            if unknown:
                raise RuntimeError(
                    f"repo '{unknown[0]}' not registered — mention a registered repo with `@Name`")
            guidebook = GUIDEBOOK_SEED.read_text(encoding="utf-8") if GUIDEBOOK_SEED.exists() else ""
        finally:
            session.close()
        ctx.artifacts["repo_row"] = target
        ctx.artifacts["context_repos"] = context
        ctx.artifacts["guidebook"] = guidebook

    async def _investigate(self, ctx: BlueprintContext) -> None:
        thread_manager = ctx.services["thread_manager"]
        session = get_session()
        try:
            mode = session.query(Mode).filter_by(name="ask").one_or_none()
        finally:
            session.close()
        repo: Repo | None = ctx.artifacts["repo_row"]
        context: list[Repo] = ctx.artifacts.get("context_repos") or ([repo] if repo else [])
        task = ctx.artifacts.get("task") or ctx.run.title
        if repo is not None:
            persona_prompt = (mode.persona_prompt if mode else "") + (
                f"\n\n--- Repo guidebook (curated) ---\n{ctx.artifacts['guidebook']}"
                "\n\nNavigation protocol: orient in the guidebook, then grep/glob/read on the "
                "mounted tree as ground truth. Answer with file:line citations. "
                "Everything is mounted READ-ONLY — do not modify anything."
            )
        else:
            # No repo mentioned — general-assistant mode. The agent can answer
            # questions, explain concepts, discuss architecture. It has no file
            # access; an @mention in a later turn (remount) opts into that.
            persona_prompt = (mode.persona_prompt if mode else "") + (
                "\n\nNo repo is mounted — you are in general-assistant mode. Answer "
                "questions, explain concepts, and discuss architecture from your "
                "training knowledge. You do NOT have file access right now. If the "
                "user wants you to look at specific code, they can mention a repo "
                "with `@RepoName` and you'll get read-only access to it on the next turn."
            )
        thread = await thread_manager.spawn(
            ctx.run, persona="researcher", prompt=task, persona_prompt=persona_prompt,
            writable_repo=None, context_repos=context,
            resume_from_thread_id=ctx.artifacts.get("resume_from_thread_id"),
        )
        ctx.artifacts["thread_id"] = thread.id
        # Block until the thread's turn ends (idle/completed/failed).
        await self._await_thread(thread.id)

    async def _await_thread(self, thread_id: str, poll_seconds: float = 2.0) -> None:
        while True:
            session = get_session()
            try:
                thread = session.get(Thread, thread_id)
                status = thread.status if thread else "failed"
            finally:
                session.close()
            if status in ("idle", "completed", "failed", "stopped",
                          # H-38: interrupted (nudge/stop) and replaced
                          # (kill-replace) are terminal too — the old set
                          # omitted them so the await loop never returned
                          # and the blueprint stuck forever.
                          "interrupted", "replaced"):
                return
            await asyncio.sleep(poll_seconds)

    async def _complete(self, ctx: BlueprintContext) -> None:
        thread_manager = ctx.services["thread_manager"]
        session = get_session()
        try:
            run = ctx.run
            thread_id = ctx.artifacts.get("thread_id")
            if thread_id:
                thread = session.get(Thread, thread_id)
                if thread and thread.status == "failed":
                    transition(run, RunStage.FAILED)
                    session.commit()
                    # H-39: this node's stage is COMPLETED, so the UI was
                    # already told "completed"; overriding to FAILED without
                    # re-publishing left the UI showing "completed" while
                    # the DB said FAILED. Re-publish the FAILED stage (best-
                    # effort: the unit test calls _complete without a relay).
                    relay = ctx.services.get("relay")
                    if relay is not None:
                        await relay.publish_run_stage(
                            run.id, RunStage.FAILED.value, run.available_actions)
                    return
                # Trajectory summaries written FROM DAY ONE (distiller history).
                session.add(TrajectorySummary(
                    run_id=run.id, thread_id=thread_id, user_id=run.created_by,
                    summary=run.auto_summary or f"Ask run on {run.repo or 'repo'}: {run.title[:200]}",
                ))
            run.finished_at = datetime.now(timezone.utc)
            session.commit()
        finally:
            session.close()
        if ctx.artifacts.get("thread_id"):
            await thread_manager.settle_cost(ctx.artifacts["thread_id"])
