"""Ask mode blueprint: single read-only thread.

hydrate (deterministic) -> investigate (agentic: one thread, live-grep ground
truth, hand-written ServerApp AGENTS.md seed until maps arrive) ->
await_user (deterministic: cost readback + trajectory summary, then PARK at
awaiting_user). Ask is a CONVERSATION, not a one-shot: the run used to land
on COMPLETED after the first answer, which locked the composer (terminal
stage) and made follow-up questions impossible. Parking at awaiting_user
keeps the lead thread idle-lingering for nudges; when the thread's idle TTL
finally completes it, the heartbeat persister flips the run to COMPLETED.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from collegium_contracts import RunStage

from app.db.base import get_session
from app.db.models.mode import Mode
from app.db.models.repo import Repo
from app.db.models.thread import Thread
from app.db.models.trajectory import TrajectorySummary
from app.orchestrator.blueprints.base import Blueprint, BlueprintContext, Node, media_args
from app.services.runs import transition

GUIDEBOOK_SEED = Path(__file__).parent / "assets" / "ServerApp.AGENTS.md"


class AskBlueprint(Blueprint):
    name = "ask"

    def nodes(self) -> list[Node]:
        return [
            Node("hydrate", self._hydrate, deterministic=True, stage=RunStage.PROVISIONING),
            Node("investigate", self._investigate, deterministic=False, stage=RunStage.INVESTIGATING),
            # Park, don't finish: COMPLETED is terminal (composer locked), so
            # an answered question killed the conversation. AWAITING_USER
            # keeps the session open; completion arrives via the thread's
            # idle TTL -> heartbeat persister (see heartbeats._maybe_complete_ask_run).
            Node("complete", self._complete, deterministic=True, stage=RunStage.AWAITING_USER),
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
        # Model selection (validated at create_run): one alias = the run's
        # lane uses it; several = compare mode, one lane per model answering
        # the same prompt in parallel. None = the deployment default.
        models: list[str] = ctx.artifacts.get("models") or []
        reasoning_map: dict[str, str] = ctx.artifacts.get("reasoning") or {}
        if len(models) <= 1:
            from app.core.config import get_settings
            model = models[0] if models else None
            thread = await thread_manager.spawn(
                ctx.run, persona="researcher", prompt=task, persona_prompt=persona_prompt,
                writable_repo=None, context_repos=context,
                resume_from_thread_id=ctx.artifacts.get("resume_from_thread_id"),
                model=model,
                reasoning=reasoning_map.get(model or get_settings().gateway_model),
                **media_args(ctx),
            )
            ctx.artifacts["thread_id"] = thread.id
            ctx.artifacts["thread_ids"] = [thread.id]
            # Block until the thread's turn ends (idle/completed/failed).
            await self._await_thread(thread.id)
            return

        # Compare fan-out: lanes are distinguished by model, so the persona
        # carries the registry label (chips/lanes show it) — the researcher
        # ROLE and prompt are identical across lanes; the model is the only
        # variable under comparison.
        from app.core.config import get_settings
        settings = get_settings()

        async def _spawn_lane(alias: str) -> Thread:
            option = settings.model_option(alias)
            return await thread_manager.spawn(
                ctx.run, persona=option.label if option else alias,
                prompt=task, persona_prompt=persona_prompt,
                writable_repo=None, context_repos=context, model=alias,
                reasoning=reasoning_map.get(alias), **media_args(ctx),
            )

        results = await asyncio.gather(
            *(_spawn_lane(m) for m in models), return_exceptions=True)
        lanes = [r for r in results if isinstance(r, Thread)]
        failures = [r for r in results if isinstance(r, BaseException)]
        if not lanes:
            # Every lane failed to start — same semantics as the single-model
            # path raising: the run fails with the real reason.
            raise failures[0]
        if failures:
            # Partial start: the surviving lanes still answer. Say so in the
            # stream — a silently missing lane reads as "that model had
            # nothing to say".
            relay = ctx.services.get("relay")
            if relay is not None:
                await relay.publish_note(
                    ctx.run.id,
                    f"{len(failures)} of {len(models)} model lanes failed to start: "
                    f"{str(failures[0])[:200]}")
        ctx.artifacts["thread_id"] = lanes[0].id  # legacy single-lane readers
        ctx.artifacts["thread_ids"] = [t.id for t in lanes]
        await asyncio.gather(*(self._await_thread(t.id) for t in lanes))

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
                          # and the blueprint stuck forever. A4: input_required
                          # (approval-parked) is terminal for the blueprint —
                          # the run stage tracks the human wait.
                          "interrupted", "replaced", "input_required"):
                return
            await asyncio.sleep(poll_seconds)

    async def _complete(self, ctx: BlueprintContext) -> None:
        thread_manager = ctx.services["thread_manager"]
        # Compare runs carry several lanes; single-model runs carry one (the
        # legacy thread_id key is kept for pre-fanout callers).
        thread_ids: list[str] = (
            ctx.artifacts.get("thread_ids")
            or ([ctx.artifacts["thread_id"]] if ctx.artifacts.get("thread_id") else []))
        settle_ids: list[str] = []
        session = get_session()
        try:
            run = ctx.run
            lanes = {tid: session.get(Thread, tid) for tid in thread_ids}
            failed = [tid for tid, t in lanes.items() if t is None or t.status == "failed"]
            survivors = [tid for tid in thread_ids if tid not in failed]
            if thread_ids and not survivors:
                # Every lane failed — the run has no answer to show.
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
            for tid in survivors:
                session.add(TrajectorySummary(
                    run_id=run.id, thread_id=tid, user_id=run.created_by,
                    summary=run.auto_summary or f"Ask run on {run.repo or 'repo'}: {run.title[:200]}",
                ))
            # No finished_at here: the run is PARKED at awaiting_user, not
            # over. finished_at is stamped when the persister completes the
            # run (thread idle TTL) — marking it now would lie about liveness.
            session.commit()
            settle_ids = survivors
        finally:
            session.close()
        for tid in settle_ids:
            await thread_manager.settle_cost(tid)
