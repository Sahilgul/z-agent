"""N3/N4: real Postgres+Redis integration tier.

These tests are skipped unless BOTH env vars point at live infra:

    COLLEGIUM_INTEGRATION=1
    INTEGRATION_DATABASE_URL=postgresql+psycopg://user:pass@host/db
    INTEGRATION_REDIS_URL=redis://host:6379/0

Concurrency, redelivery, resume, and reconnect behavior must be executable
tests, not review comments. In CI, provision throwaway postgres/redis
services (or run scripts/ra_evidence.sh for the checkpointer variant).
"""

import os

import pytest
import sqlalchemy as sa

pytestmark = pytest.mark.skipif(
    os.environ.get("COLLEGIUM_INTEGRATION") != "1",
    reason="real-infra integration tier (set COLLEGIUM_INTEGRATION=1)",
)

DB_URL = os.environ.get("INTEGRATION_DATABASE_URL", "")
REDIS_URL = os.environ.get("INTEGRATION_REDIS_URL", "")


@pytest.fixture()
def pg_engine():
    if not DB_URL:
        pytest.skip("INTEGRATION_DATABASE_URL not set")
    engine = sa.create_engine(DB_URL)
    from app.db.base import Base
    Base.metadata.create_all(engine)
    yield engine
    Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture()
def pg_session_factory(pg_engine, monkeypatch):
    factory = sa.orm.sessionmaker(bind=pg_engine, autoflush=False,
                                  expire_on_commit=False)
    # get_session() reads the module-global SessionLocal at CALL time, so
    # one patch redirects every service.
    import app.db.base as base
    monkeypatch.setattr(base, "SessionLocal", factory)
    return factory


@pytest.fixture()
async def real_redis():
    if not REDIS_URL:
        pytest.skip("INTEGRATION_REDIS_URL not set")
    import redis.asyncio as aioredis
    r = aioredis.from_url(REDIS_URL, decode_responses=True)
    yield r
    await r.flushdb()
    await r.aclose()
