"""App factory (plan §8): lifespan, routers, WS mount. Routers stay thin;
all domain logic lives in services/ and orchestrator/ (no FastAPI imports there).
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.core.config import get_settings
from app.core.logging import configure_logging, get_logger
from app.events.bus import IngestConsumer
from app.events.control import LaneControl
from app.events.relay import Relay
from app.gateway.litellm import GatewayClient
from app.orchestrator.lane_manager import LaneManager
from app.orchestrator.run_manager import RunManager
from app.sandbox.fetcher import start_fetch_loop
from app.services.approvals import ApprovalService
from app.services.heartbeats import HeartbeatPersister

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
    lane_manager = LaneManager(ingest, relay, gateway)
    run_manager = RunManager(ingest, relay, lane_manager, control)
    approval_service = ApprovalService(relay, control)
    heartbeat_persister = HeartbeatPersister()

    app.state.relay = relay
    app.state.ingest = ingest
    app.state.control = control
    app.state.gateway = gateway
    app.state.lane_manager = lane_manager
    app.state.run_manager = run_manager
    app.state.approval_service = approval_service
    app.state.prewarm_pool = __import__("app.services.hydration", fromlist=["PrewarmPool"]).PrewarmPool()

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
    await relay.close()
    await control.close()


def create_app() -> FastAPI:
    app = FastAPI(title="Zagent", version="0.1.0", lifespan=lifespan)

    from app.api import approvals, auth, bench, byo_pat, campaigns, hydration, ideas, knowledge, lanes, modes, proposals, push, repos, runs, sessions, team, webhooks
    from app.ws.events import router as ws_router

    for r in (auth.router, team.router, runs.router, lanes.router,
              approvals.router, repos.router, modes.router, sessions.router,
              hydration.router, knowledge.router, ideas.router, byo_pat.router,
              proposals.router, push.router, bench.router, campaigns.router,
              webhooks.router):
        app.include_router(r)
    app.include_router(ws_router)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "service": "zagent-backend"}

    return app


app = create_app()
