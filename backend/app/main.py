"""App factory: lifespan, routers, WS mount. Routers stay thin;
all domain logic lives in services/ and orchestrator/ (no FastAPI imports there).
"""

from __future__ import annotations

import asyncio
import inspect
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.core.config import get_settings
from app.core.logging import configure_logging, get_logger
from app.events.bus import IngestConsumer
from app.events.control import LaneControl
from app.events.relay import Relay
from app.gateway.litellm import GatewayClient
from app.orchestrator.run_manager import RunManager
from app.orchestrator.thread_manager import ThreadManager
from app.sandbox.fetcher import start_fetch_loop
from app.services.approvals import ApprovalService
from app.services.heartbeats import HeartbeatPersister
from app.services.hydration import PrewarmPool

log = get_logger(service="main")


def _check_migrations() -> None:
    """M3: refuse to serve against a stale schema. Compares the DB's
    alembic_version with the migration head; a mismatch means the deploy
    forgot `alembic upgrade head` and every query is a coin flip.

    Skipped for a brand-new local SQLite file (first-boot dev path — tables
    are created by create_all elsewhere) and for in-memory test engines.
    """
    settings = get_settings()
    url = settings.db_url
    if url.startswith("sqlite"):
        path = url.split("///", 1)[-1]
        if path == ":memory:" or not __import__("pathlib").Path(path).exists():
            return
    import sqlalchemy as sa
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    import app.db.base as db_base

    # backend/app/main.py -> parents[1] is the backend dir holding alembic.ini.
    alembic_cfg = Config(str(__import__("pathlib").Path(__file__).resolve().parents[1] / "alembic.ini"))
    head = ScriptDirectory.from_config(alembic_cfg).get_current_head()
    with db_base.engine.connect() as conn:
        try:
            current = conn.execute(sa.text("SELECT version_num FROM alembic_version")).scalar()
        except sa.exc.SQLAlchemyError:
            # No alembic_version table on a non-empty DB: schema provenance is
            # unknown — treat as stale rather than guessing.
            tables = conn.execute(
                sa.text("SELECT name FROM sqlite_master WHERE type='table'")
            ).fetchall() if url.startswith("sqlite") else []
            if url.startswith("sqlite") and not tables:
                return  # empty file DB, first boot
            raise RuntimeError(
                "Database has no alembic_version table — run "
                "`alembic stamp head` (legacy DB) or `alembic upgrade head`."
            ) from None
    if current != head:
        raise RuntimeError(
            f"Database schema is at revision {current!r} but the code expects "
            f"{head!r}. Run `alembic upgrade head` before starting the backend."
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    settings = get_settings()
    app.state.settings = settings
    _check_migrations()

    relay = Relay()
    ingest = IngestConsumer(relay)
    control = LaneControl()
    gateway = GatewayClient()
    thread_manager = ThreadManager(ingest, relay, gateway)
    # C1: the bridge that turns worker spawn requests into real threads.
    from app.events.spawn_bridge import SpawnBridge
    spawn_bridge = SpawnBridge(thread_manager, control, relay)
    app.state.spawn_bridge = spawn_bridge
    approval_service = ApprovalService(relay, control)
    run_manager = RunManager(ingest, relay, thread_manager, control,
                             approvals=approval_service, spawn_bridge=spawn_bridge)
    # F1: the reaper's terminal stamps run the unified money/key cleanup.
    heartbeat_persister = HeartbeatPersister(thread_manager=thread_manager)

    app.state.relay = relay
    app.state.ingest = ingest
    app.state.control = control
    app.state.gateway = gateway
    app.state.thread_manager = thread_manager
    app.state.run_manager = run_manager
    app.state.approval_service = approval_service
    # M-57: PrewarmPool was instantiated via a __import__ hack and never
    # started or closed. Import cleanly and wire a guarded close in
    # teardown (the stub has no lifecycle, but the real pool will).
    prewarm_pool = PrewarmPool()
    app.state.prewarm_pool = prewarm_pool

    await ingest.start()
    await spawn_bridge.start()
    await approval_service.start()
    await heartbeat_persister.start()
    start_fetch_loop()
    zombies = await run_manager.reconcile_on_boot()
    if zombies:
        log.warning("reconciled zombie runs", count=zombies)

    # External-write recovery sweep: re-drive trigger events whose log row
    # is stuck in 'received' (process crashed mid-dispatch) and drain
    # rate-limit-queued events as capacity returns.
    async def _trigger_sweep() -> None:
        from app.services import triggers
        while True:
            await asyncio.sleep(30)
            try:
                await triggers.recover_stuck(run_manager)
                await triggers.drain_queued(run_manager)
            except Exception:
                log.warning("trigger sweep failed", exc_info=True)

    sweep_task = asyncio.create_task(_trigger_sweep(),
                                     name="trigger-recovery-sweep")

    # Daily maintenance: shred expired session volumes (retention) and run
    # the Sleep-Time Distiller. Both existed but were never scheduled.
    async def _daily_maintenance() -> None:
        from app.sandbox.manager import sandbox_manager
        from app.services import distiller
        while True:
            await asyncio.sleep(24 * 3600)
            try:
                purged = sandbox_manager.purge_expired_sessions()
                if purged:
                    log.info("retention sweep purged session volumes",
                             count=purged)
            except Exception:
                log.warning("retention sweep failed", exc_info=True)
            try:
                result = await distiller.run_nightly()
                if result.get("error"):
                    log.warning("nightly distill failed (runs unmined, will retry)",
                                error=result["error"])
            except Exception:
                log.warning("nightly distill crashed", exc_info=True)

    maintenance_task = asyncio.create_task(_daily_maintenance(),
                                           name="daily-maintenance")

    yield

    # E6: drain in-flight blueprint tasks BEFORE stopping the event services
    # they publish on — a cancelled mid-node run used to strand its thread
    # until the next boot's reconcile.
    await run_manager.shutdown()
    await spawn_bridge.stop()
    await ingest.stop()
    await approval_service.stop()
    await heartbeat_persister.stop()
    # H-45: shut the fetch scheduler down so it doesn't fire during teardown.
    from app.sandbox.fetcher import stop_fetch_loop
    stop_fetch_loop()
    for task in (sweep_task, maintenance_task):
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    await relay.close()
    await control.close()
    # M-57: close the prewarm pool if it owns resources (no-op for the stub).
    close = getattr(prewarm_pool, "close", None)
    if close is not None:
        if inspect.iscoroutinefunction(close):
            await close()
        else:
            close()


def create_app() -> FastAPI:
    app = FastAPI(title="Collegium", version="0.1.0", lifespan=lifespan)

    from app.api import (
        approvals,
        auth,
        bench,
        byo_pat,
        campaigns,
        hydration,
        ideas,
        knowledge,
        modes,
        proposals,
        push,
        repos,
        runs,
        sessions,
        team,
        threads,
        webhooks,
    )
    from app.ws.events import router as ws_router

    for r in (auth.router, team.router, runs.router, threads.router,
              approvals.router, repos.router, modes.router, sessions.router,
              hydration.router, knowledge.router, ideas.router, byo_pat.router,
              proposals.router, push.router, bench.router, campaigns.router,
              webhooks.router):
        app.include_router(r)
    app.include_router(ws_router)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "service": "collegium-backend"}

    return app


app = create_app()
