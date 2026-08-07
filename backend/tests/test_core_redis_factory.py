"""Redis factory (memory:// local-dev path) + bootstrap admin seed."""

import asyncio
import json

import fakeredis
import redis.asyncio as real_redis
from collegium_contracts import StepEvent, StepKind

from app.core.config import get_settings
from app.core.redis_factory import in_memory, make_redis
from app.core.security import verify_pin
from app.db.models.event import Event
from app.db.models.thread import Thread
from app.db.models.run import Run


def test_memory_scheme_returns_shared_fakeredis(monkeypatch):
    monkeypatch.setenv("COLLEGIUM_REDIS_URL", "memory://0")
    get_settings.cache_clear()
    try:
        a = make_redis()
        b = make_redis()
        assert isinstance(a, fakeredis.aioredis.FakeRedis)
        # same FakeServer — pub/sub and streams are visible across clients
        assert a.connection_pool.connection_kwargs["server"] is \
            b.connection_pool.connection_kwargs["server"]
    finally:
        get_settings.cache_clear()


async def test_memory_clients_share_the_bus(monkeypatch):
    monkeypatch.setenv("COLLEGIUM_REDIS_URL", "memory://0")
    get_settings.cache_clear()
    try:
        pub = make_redis()
        sub = make_redis()
        received = []
        async with sub.pubsub() as ps:
            await ps.subscribe("thread:x:control")
            await ps.get_message(timeout=1.0)  # consume subscribe ack first
            await pub.publish("thread:x:control", "stop")
            msg = await ps.get_message(ignore_subscribe_messages=True, timeout=1.0)
            if msg:
                received.append(msg["data"])
        assert received == ["stop"]
        await pub.aclose()
        await sub.aclose()
    finally:
        get_settings.cache_clear()


def test_real_scheme_returns_redis_client(monkeypatch):
    monkeypatch.setenv("COLLEGIUM_REDIS_URL", "redis://localhost:6379/0")
    get_settings.cache_clear()
    try:
        client = make_redis()
        assert isinstance(client, real_redis.Redis)
        assert not isinstance(client, fakeredis.aioredis.FakeRedis)
    finally:
        get_settings.cache_clear()


async def test_memory_ingest_loop_consumes_without_freezing(session, make_user, monkeypatch):
    """Regression: fakeredis blocking reads froze the dev server's one asyncio
    loop (monitor stuck at 'loading run…'). The in-memory path must poll AND
    still deliver events."""
    monkeypatch.setenv("COLLEGIUM_REDIS_URL", "memory://0")
    get_settings.cache_clear()
    try:
        assert in_memory() is True
        from app.events.bus import STREAM_PREFIX, IngestConsumer
        from tests.conftest import FakeRelay

        u = make_user("m")
        session.add_all([
            Run(id="rm1", created_by=u.id, mode="ask", stage="investigating"),
            Thread(id="lm1", run_id="rm1", persona="researcher", status="running", next_seq=0),
        ])
        session.commit()

        consumer = IngestConsumer(FakeRelay())
        ev = StepEvent(run_id="rm1", thread_id="lm1", seq=0, kind=StepKind.MESSAGE,
                       title="hello", detail={})
        await consumer.redis.xadd(STREAM_PREFIX + "rm1", {"payload": ev.model_dump_json()})
        consumer.register_run("rm1")
        await consumer.start()
        try:
            # another coroutine must progress while the consumer loop runs
            await asyncio.wait_for(asyncio.sleep(0.05), timeout=2)
            for _ in range(40):
                await asyncio.sleep(0.05)
                session.expire_all()
                if session.query(Event).filter_by(run_id="rm1").one_or_none():
                    break
        finally:
            await consumer.stop()
        row = session.query(Event).filter_by(run_id="rm1").one_or_none()
        assert row is not None and row.title == "hello"
    finally:
        get_settings.cache_clear()


async def test_memory_relay_delta_poll_delivers(monkeypatch):
    monkeypatch.setenv("COLLEGIUM_REDIS_URL", "memory://0")
    get_settings.cache_clear()
    try:
        from app.events.relay import Relay

        relay = Relay()
        queue = relay.subscribe("rd1")
        publisher = make_redis()
        try:
            await asyncio.sleep(0.15)  # let the delta loop subscribe first
            await publisher.publish("deltas:rd1", json.dumps(
                {"thread_id": "l", "kind": "typing", "text": "hi"}))
            msg = await asyncio.wait_for(queue.get(), timeout=2)
            assert msg["type"] == "delta"
            assert msg["delta"]["text"] == "hi"
        finally:
            relay.unsubscribe("rd1", queue)
            await relay.close()
            await publisher.aclose()
    finally:
        get_settings.cache_clear()


def test_seed_bootstraps_active_admin_once(session, monkeypatch):
    from app.auth.seed_users import seed
    from app.core.config import get_settings
    from app.db.models.user import User

    # C-14: the bootstrap admin is opt-in (no shipped default). Configure
    # username + pin, then seed — a production deploy with empty defaults
    # never silently seeds an active admin with a known PIN.
    monkeypatch.setenv("COLLEGIUM_BOOTSTRAP_ADMIN_USERNAME", "sahil")
    monkeypatch.setenv("COLLEGIUM_BOOTSTRAP_ADMIN_PIN", "4545")
    get_settings.cache_clear()
    try:
        seed()
        admin = session.query(User).filter_by(username="sahil").one()
        assert admin.role == "admin"
        assert admin.status == "active"
        assert verify_pin("4545", admin.pin_hash)

        seed()  # idempotent — no duplicate, no pin reset
        assert session.query(User).filter_by(username="sahil").count() == 1
    finally:
        get_settings.cache_clear()


def test_seed_skips_bootstrap_admin_when_unset(session):
    """C-14: with empty bootstrap defaults (the production default), seed()
    must NOT create an active admin with a known PIN."""
    from app.auth.seed_users import seed
    from app.db.models.user import User

    seed()
    assert session.query(User).filter_by(username="sahil").count() == 0
