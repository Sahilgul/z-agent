"""Thread manager: spawn/control threads end-to-end.

Spawn: capacity semaphore -> mint per-thread LiteLLM virtual key (the cost data
path) -> Thread row -> worker container with session volume + stamp/mounts ->
ingest stream registered. Cost readback at thread end reconciles from gateway
metering (grace window), filling thread/run cost fields.

PREWARM_POOL (semantics DEFINED, implementation lands with the VM move):
A small fleet of pre-started worker containers held ready so the first thread of a
run spawns in ~1s instead of cold-starting. Semantics:
  * Pool workers are GENERIC: no per-thread LiteLLM virtual key and no session
    volume at prewarm time. Both are LATE-BOUND at claim time — claiming a pool
    worker mints the thread's gateway key and mounts sessions/<run_id>/<thread_id>/.
  * Golden repos are mounted read-only at prewarm; writable clones are stamped
    per-thread at claim (stamp_clone is seconds, so this is not the cold path).
  * Capacity derives from settings.global_thread_cap: pool_size =
    min(settings.prewarm_pool_size, global_thread_cap); pool workers hold semaphore
    slots so a full pool can never starve interactive spawns.
  * Claim order is FIFO; a claimed worker is replaced asynchronously.
prewarm_status() below is the documented-not-implemented stub the API surfaces.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone

from app.core.config import get_settings
from app.core.logging import get_logger
from app.db.base import get_session
from app.db.models.thread import Thread
from app.db.models.repo import Repo
from app.db.models.run import Run
from app.events.bus import IngestConsumer
from app.events.relay import Relay
from app.gateway.litellm import GatewayClient
from app.orchestrator.autonomy import permission_mode_for
from app.orchestrator.semaphores import capacity
from app.sandbox.manager import sandbox_manager

log = get_logger(service="thread_manager")


def prewarm_status() -> dict:
    """Documented-not-implemented stub: the pool semantics are DEFINED
    in this module's docstring; the live pool lands with the VM move. The API
    surfaces this verbatim so the UI never implies warmth that doesn't exist."""
    settings = get_settings()
    return {
        "enabled": False,
        "pool_size": 0,
        "available": 0,
        "capacity_from": "settings.global_thread_cap",
        "note": ("prewarm pool defined, not implemented — late-bound gateway key + "
                 "session volume at claim time; capacity "
                 f"min(prewarm_pool_size={settings.prewarm_pool_size}, "
                 f"global_thread_cap={settings.global_thread_cap})"),
    }


class ThreadSpawnError(RuntimeError):
    pass


class ThreadManager:
    def __init__(self, ingest: IngestConsumer, relay: Relay, gateway: GatewayClient) -> None:
        self.ingest = ingest
        self.relay = relay
        self.gateway = gateway
        self.settings = get_settings()

    async def spawn(self, run: Run, persona: str, prompt: str, persona_prompt: str,
                    writable_repo: Repo | None, context_repos: list[Repo],
                    resume_session: bool = False,
                    resume_from_thread_id: str | None = None) -> Thread:
        repo_name = writable_repo.name if writable_repo else None
        # Flywheel injection: pinned knowledge + the owner's
        # episodic recall join every thread's persona prompt. Cached per run, so
        # only the first thread pays the rerank; stored in spawn_context so a
        # kill/replace replay reproduces the exact same prompt.
        from app.services import knowledge
        persona_prompt += await knowledge.prompt_block_for_run(
            run.id, run.title, run.created_by, repo_name or run.repo)
        ok, reason = await capacity.try_acquire(repo_name)
        if not ok:
            raise ThreadSpawnError(reason)

        # When resuming from a prior thread (mode switch or kill-replace),
        # inherit its session_id so the SDK picks up the conversation. The
        # container mounts the prior thread's session volume (wired in
        # run_thread_container via resume_from_thread_id).
        inherited_session_id: str | None = None
        if resume_from_thread_id:
            session = get_session()
            try:
                prior = session.get(Thread, resume_from_thread_id)
                if prior is not None:
                    inherited_session_id = prior.session_id
            finally:
                session.close()

        thread = Thread(
            id=str(uuid.uuid4()), run_id=run.id, persona=persona,
            repo_scope=repo_name,
            status="queued", budget_usd=self.settings.default_thread_budget_usd,
            session_id=inherited_session_id,
            spawn_context={"prompt": prompt, "persona_prompt": persona_prompt,
                           "resume_session": resume_session,
                           "mode": run.mode,
                           **({"resume_from_thread_id": resume_from_thread_id}
                              if resume_from_thread_id else {})},
        )
        session = get_session()
        try:
            session.add(thread)
            session.commit()
        finally:
            session.close()
        # Row exists -> active_thread_count owns the slot now (see Capacity).
        capacity.commit_reservation(repo_name)

        try:
            vk = await self.gateway.mint_key(
                alias=f"thread-{thread.id[:8]}", max_budget_usd=thread.budget_usd,
            )
            thread.gateway_key = vk.key
            thread.gateway_key_alias = vk.alias
        except Exception as exc:  # gateway-down: fail safe before container start
            self._mark(thread.id, "failed")
            raise ThreadSpawnError(f"gateway key mint failed: {exc}") from exc

        permission_mode = permission_mode_for(run.autonomy)
        try:
            container_id = await asyncio.to_thread(
                sandbox_manager.run_thread_container,
                run, thread, prompt, persona_prompt, permission_mode,
                writable_repo, context_repos,
                resume_from_thread_id=resume_from_thread_id,
            )
        except Exception as exc:
            self._mark(thread.id, "failed")
            raise ThreadSpawnError(f"container start failed: {exc}") from exc

        session = get_session()
        try:
            row = session.get(Thread, thread.id)
            row.container_id = container_id
            row.status = "running"
            row.gateway_key = thread.gateway_key
            row.gateway_key_alias = thread.gateway_key_alias
            row.heartbeat_at = datetime.now(timezone.utc)
            session.commit()
        finally:
            session.close()

        self.ingest.register_run(run.id)
        await self.relay.publish_thread_status(run.id, thread.id, "running")
        return thread

    async def spawn_many(self, run: Run, specs: list[dict],
                         context_repos: list[Repo],
                         queue_poll_seconds: float = 5.0) -> list[Thread]:
        """Width-swarm fan-out: spawn one thread per spec
        CONCURRENTLY — parallel stamping falls out of each spawn running its own
        stamp in a thread. Requests beyond the global cap queue deterministically
        and the UI says so (a single swarm_queued relay note per waiting thread);
        a thread that still fails (gateway/container) is marked failed and does not
        sink the rest of the swarm. Read-only personas pass writable_repo=None —
        the per-repo write lock never engages for Explorer threads."""
        async def _spawn_one(spec: dict) -> Thread | None:
            announced = False
            while True:
                try:
                    return await self.spawn(
                        run, persona=spec["persona"], prompt=spec["prompt"],
                        persona_prompt=spec["persona_prompt"],
                        writable_repo=None, context_repos=context_repos,
                    )
                except ThreadSpawnError as exc:
                    if "queued" not in str(exc):
                        log.error("swarm thread spawn failed", run_id=run.id,
                                  persona=spec.get("persona"), error=str(exc)[:200])
                        return None
                    if not announced:
                        announced = True
                        await self.relay.publish_thread_status(
                            run.id, spec.get("thread_hint", spec["persona"]), "queued")
                    await asyncio.sleep(queue_poll_seconds)

        threads = await asyncio.gather(*(_spawn_one(s) for s in specs))
        return [l for l in threads if l is not None]

    async def settle_cost(self, thread_id: str) -> float:
        """End-of-thread spend readback (eventually consistent, grace
        window). This — not the SDK's Anthropic-priced calculator — fills cost."""
        session = get_session()
        try:
            thread = session.get(Thread, thread_id)
            if not thread or not thread.gateway_key:
                return 0.0
            spend = await self.gateway.read_spend_reconciled(thread.gateway_key)
            thread.cost_usd = spend
            thread.finished_at = datetime.now(timezone.utc)
            run = session.get(Run, thread.run_id)
            if run:
                run.cost_usd = sum(l.cost_usd for l in session.query(Thread).filter_by(run_id=run.id))
            session.commit()
            return spend
        finally:
            session.close()

    async def release_key(self, thread_id: str) -> None:
        session = get_session()
        try:
            thread = session.get(Thread, thread_id)
            key = thread.gateway_key if thread else None
        finally:
            session.close()
        if key:
            try:
                await self.gateway.delete_key(key)
            except Exception as exc:
                log.warning("key release failed", thread_id=thread_id, error=str(exc)[:120])

    def _mark(self, thread_id: str, status: str) -> None:
        session = get_session()
        try:
            thread = session.get(Thread, thread_id)
            if thread:
                thread.status = status
                session.commit()
        finally:
            session.close()
