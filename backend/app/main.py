"""App factory: lifespan, routers, WS mount. Routers stay thin;
all domain logic lives in services/ and orchestrator/ (no FastAPI imports there).
"""

from __future__ import annotations

import inspect
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.core.config import get_settings
from app.core.logging import configure_logging, get_logger
from app.events.bus import IngestConsumer
from app.events.control import LaneControl
from app.events.relay import Relay
from app.gateway.litellm import GatewayClient
from app.orchestrator.thread_manager import ThreadManager
from app.orchestrator.run_manager import RunManager
from app.sandbox.fetcher import start_fetch_loop
from app.services.approvals import ApprovalService
from app.services.heartbeats import HeartbeatPersister
from app.services.hydration import PrewarmPool

log = get_logger(service="main")


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    settings = get_settings()
    app.state.settings = settings

    relay = Relay()
    ingest = IngestConsumer(relay)
    control = LaneControl()
    gateway = GatewayClient()
    thread_manager = ThreadManager(ingest, relay, gateway)
    approval_service = ApprovalService(relay, control)
    run_manager = RunManager(ingest, relay, thread_manager, control,
                             approvals=approval_service)
    heartbeat_persister = HeartbeatPersister()

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
    await approval_service.start()
    await heartbeat_persister.start()
    start_fetch_loop()
    zombies = await run_manager.reconcile_on_boot()
    if zombies:
        log.warning("reconciled zombie runs", count=zombies)

    yield

    await ingest.stop()
    await approval_service.stop()
    await heartbeat_persister.stop()
    # H-45: shut the fetch scheduler down so it doesn't fire during teardown.
    from app.sandbox.fetcher import stop_fetch_loop
    stop_fetch_loop()
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

    from app.api import approvals, auth, bench, byo_pat, campaigns, hydration, ideas, knowledge, threads, modes, proposals, push, repos, runs, sessions, team, webhooks
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
