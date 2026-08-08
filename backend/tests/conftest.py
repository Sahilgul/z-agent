"""Shared test infra: in-memory SQLite, fake redis, fake services, auth helpers.

Mock-only (mock-unit-testing-standard): no Docker daemon, no real Redis, no network.
Env vars are set BEFORE any `app.*` import so the import-time engine in
app.db.base uses an in-memory URL and never touches the filesystem.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import uuid
from pathlib import Path
from types import SimpleNamespace

# ---- env MUST be set before importing app.* (app.db.base builds engine at import)
os.environ.setdefault("COLLEGIUM_DB_URL", "sqlite:///:memory:")
os.environ.setdefault("COLLEGIUM_REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("COLLEGIUM_GATEWAY_URL", "http://gateway.test")
os.environ.setdefault("COLLEGIUM_LITELLM_MASTER_KEY", "test-master-key")
os.environ.setdefault("COLLEGIUM_WORKER_REDIS_URL", "redis://redis:6379/0")
os.environ.setdefault("COLLEGIUM_WORKER_GATEWAY_URL", "http://gateway:4000")
os.environ.setdefault("COLLEGIUM_WORKER_IMAGE", "collegium-worker:test")
os.environ.setdefault("COLLEGIUM_JWT_SECRET", "test-jwt-secret")
os.environ.setdefault("COLLEGIUM_ADMIN_USERNAMES", "sahil")
os.environ.setdefault("COLLEGIUM_ADO_ORG", "testorg")
os.environ.setdefault("COLLEGIUM_ADO_PROJECT", "testproj")
os.environ.setdefault("COLLEGIUM_FETCH_PAT", "fetch-pat")
os.environ.setdefault("COLLEGIUM_FLEET_PAT", "fleet-pat")
os.environ.setdefault("COLLEGIUM_BYO_PAT_ENCRYPTION_KEY", "test-byo-pat-key")  # H-44
os.environ.setdefault("COLLEGIUM_GLOBAL_LANE_CAP", "12")
os.environ.setdefault("COLLEGIUM_DEFAULT_LANE_BUDGET_USD", "5.0")

_TMP_ROOT = Path(tempfile.mkdtemp(prefix="collegium-test-"))
os.environ.setdefault("COLLEGIUM_GOLDEN_DIR", str(_TMP_ROOT / "golden"))
os.environ.setdefault("COLLEGIUM_SESSIONS_DIR", str(_TMP_ROOT / "sessions"))
os.environ.setdefault("COLLEGIUM_WORKSPACES_DIR", str(_TMP_ROOT / "workspaces"))
# fleet-config: real read-only local fixtures
os.environ.setdefault("COLLEGIUM_FLEET_CONFIG_DIR", str(Path(__file__).resolve().parents[2] / "fleet-config"))

from datetime import UTC

import pytest
from sqlalchemy import DateTime, create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool
from sqlalchemy.types import TypeDecorator

import app.db.base as db_base
from app.core.config import get_settings
from app.db.models import (
    Approval,
    Delivery,
    EvalCase,
    EvalRun,
    Event,
    IdeaComment,
    IdeaThread,
    KnowledgeItem,
    Mode,
    Notification,
    Plan,
    PlanStep,
    Playbook,
    PrLink,
    Proposal,
    Repo,
    RepoProfile,
    Run,
    SetupCode,
    Thread,
    TrajectorySummary,
    Trigger,
    TriggerEventLog,
    User,
)


class TZDateTime(TypeDecorator):
    """Coerces naive datetimes read back from SQLite to UTC-aware so that
    comparisons against `datetime.now(timezone.utc)` (used throughout the
    production code) don't raise. Storage impl stays DateTime (naive)."""
    impl = DateTime
    cache_ok = True

    def process_result_value(self, value, dialect):
        if value is not None and getattr(value, "tzinfo", None) is None:
            return value.replace(tzinfo=UTC)
        return value


def _apply_tz_datetime() -> None:
    for table in db_base.Base.metadata.tables.values():
        for col in table.columns:
            if isinstance(col.type, DateTime) and not isinstance(col.type, TZDateTime):
                col.type = TZDateTime()


# ---------------------------------------------------------------- DB engine
# Speed: ONE shared in-memory schema for the whole suite (StaticPool keeps the
# single connection alive). Per-test isolation = DELETE FROM every table in
# reverse FK order — microseconds in memory, and immune to the savepoint-
# rollback leakage observed on this stack. Per-test setup drops from ~0.8s
# (file DB + create_all/drop_all) to ~2ms.
@pytest.fixture(scope="session")
def _schema_engine():
    eng = create_engine(
        "sqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    _apply_tz_datetime()
    db_base.Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def engine(_schema_engine):
    with _schema_engine.begin() as conn:
        for table in reversed(db_base.Base.metadata.sorted_tables):
            conn.execute(table.delete())
    old_engine = db_base.engine
    old_local = db_base.SessionLocal
    db_base.engine = _schema_engine
    db_base.SessionLocal = db_base.sessionmaker(
        bind=_schema_engine, class_=Session, expire_on_commit=False, future=True
    )
    try:
        yield _schema_engine
    finally:
        db_base.engine = old_engine
        db_base.SessionLocal = old_local


@pytest.fixture
def session(engine):
    sess = db_base.SessionLocal()
    try:
        yield sess
    finally:
        sess.close()


# ---------------------------------------------------------------- speed: bcrypt
# Security hardness is out of scope for the suite: bcrypt is stubbed to an
# INSTANT deterministic fake with the same call contract (hashpw/checkpw/
# gensalt). PIN policy logic in app.core.security still runs for real.
@pytest.fixture(autouse=True)
def _fast_bcrypt(monkeypatch):
    import hashlib

    import bcrypt

    def fake_hashpw(pw: bytes, salt: bytes) -> bytes:
        return b"t$" + hashlib.sha256(pw).hexdigest().encode()

    def fake_checkpw(pw: bytes, hashed: bytes) -> bool:
        return hashed == fake_hashpw(pw, b"")

    monkeypatch.setattr(bcrypt, "gensalt", lambda *a, **k: b"t$test")
    monkeypatch.setattr(bcrypt, "hashpw", fake_hashpw)
    monkeypatch.setattr(bcrypt, "checkpw", fake_checkpw)


# ---------------------------------------------------------------- speed: polls
# Ingest/approval consumer loops idle-poll at 0.5s in production; in tests the
# fakes answer instantly, so the poll only wastes wall-clock.
@pytest.fixture(autouse=True)
def _fast_idle_polls(monkeypatch):
    import app.events.bus as bus_mod
    import app.services.approvals as approvals_mod

    monkeypatch.setattr(bus_mod, "IDLE_POLL_SECONDS", 0.01)
    monkeypatch.setattr(approvals_mod, "IDLE_POLL_SECONDS", 0.01)


# Thread spawn injects the flywheel knowledge block via the gateway-backed rerank;
# in unit tests the block is empty by default (knowledge-service tests exercise
# rerank/fallback directly with injected rankers — no real sockets anywhere).
@pytest.fixture(autouse=True)
def _no_knowledge_block(monkeypatch):
    import app.services.knowledge as knowledge_mod

    async def _empty(run_id, task_text, user_id, repo, ranker=None):
        return ""

    monkeypatch.setattr(knowledge_mod, "prompt_block_for_run", _empty)


# The JSONL transcript writer appends to a real directory; keep every test's
# writes inside tmp so the suite never litters the repo checkout. Set via env,
# not on the cached instance: tests that call get_settings.cache_clear() would
# otherwise rebuild Settings with the default ./transcripts path.
@pytest.fixture(autouse=True)
def _isolated_transcripts(monkeypatch, tmp_path):
    monkeypatch.setenv("COLLEGIUM_TRANSCRIPTS_DIR", str(tmp_path / "transcripts"))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# ---------------------------------------------------------------- user factory
@pytest.fixture
def make_user(session):
    def _make(username: str = "alice", *, role: str = "member", status: str = "active",
              pin: str = "1234", display_name: str = "Alice", token_version: int = 0,
              ado_descriptor: str | None = None) -> User:
        from app.core.security import hash_pin
        u = User(
            username=username, display_name=display_name, role=role, status=status,
            pin_hash=hash_pin(pin) if pin else None, token_version=token_version,
            ado_descriptor=ado_descriptor,
        )
        session.add(u)
        session.commit()
        return u
    return _make


# ---------------------------------------------------------------- fake redis
class FakeRedis:
    """Hand-rolled async in-memory fake matching the methods production calls:
    xadd, xack, xreadgroup, xgroup_create, rpush, publish, pubsub, aclose."""

    def __init__(self) -> None:
        self.streams: dict[str, list[tuple[str, dict]]] = {}
        self.lists: dict[str, list[str]] = {}
        self.pubsub_channels: dict[str, list[str]] = {}
        self._msg_id = 0
        self.groups: dict[str, set[str]] = {}  # stream -> group names
        self.pel: dict[str, dict[str, dict]] = {}  # stream -> {msg_id -> fields}
        self.delivered: dict[str, set[str]] = {}  # stream -> msg_ids ever delivered to group
        self.acked: dict[str, set[str]] = {}  # stream -> acked msg_ids (H-52)
        self.published: list[tuple[str, str]] = []
        self.kv: dict[str, str] = {}  # plain GET/SET (ack keys, heartbeat keys)

    def _next_id(self) -> str:
        self._msg_id += 1
        return f"{self._msg_id}-0"

    async def xadd(self, stream: str, fields: dict) -> str:
        await asyncio.sleep(0)
        mid = self._next_id()
        self.streams.setdefault(stream, []).append((mid, dict(fields)))
        return mid

    async def xlen(self, stream: str) -> int:
        await asyncio.sleep(0)
        return len(self.streams.get(stream, []))

    async def xautoclaim(self, stream: str, group: str, consumer: str,
                         min_idle_time: int = 0, start_id: str = "0-0",
                         count: int = 100):
        """D4: return pending (delivered, unacked) entries like XAUTOCLAIM."""
        await asyncio.sleep(0)
        pel = self.pel.get(stream, {})
        entries = [(mid, dict(fields)) for mid, fields in list(pel.items())][:count]
        return ["0-0", entries, []]

    async def xack(self, stream: str, group: str, *msg_ids: str) -> int:
        await asyncio.sleep(0)
        pel = self.pel.get(stream, {})
        for mid in msg_ids:
            pel.pop(mid, None)
        # H-52: record acks so the durability contract (a processed message
        # is acked, not just persisted) is observable in tests.
        self.acked.setdefault(stream, set()).update(msg_ids)
        return len(msg_ids)

    async def xgroup_create(self, stream: str, group: str, id: str = "0", mkstream: bool = False) -> None:
        await asyncio.sleep(0)
        self.streams.setdefault(stream, [])
        # H-54: only raise BUSYGROUP when the group ALREADY exists — the old
        # fake raised unconditionally, so the group-create SUCCESS path was
        # dead under test and _ensure_group's first-call branch was untested.
        if group in self.groups.get(stream, set()):
            raise __import__("redis").ResponseError("BUSYGROUP Consumer Group name already exists")
        self.groups.setdefault(stream, set()).add(group)
        self.pel.setdefault(stream, {})

    async def xreadgroup(self, group: str, consumer: str, streams: dict, count: int = 100, block: int = 0):
        await asyncio.sleep(0)
        out: list[tuple[str, list[tuple[str, dict]]]] = []
        for stream, start in streams.items():
            entries = self.streams.get(stream, [])
            pel = self.pel.setdefault(stream, {})
            delivered = self.delivered.setdefault(stream, set())
            picked: list[tuple[str, dict]] = []
            for mid, fields in entries:
                if start == ">":
                    if mid not in delivered:
                        picked.append((mid, dict(fields)))
                        delivered.add(mid)
                        pel[mid] = dict(fields)
                else:
                    if mid in pel:
                        picked.append((mid, dict(fields)))
                if len(picked) >= count:
                    break
            if picked:
                out.append((stream, picked))
        return out

    async def get(self, key: str):
        await asyncio.sleep(0)
        return self.kv.get(key)

    async def set(self, key: str, value: str, ex: int | None = None,
                  nx: bool = False):
        await asyncio.sleep(0)
        if nx and key in self.kv:
            return None
        self.kv[key] = value
        return True

    async def expire(self, key: str, seconds: int) -> bool:
        await asyncio.sleep(0)
        return True

    async def rpush(self, key: str, *values: str) -> int:
        await asyncio.sleep(0)
        self.lists.setdefault(key, []).extend(values)
        return len(self.lists[key])

    async def publish(self, channel: str, message: str) -> int:
        await asyncio.sleep(0)
        self.published.append((channel, message))
        self.pubsub_channels.setdefault(channel, []).append(message)
        return 1

    def pubsub(self):
        return FakePubSub(self)

    async def aclose(self) -> None:
        pass


class FakePubSub:
    def __init__(self, redis: FakeRedis) -> None:
        self.redis = redis
        self._channels: list[str] = []

    async def subscribe(self, *channels: str) -> None:
        await asyncio.sleep(0)
        self._channels.extend(channels)

    async def unsubscribe(self, *channels: str) -> None:
        await asyncio.sleep(0)
        for c in channels:
            if c in self._channels:
                self._channels.remove(c)

    async def aclose(self) -> None:
        await asyncio.sleep(0)

    async def get_message(self, ignore_subscribe_messages: bool = True,
                          timeout: float = 0):
        # M-59: the relay's in-memory delta loop polls get_message; the old
        # fake had NO get_message, so the delta loop path was untestable via
        # the conftest fake (relay tests had to monkeypatch a local stub).
        # Yield one subscribed-channel message per call (or None when empty).
        await asyncio.sleep(0)
        for ch in self._channels:
            messages = self.redis.pubsub_channels.get(ch, [])
            if messages:
                msg = messages.pop(0)
                return {"type": "message", "channel": ch, "data": msg}
        return None

    async def listen(self):
        # M-59: listen() used to `return` immediately (yield nothing), so the
        # relay's `async for raw in pubsub.listen()` delta path was entirely
        # untested via this fake. Yield subscribed-channel messages so the
        # pubsub path is live.
        while True:
            for ch in self._channels:
                messages = self.redis.pubsub_channels.get(ch, [])
                while messages:
                    msg = messages.pop(0)
                    yield {"type": "message", "channel": ch, "data": msg}
            await asyncio.sleep(0.05)


@pytest.fixture
def fake_redis():
    return FakeRedis()


# ---------------------------------------------------------------- fake services
class FakeRelay:
    def __init__(self) -> None:
        self.subscribers: dict[str, set] = {}
        self.published: list[tuple[str, dict]] = []
        self._delta_started: set[str] = set()

    def subscribe(self, run_id: str, user_id=None):
        import asyncio
        q: asyncio.Queue = asyncio.Queue(maxsize=2000)
        self.subscribers.setdefault(run_id, set()).add(q)
        return q

    def unsubscribe(self, run_id: str, queue) -> None:
        self.subscribers.get(run_id, set()).discard(queue)

    async def publish_step(self, run_id, event):
        self.published.append((run_id, {"type": "step", "event": event}))

    async def publish_thread_status(self, run_id, thread_id, status):
        self.published.append((run_id, {"type": "thread_status", "thread_id": thread_id, "status": status}))

    async def publish_run_stage(self, run_id, stage, available_actions):
        self.published.append((run_id, {"type": "run_stage", "stage": stage, "available_actions": available_actions}))

    async def _fanout(self, run_id, message):
        self.published.append((run_id, message))

    async def publish_approval_resolved(self, run_id, approval_id, decision):
        self.published.append((run_id, {
            "type": "approval_resolved", "approval_id": approval_id, "decision": decision,
        }))

    async def publish_global(self, message, user_id=None):
        self.published.append(("__global__", message))

    async def close(self):
        pass


class FakeControl:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def interrupt(self, thread_id, *, wait_ack=False, ack_timeout_s=10.0):
        self.calls.append((f"thread:{thread_id}:control",
                           {"type": "interrupt", "wait_ack": wait_ack}))
        return False  # no worker in unit tests — callers fall back to exit checks

    async def nudge(self, thread_id, text):
        self.calls.append((f"thread:{thread_id}:control", {"type": "nudge", "text": text}))

    async def set_mode(self, thread_id, permission_mode):
        self.calls.append((f"thread:{thread_id}:control", {"type": "mode", "mode": permission_mode}))

    async def kill(self, thread_id, *, wait_ack=False, ack_timeout_s=10.0):
        self.calls.append((f"thread:{thread_id}:control",
                           {"type": "kill", "wait_ack": wait_ack}))
        return False

    async def resolve_approval(self, approval_id, decision, reason="",
                               edited_args=None):
        self.calls.append((f"approval:{approval_id}:decision", {"decision": decision, "reason": reason}))

    async def close(self):
        pass


class FakeThreadManager:
    def __init__(self) -> None:
        self.spawned: list[dict] = []
        self.settled: list[str] = []
        self.released: list[str] = []
        self._spawn_count = 0

    async def spawn(self, run, persona, prompt, persona_prompt, writable_repo, context_repos,
                   resume_session=False, resume_from_thread_id=None,
                   model=None, reasoning=None):
        from app.db.models.thread import Thread
        self._spawn_count += 1
        thread = Thread(id=str(uuid.uuid4()), run_id=run.id, persona=persona,
                    repo_scope=writable_repo.name if writable_repo else None,
                    status="running")
        self.spawned.append({
            "run_id": run.id, "persona": persona, "prompt": prompt,
            "resume_from_thread_id": resume_from_thread_id,
        })
        return thread

    async def settle_cost(self, thread_id):
        self.settled.append(thread_id)
        return 0.0

    async def release_key(self, thread_id):
        self.released.append(thread_id)


class FakeRunManager:
    def __init__(self, thread_manager=None, relay=None, control=None):
        self.thread_manager = thread_manager or FakeThreadManager()
        self.relay = relay or FakeRelay()
        self.control = control or FakeControl()
        self.created: list[dict] = []
        self.stopped: list[str] = []
        self.abandoned: list[str] = []
        self.nudged: list[tuple] = []
        self.stopped_threads: list[tuple] = []
        self.pinned: list[tuple] = []
        self.replaced: list[tuple] = []
        self.continued: list[str] = []
        self.resumed: list[str] = []
        self.resumed_forwarded: list[dict] = []
        self.replanned: list[tuple] = []
        self.prs_opened: list[str] = []
        self.prs_merged: list[tuple] = []
        self.started_plans: list[str] = []
        self.switched_modes: list[tuple] = []
        self.blueprinted: list[tuple] = []

    async def _run_blueprint(self, run_id, blueprint_mode, extra_artifacts=None):
        # Revive/mode-switch path: chains a fresh lane on the prior session
        # volume. Recorded so tests can assert the chain (and its artifacts).
        self.blueprinted.append((run_id, blueprint_mode, extra_artifacts))

    async def create_run(self, source, initiated_by, mode_name, task, repo=None,
                         work_item_id=None, autonomy=None, fanout=None, delivery_id=None,
                         models=None, reasoning=None, images=None, swarm_model=None,
                         idempotency_key=None):
        # Selection validation is the REAL thing (the validators are self-free)
        # so API tests see production's 422s, not a fake that accepts anything.
        from app.orchestrator.run_manager import RunManager
        models = RunManager._validate_models(self, models, mode_name)
        reasoning = RunManager._validate_reasoning(self, reasoning, models)
        RunManager._validate_swarm_model(self, swarm_model)
        if images:
            from app.services import vision
            vision.validate_images(images)  # real 422s for bad attachments
        from collegium_contracts import RunStage

        from app.db.base import get_session
        from app.services.runs import transition
        run = Run(id=str(uuid.uuid4()), created_by=initiated_by, source=source,
                  mode=mode_name, autonomy=autonomy or "supervised", title=task[:256],
                  repo=repo, work_item_id=work_item_id, delivery_id=delivery_id)
        transition(run, RunStage.QUEUED)
        # M-60: persist the run so a POST→GET round-trip sees it. The fake
        # used to only append to self.created, leaving the DB empty, so a
        # follow-up GET returned no runs (no round-trip coverage).
        session = get_session()
        try:
            session.add(run)
            session.commit()
            session.refresh(run)
        finally:
            session.close()
        self.created.append({"id": run.id, "task": task, "repo": repo, "fanout": fanout,
                             "delivery_id": delivery_id, "models": models,
                             "reasoning": reasoning})
        return run

    async def stop_run(self, run_id):
        self.stopped.append(run_id)

    async def abandon_run(self, run_id):
        self.abandoned.append(run_id)

    async def nudge_thread(self, run_id, thread_id, text):
        self.nudged.append((run_id, thread_id, text))

    async def continue_to_development(self, run_id):
        self.continued.append(run_id)

    async def resume_run(self, run_id, initiated_by):
        # H-22: resume continues the SAME run row (no fresh run created).
        # G-09: record the FORWARDED mode/title/repo/autonomy so a test can
        # verify resume preserves the original run's identity (the real
        # resume_run re-executes the existing row and reads its mode/title/
        # repo; the old fake hardcoded mode="ask", title="resumed" and hid
        # whether the forwarding actually happened).
        from collegium_contracts import RunStage

        from app.db.base import get_session
        from app.db.models.run import Run as _Run
        from app.services.runs import transition
        forwarded = {}
        session = get_session()
        try:
            orig = session.get(_Run, run_id)
            if orig is not None:
                forwarded = {"mode": orig.mode, "title": orig.title,
                             "repo": orig.repo, "autonomy": orig.autonomy}
        finally:
            session.close()
        run = Run(id=run_id, created_by=initiated_by,
                  mode=forwarded.get("mode", "ask"),
                  autonomy=forwarded.get("autonomy", "supervised"),
                  title=forwarded.get("title", "resumed"),
                  repo=forwarded.get("repo"))
        transition(run, RunStage.QUEUED)
        self.resumed.append(run_id)
        self.resumed_forwarded.append({"run_id": run_id, "forwarded": forwarded})
        return run

    async def replan(self, run_id, notes=""):
        self.replanned.append((run_id, notes))

    async def create_pr(self, run_id):
        self.prs_opened.append(run_id)
        from app.db.models.delivery import PrLink
        return PrLink(run_id=run_id, repo="ServerApp", branch="agent/x",
                       ado_pr_id=99, status="open")

    async def merge_pr(self, run_id, user_id):
        self.prs_merged.append((run_id, user_id))
        return f"https://ado/merge/{run_id}"

    async def start_plan(self, run_id):
        self.started_plans.append(run_id)

    async def stop_thread(self, run_id, thread_id):
        self.stopped_threads.append((run_id, thread_id))

    async def pin_finding(self, run_id, thread_id, note=""):
        self.pinned.append((run_id, thread_id, note))

    async def kill_replace_thread(self, run_id, thread_id, extra_context_repo_names=None):
        from app.db.models.thread import Thread
        self.replaced.append((run_id, thread_id))
        return Thread(id=f"replacement-{thread_id}", run_id=run_id, persona="explorer",
                    status="running")

    async def remount_thread(self, run_id, thread_id, extra_repo_names):
        # Turn-X @mention expansion: delegate to kill_replace_thread so the
        # fake records the same replaced tuple; return a fresh replacement.
        from app.db.models.thread import Thread
        self.replaced.append((run_id, thread_id))
        return Thread(id=f"replacement-{thread_id}", run_id=run_id, persona="explorer",
                    status="running")

    async def _wait_for_heartbeat(self, thread_id, timeout_s=20.0, poll_s=0.5):
        # The fake worker is "ready" immediately — no real Redis to poll.
        return True

    async def switch_mode(self, run_id, mode_name):
        self.switched_modes.append((run_id, mode_name))

    async def reconcile_on_boot(self):
        return 0


class FakeIngest:
    def __init__(self, relay=None):
        self.relay = relay
        self.registered: list[str] = []
        self.unregistered: list[str] = []

    def register_run(self, run_id):
        self.registered.append(run_id)

    def unregister_run(self, run_id):
        self.unregistered.append(run_id)

    async def start(self):
        pass

    async def stop(self):
        pass


class FakeApprovalService:
    def __init__(self):
        self.decisions: list[tuple] = []

    async def decide(self, approval_id, decision, decided_by, reason="",
                     edited_args=None):
        self.decisions.append((approval_id, decision, decided_by, reason))
        return SimpleNamespace(decision=decision)

    async def start(self):
        pass

    async def stop(self):
        pass


class FakeAdoClient:
    """Stand-in for AdoClient so hydration routes never hit the network."""
    def __init__(self, tickets=None, work_items=None):
        self._tickets = tickets or []
        self._work_items = work_items or {}

    async def my_active_tickets(self, descriptor):
        return self._tickets

    async def get_work_item(self, work_item_id):
        return self._work_items.get(work_item_id, {"id": work_item_id, "fields": {"System.Title": f"item {work_item_id}"}})


class FakeGateway:
    def __init__(self):
        self.minted: list[dict] = []
        self.deleted: list[str] = []
        self.spend: float = 0.0

    async def mint_key(self, alias, max_budget_usd, models=None):
        from app.gateway.litellm import VirtualKey
        self.minted.append({"alias": alias, "max_budget": max_budget_usd})
        return VirtualKey(key=f"sk-{alias}", alias=alias, max_budget=max_budget_usd)

    async def delete_key(self, key):
        self.deleted.append(key)

    async def read_spend_reconciled(self, key, grace_seconds=5.0, polls=3):
        return self.spend

    async def key_spend(self, key):
        return self.spend


# ---------------------------------------------------------------- fastapi app
@pytest.fixture
def app_client(monkeypatch, engine):
    """TestClient with the lifespan fully neutralized: we never enter the
    `with client` block, and instead wire app.state by hand with fakes so
    no Redis/Docker/scheduler starts. Depends on `engine` so requests run
    inside the test's rolled-back transaction."""
    from fastapi.testclient import TestClient

    import app.main as main_mod

    fake_relay = FakeRelay()
    fake_control = FakeControl()
    fake_gateway = FakeGateway()
    fake_thread_manager = FakeThreadManager()
    fake_ingest = FakeIngest(relay=fake_relay)
    fake_run_manager = FakeRunManager(thread_manager=fake_thread_manager, relay=fake_relay, control=fake_control)
    fake_approval = FakeApprovalService()
    from app.services.hydration import PrewarmPool
    fake_prewarm_pool = PrewarmPool()
    fake_ado_client = FakeAdoClient()

    # Prevent the lifespan from starting real services if it ever runs.
    monkeypatch.setattr(main_mod, "Relay", lambda *a, **k: fake_relay)
    monkeypatch.setattr(main_mod, "IngestConsumer", lambda *a, **k: fake_ingest)
    monkeypatch.setattr(main_mod, "LaneControl", lambda *a, **k: fake_control)
    monkeypatch.setattr(main_mod, "GatewayClient", lambda *a, **k: fake_gateway)
    monkeypatch.setattr(main_mod, "ThreadManager", lambda *a, **k: fake_thread_manager)
    monkeypatch.setattr(main_mod, "RunManager", lambda *a, **k: fake_run_manager)
    monkeypatch.setattr(main_mod, "ApprovalService", lambda *a, **k: fake_approval)
    monkeypatch.setattr(main_mod, "start_fetch_loop", lambda: None)

    app = main_mod.create_app()
    app.state.settings = main_mod.get_settings()
    app.state.relay = fake_relay
    app.state.ingest = fake_ingest
    app.state.control = fake_control
    app.state.gateway = fake_gateway
    app.state.thread_manager = fake_thread_manager
    app.state.run_manager = fake_run_manager
    app.state.approval_service = fake_approval
    app.state.prewarm_pool = fake_prewarm_pool
    app.state.ado_client = fake_ado_client

    client = TestClient(app, raise_server_exceptions=False)
    yield client, app, {
        "relay": fake_relay, "control": fake_control, "gateway": fake_gateway,
        "thread_manager": fake_thread_manager, "ingest": fake_ingest,
        "run_manager": fake_run_manager, "approval_service": fake_approval,
    }


@pytest.fixture
def auth_user(session, make_user):
    return make_user("alice", role="member", status="active", pin="1234")


@pytest.fixture
def admin_user(session, make_user):
    return make_user("sahil", role="admin", status="active", pin="1234")


def make_token(user: User) -> str:
    from app.core.security import issue_token
    return issue_token(user)


@pytest.fixture
def auth_client(app_client, auth_user):
    client, app, services = app_client
    token = make_token(auth_user)
    client.cookies.set("collegium_token", token)
    return client, app, services, auth_user


@pytest.fixture
def admin_client(app_client, admin_user):
    client, app, services = app_client
    token = make_token(admin_user)
    client.cookies.set("collegium_token", token)
    return client, app, services, admin_user


# silence unused-import warnings for re-exports used by test modules
__all__ = [
    "Approval",
    "Delivery",
    "EvalCase",
    "EvalRun",
    "Event",
    "FakeAdoClient",
    "FakeApprovalService",
    "FakeAsyncClient",
    "FakeControl",
    "FakeGateway",
    "FakeIngest",
    "FakeRedis",
    "FakeRelay",
    "FakeResponse",
    "FakeRunManager",
    "FakeThreadManager",
    "IdeaComment",
    "IdeaThread",
    "KnowledgeItem",
    "Mode",
    "Notification",
    "Plan",
    "PlanStep",
    "Playbook",
    "PrLink",
    "Proposal",
    "Repo",
    "RepoProfile",
    "Run",
    "SetupCode",
    "Thread",
    "TrajectorySummary",
    "Trigger",
    "TriggerEventLog",
    "User",
    "install_fake_httpx",
    "make_token",
]


# --------------------------------------------------------------- fake httpx
import httpx as _httpx


class FakeResponse:
    def __init__(self, json_data=None, status_code=200, text=""):
        self._json = json_data if json_data is not None else {}
        self.status_code = status_code
        self.text = text

    def raise_for_status(self):
        if self.status_code >= 400:
            raise _httpx.HTTPStatusError("err", request=None, response=self)

    def json(self):
        return self._json


class FakeAsyncClient:
    """Async context manager that returns canned responses by URL substring.
    `routes` maps a URL substring -> FakeResponse (first match wins)."""
    def __init__(self, routes=None, **kwargs):
        self._routes = routes or {}
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def _match(self, method, url):
        self.calls.append((method, url))
        for sub, resp in self._routes.items():
            if sub in url:
                return resp
        return FakeResponse()

    async def get(self, url, **kw):
        return self._match("GET", url)

    async def post(self, url, **kw):
        return self._match("POST", url)

    async def patch(self, url, **kw):
        return self._match("PATCH", url)


def install_fake_httpx(monkeypatch, module, routes):
    """Patch `module.httpx.AsyncClient` to return a FakeAsyncClient with routes."""
    fake = FakeAsyncClient(routes=routes)
    monkeypatch.setattr(module.httpx, "AsyncClient", lambda *a, **k: fake)
    return fake
