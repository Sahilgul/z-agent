"""N3: real-Redis ingest — events published to the run stream are consumed,
persisted, and acked by the real IngestConsumer; a poison payload dead-letters
instead of vanishing silently."""

import asyncio

import pytest


class _CollectRelay:
    def __init__(self):
        self.steps = []
        self.notes = []

    async def publish_step(self, run_id, step):
        self.steps.append((run_id, step))

    async def publish_note(self, run_id, note):
        self.notes.append((run_id, note))


def _make_run(factory, run_id: str) -> None:
    from app.db.models.run import Run
    from app.db.models.user import User
    s = factory()
    u = User(username=f"it-{run_id}")
    s.add(u)
    s.commit()
    s.add(Run(id=run_id, created_by=u.id, source="api", mode="ask",
              title="integration"))
    s.commit()
    s.close()


@pytest.mark.asyncio
async def test_ingest_consumes_and_acks(real_redis, pg_session_factory,
                                        monkeypatch):
    monkeypatch.setattr("app.events.bus.make_redis", lambda **kw: real_redis)
    from collegium_contracts import StepEvent, StepKind

    from app.events.bus import STREAM_PREFIX, IngestConsumer

    _make_run(pg_session_factory, "r-int")
    event = StepEvent(run_id="r-int", thread_id="t-int", seq=0,
                      kind=StepKind.MESSAGE, title="hi", detail={})
    relay = _CollectRelay()
    consumer = IngestConsumer(relay)
    consumer.register_run("r-int")
    await real_redis.xadd(f"{STREAM_PREFIX}r-int",
                          {"payload": event.model_dump_json()})
    await consumer.start()
    try:
        for _ in range(50):
            await asyncio.sleep(0.1)
            if relay.steps:
                break
        assert relay.steps, "event was not relayed within 5s"
        # Acked: the pending list for this group is empty.
        pending = await real_redis.xpending(f"{STREAM_PREFIX}r-int", "ingest")
        count = pending[0] if isinstance(pending, (list, tuple)) else pending.get("pending")
        assert int(count) == 0
    finally:
        await consumer.stop()


@pytest.mark.asyncio
async def test_poison_payload_dead_letters(real_redis, pg_session_factory,
                                           monkeypatch):
    monkeypatch.setattr("app.events.bus.make_redis", lambda **kw: real_redis)
    from app.events.bus import STREAM_PREFIX, IngestConsumer

    _make_run(pg_session_factory, "r-poison")
    relay = _CollectRelay()
    consumer = IngestConsumer(relay)
    consumer.register_run("r-poison")
    await real_redis.xadd(f"{STREAM_PREFIX}r-poison", {"payload": "not json {"})
    await consumer.start()
    try:
        for _ in range(50):
            await asyncio.sleep(0.1)
            depth = await real_redis.xlen(f"{STREAM_PREFIX}r-poison:deadletter")
            if depth:
                break
        assert depth >= 1, "poison payload was neither processed nor dead-lettered"
    finally:
        await consumer.stop()
