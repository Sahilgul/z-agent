"""Width-swarm blueprint — Agent-R&D topology.

hydrate (deterministic: repo scope + requested fan-out count, capped and said
so) -> decompose (agentic Lead: N DISTINCT slices + per-thread prompts, never
arithmetic clones; counter-proposal recorded) -> fanout (deterministic: one
read-only Explorer thread per slice, spawned CONCURRENTLY — parallel stamping
falls out of spawn_many; over-cap requests queue deterministically) -> collect
(deterministic: await all threads, gather notebooks from the event stream —
never from self-report at collect time) -> synthesize (agentic Lead: rollup
into one answer; run.auto_summary) -> complete (deterministic: per-thread
trajectories + gateway-metered cost settle).

All threads are READ-ONLY (width swarms never write; the per-repo write lock
is not engaged). The user talks only to the Lead — Explorer threads report
Notebook contracts, never chat.
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import UTC, datetime

from collegium_contracts import Decomposition, RunStage

from app.core.config import get_settings
from app.core.logging import get_logger
from app.db.base import get_session
from app.db.models.event import Event
from app.db.models.mode import Mode
from app.db.models.repo import Repo, RepoStatus
from app.db.models.run import Run
from app.db.models.thread import Thread
from app.db.models.trajectory import TrajectorySummary
from app.orchestrator.blueprints.base import Blueprint, BlueprintContext, Node, lane_override, media_args
from app.orchestrator.blueprints.goal import THREAD_MAX_WAIT_S
from app.orchestrator.blueprints.plan import PlanBlueprint
from app.services.runs import transition

log = get_logger(service="swarm_blueprint")

DEFAULT_FANOUT = 3
FANOUT_RE = re.compile(r"spawn\s+(\d+)", re.IGNORECASE)

NOTEBOOK_HINT = (
    "\n\nYou are an EXPLORER thread in a width swarm. Work your assigned slice ONLY "
    "(your angle is what makes it distinct — do not duplicate sibling threads). "
    "Everything is mounted READ-ONLY. Report ONE notebook as your final message, "
    "a fenced ```json block matching the Notebook contract: "
    '{"findings": [...], "evidence": [{"file": "...", "line": 0, "note": "..."}], '
    '"confidence": "high|medium|low", "open_questions": [...]}. '
    "Every finding carries file:line evidence — grep/read ground truth only."
)

DECOMPOSE_HINT = (
    "\n\nDivide the task into the requested number of DISTINCT, non-overlapping "
    "slices (by angle/module/concern — equal in count, never arithmetic clones of "
    "one prompt) and author each thread's prompt. If the requested count is wasteful, "
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
        from app.services.mentions import resolve_run_repos
        cap = get_settings().global_thread_cap
        session = get_session()
        try:
            # Fleet mode: no target + no @mention -> mount the WHOLE usable
            # fleet as read-only context (the existing fallback). A target
            # (explicit repo or @mention) narrows context to the named repos.
            target, mentioned, unknown = resolve_run_repos(
                session, ctx.artifacts.get("repo") or ctx.run.repo,
                ctx.artifacts.get("task") or ctx.run.title)
            if unknown:
                raise RuntimeError(
                    f"repo '{unknown[0]}' not registered — mention a registered repo with `@Name`")
            repo = target
            if repo is not None:
                context = mentioned or [repo]
            else:
                context = session.query(Repo).filter(Repo.status.in_(RepoStatus.USABLE)).all()
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
            # Requests beyond the cap queue deterministically AND THE UI SAYS
            # SO — a run-scoped note (L-22: was a misuse of publish_thread_status
            # with a fake thread id "swarm" and a sentence as the status, which
            # the UI silently dropped). Use the dedicated publish_note channel.
            await relay.publish_note(
                ctx.run.id,
                f"queued: requested {ctx.artifacts['fanout']} threads, cap {cap} — running {requested}")

    # --------------------------------------------------------------- decompose
    async def _decompose(self, ctx: BlueprintContext) -> None:
        thread_manager = ctx.services["thread_manager"]
        n = ctx.artifacts["requested_fanout"]
        task = ctx.artifacts.get("task") or ctx.run.title
        persona_prompt = ctx.artifacts["mode_persona"] + DECOMPOSE_HINT
        prompt = (
            f"Task: {task}\nRequested slices: {n}\n"
            f"Target repo: {(ctx.artifacts['repo_row'].name if ctx.artifacts['repo_row'] else 'fleet-wide')}"
        )
        model, reasoning = lane_override(ctx)
        thread = await thread_manager.spawn(
            ctx.run, persona="lead", prompt=prompt, persona_prompt=persona_prompt,
            writable_repo=None, context_repos=ctx.artifacts["context_repos"],
            resume_from_thread_id=ctx.artifacts.get("resume_from_thread_id"),
            model=model, reasoning=reasoning, **media_args(ctx),
        )
        ctx.artifacts["decompose_thread_id"] = thread.id
        await _await_thread(thread.id)

        parsed = PlanBlueprint._parse_json(_last_message_text(thread.id))
        decomposition: Decomposition | None = None
        if parsed:
            try:
                decomposition = Decomposition.model_validate(parsed)
            except Exception:
                log.info("decompose output failed validation; degrading", run_id=ctx.run.id)
        if decomposition is None or not decomposition.slices:
            # Degrade to single-thread behavior instead of dying — a swarm whose
            # Lead can't decompose is still an answerable question.
            decomposition = Decomposition(
                slices=[{"title": task[:80], "prompt": task,
                         "repo": ctx.artifacts["repo_row"].name if ctx.artifacts["repo_row"] else None,
                         "angle": "whole task"}],
                rationale="decompose thread output unparsable — fell back to one slice",
            )
        ctx.artifacts["decomposition"] = decomposition

    # --------------------------------------------------------------- fanout
    async def _fanout(self, ctx: BlueprintContext) -> None:
        thread_manager = ctx.services["thread_manager"]
        decomposition: Decomposition = ctx.artifacts["decomposition"]
        model, reasoning = lane_override(ctx)
        specs = [
            {"persona": "explorer", "prompt": s.prompt,
             "persona_prompt": ctx.artifacts["mode_persona"] + NOTEBOOK_HINT,
             "thread_hint": f"explorer-{i}",
             # The composer's single-model override applies to every slice —
             # a swarm on deepseek-pro is uniformly deepseek-pro.
             **({"model": model, "reasoning": reasoning} if model else {}),
             # Attachments ride every slice: vision slices see them natively,
             # blind slices get the pre-pass description in their prompt.
             **media_args(ctx)}
            for i, s in enumerate(decomposition.slices)
        ]
        threads = await thread_manager.spawn_many(ctx.run, specs, ctx.artifacts["context_repos"])
        ctx.artifacts["explorer_thread_ids"] = [l.id for l in threads]
        ctx.artifacts["fanout_shortfall"] = len(decomposition.slices) - len(threads)

    # --------------------------------------------------------------- collect
    async def _collect(self, ctx: BlueprintContext) -> None:
        thread_ids = ctx.artifacts["explorer_thread_ids"]
        # E4: per-slice isolation — a wedged explorer raises inside its own
        # await instead of parking the gather; the surviving notebooks still
        # synthesize and the wedged thread is finished + counted.
        outcomes = await asyncio.gather(
            *(_await_thread(lid) for lid in thread_ids), return_exceptions=True)
        wedged = [lid for lid, o in zip(thread_ids, outcomes, strict=True)
                  if isinstance(o, Exception)]
        if wedged:
            log.warning("swarm explorers wedged — continuing without them",
                        run_id=ctx.run.id, thread_ids=wedged)
            ctx.artifacts["wedged_explorer_ids"] = wedged
            thread_manager = ctx.services["thread_manager"]
            for lid in wedged:
                try:
                    await thread_manager.finish_thread(lid, "failed")
                except Exception:
                    log.warning("wedged explorer finish failed", thread_id=lid)
        notebooks: list[dict] = []
        for lid in thread_ids:
            notebooks.append({"thread_id": lid, "notebook": _notebook_for(lid),
                              "fallback_text": _last_message_text(lid)})
        ctx.artifacts["notebooks"] = notebooks

    # --------------------------------------------------------------- synthesize
    async def _synthesize(self, ctx: BlueprintContext) -> None:
        thread_manager = ctx.services["thread_manager"]
        notebooks = ctx.artifacts["notebooks"]
        task = ctx.artifacts.get("task") or ctx.run.title
        if not notebooks:
            ctx.artifacts["synthesis"] = "swarm produced no threads to synthesize"
        else:
            persona_prompt = ctx.artifacts["mode_persona"] + (
                "\n\nYou are the LEAD synthesizing your Explorer threads' notebooks "
                "into ONE answer for the user — consensus, disagreements, open "
                "questions. Cite file:line from their evidence. Plain prose, no JSON."
            )
            prompt = (f"Task: {task}\n\nExplorer notebooks (JSON):\n"
                      + json.dumps(notebooks, default=str)[:12000])
            model, reasoning = lane_override(ctx)
            thread = await thread_manager.spawn(
                ctx.run, persona="lead", prompt=prompt, persona_prompt=persona_prompt,
                writable_repo=None, context_repos=ctx.artifacts["context_repos"],
                model=model, reasoning=reasoning, **media_args(ctx),
            )
            ctx.artifacts["synthesis_thread_id"] = thread.id
            await _await_thread(thread.id)
            ctx.artifacts["synthesis"] = _last_message_text(thread.id) or ""

        session = get_session()
        try:
            run = session.get(Run, ctx.run.id)
            decomp: Decomposition = ctx.artifacts["decomposition"]
            notes = []
            if decomp.counter_proposal:
                notes.append(f"Lead counter-proposed {decomp.counter_proposal} threads: {decomp.rationale}")
            if ctx.artifacts.get("fanout_shortfall"):
                notes.append(f"{ctx.artifacts['fanout_shortfall']} thread(s) failed to spawn")
            run.auto_summary = (ctx.artifacts["synthesis"] or "")[:2000]
            if notes:
                run.auto_summary = (run.auto_summary + "\n\n" + " · ".join(notes))[:2000]
            session.commit()
        finally:
            session.close()

    # --------------------------------------------------------------- complete
    async def _complete(self, ctx: BlueprintContext) -> None:
        thread_manager = ctx.services["thread_manager"]
        thread_ids = ctx.artifacts.get("explorer_thread_ids", [])
        session = get_session()
        try:
            threads = [session.get(Thread, lid) for lid in thread_ids]
            failed = [l for l in threads if l and l.status == "failed"]
            run = session.get(Run, ctx.run.id)
            if thread_ids and len(failed) == len(thread_ids):
                transition(run, RunStage.FAILED)
                session.commit()
                # H-39: re-publish the FAILED stage — this node's stage is
                # COMPLETED so the UI was already told "completed" (best-
                # effort: the unit test calls _complete without a relay).
                relay = ctx.services.get("relay")
                if relay is not None:
                    await relay.publish_run_stage(
                        run.id, RunStage.FAILED.value, run.available_actions)
                return
            for thread in threads:
                if thread is None:
                    continue
                session.add(TrajectorySummary(
                    run_id=run.id, thread_id=thread.id, user_id=run.created_by,
                    summary=f"explorer slice on {thread.repo_scope or ctx.run.repo or 'fleet'}",
                ))
            if failed:
                run.auto_summary = ((run.auto_summary or "")
                                    + f"\n\n{len(failed)} explorer thread(s) failed")[:2000]
            run.finished_at = datetime.now(UTC)
            session.commit()
        finally:
            session.close()
        for lid in thread_ids:
            # E4/F1: explorers idle-lingered until the TTL, holding capacity
            # after the run completed. finish_thread stamps terminal, stops
            # the container, and runs the unified settle/release/clear.
            await thread_manager.finish_thread(lid)
            await thread_manager.settle_cost(lid)
        # M-47: the decompose (Lead) and synthesis threads were never
        # cost-settled — their gateway keys and spend leaked (never
        # released/folded into the run). Settle them alongside explorers.
        decompose_id = ctx.artifacts.get("decompose_thread_id")
        if decompose_id:
            await thread_manager.settle_cost(decompose_id)
        synthesis_id = ctx.artifacts.get("synthesis_thread_id")
        if synthesis_id:
            await thread_manager.settle_cost(synthesis_id)


# ------------------------------------------------------------------- helpers
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
            # E4: without a bound, ONE wedged explorer parked the gather
            # forever and its finished siblings idled with capacity + locks
            # held until the idle TTL.
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


def _notebook_for(thread_id: str) -> dict | None:
    """The thread's Notebook contract as stored by the ingest — collect reads the
    EVENT STREAM, never the agent's say-so at collect time (tamper-proof)."""
    session = get_session()
    try:
        row = (
            session.query(Event)
            .filter_by(thread_id=thread_id, type="notebook")
            .order_by(Event.seq.desc())
            .first()
        )
        return row.payload if row and isinstance(row.payload, dict) else None
    finally:
        session.close()
