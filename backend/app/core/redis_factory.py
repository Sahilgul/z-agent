"""One Redis client factory for the whole backend (single-writer bus).

redis_url scheme decides the client:
- "redis://…"  -> real server (all deployments set COLLEGIUM_REDIS_URL explicitly)
- "memory://…" -> in-process fakeredis over a SHARED FakeServer, so the relay,
  ingest consumer, thread control, and approval service all see the same bus.
  This is the no-Docker local-dev path; nothing else about the bus changes.
"""

from __future__ import annotations

import redis.asyncio as redis

from app.core.config import get_settings

_fake_server = None


def in_memory() -> bool:
    """Consumers use this to switch blocking reads (real Redis, efficient) to
    short polls — fakeredis blocking calls wait on a thread condition, which
    deadlocks the single asyncio loop of a dev server."""
    return get_settings().redis_url.startswith("memory://")


def make_redis(decode_responses: bool = True):
    settings = get_settings()
    url = settings.redis_url
    if url.startswith("memory://"):
        global _fake_server
        import fakeredis.aioredis

        if _fake_server is None:
            import fakeredis

            _fake_server = fakeredis.FakeServer()
        return fakeredis.aioredis.FakeRedis(server=_fake_server,
                                            decode_responses=decode_responses)
    return redis.from_url(url, decode_responses=decode_responses)
