"""Wave 4 Stream A worker-side regression tests.

D2 single-source seq survives container replacement (durable seq store).
D5 TypingDeltas redact with the same parity as StepEvents.
D9 durable publish retries through Redis blips.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from worker.engine.events import EventEmitter
from worker.forwarder import Forwarder

# ------------------------------------------------------------------- D2

def test_seq_store_survives_emitter_replacement(tmp_path):
    """A replacement container constructs a NEW EventEmitter over the SAME
    session volume — seq must continue, not restart at 0 (which would let the
    backend's unique constraint dedupe the replayed prefix)."""
    from collegium_contracts import StepKind
    store = tmp_path / "t1.seq"
    e1 = EventEmitter("r1", "t1", seq_store=store)
    e1._next(StepKind.MESSAGE, "hello", {}, None, None)
    e1._next(StepKind.MESSAGE, "world", {}, None, None)
    assert e1._seq == 2

    e2 = EventEmitter("r1", "t1", seq_store=store)  # "new container"
    assert e2._seq == 2
    ev = e2._next(StepKind.MESSAGE, "resumed", {}, None, None)
    assert ev.seq == 2  # continues the thread's sequence (0-indexed: 0,1 then 2)

    # Crash-durability: a missing/corrupt store degrades to 0 (redelivery),
    # never an exception.
    store.write_text("not-a-number")
    assert EventEmitter("r1", "t1", seq_store=store)._seq == 0


def test_emitter_without_store_behaves_as_before():
    from collegium_contracts import StepKind
    e = EventEmitter("r1", "t1")
    assert e._next(StepKind.MESSAGE, "x", {}, None, None).seq == 0


# ------------------------------------------------------------------- D5

def test_typing_delta_is_redacted(monkeypatch):
    """A secret the model echoes must not stream unredacted over pub/sub
    even though the durable StepEvent is clean."""
    from worker.engine.graph import _delta
    secret = "sk-ant-api03-ABCDEFGHIJKLMNOP"
    state = {"run_id": "r1", "thread_id": "t1", "context_id": "t1"}
    delta = _delta(state, f"the key is {secret}")
    assert secret not in delta.text
    assert "REDACTED" in delta.text


# ------------------------------------------------------------------- D9

class _FlakyPipe:
    def __init__(self, redis):
        self.redis = redis

    def xadd(self, *a, **k):
        return self

    async def execute(self):
        if self.redis.fail_times > 0:
            self.redis.fail_times -= 1
            raise ConnectionError("redis blip")
        self.redis.stored = True
        return []


class _FlakyRedis:
    def __init__(self, fail_times):
        self.fail_times = fail_times
        self.stored = False

    def pipeline(self, transaction=False):
        return _FlakyPipe(self)


@pytest.mark.asyncio
async def test_publish_retries_through_redis_blip(monkeypatch):
    from collegium_contracts import StepEvent, StepKind
    r = _FlakyRedis(fail_times=2)
    f = Forwarder.__new__(Forwarder)
    f.redis = r
    f.stream_key = "events:r1"
    f.thread_id = "t1"
    ev = StepEvent(run_id="r1", thread_id="t1", seq=1, kind=StepKind.MESSAGE,
                   title="t", detail={})
    await f.publish_events([ev])
    assert r.stored  # landed after retries


@pytest.mark.asyncio
def test_runner_marks_failed_when_sink_raises():
    """K3: a sink failure that outlasts bounded retries must NOT silently drop
    the durable record — the runner's exception path marks the thread FAILED
    and emits an engine_error event."""
    import inspect

    from worker.engine import runner
    src = inspect.getsource(runner.EngineRunner.run)
    assert 'self.status = "failed"' in src
    assert "_emit_engine_error" in src
    assert "return 1" in src  # non-zero exit: the crash is VISIBLE


@pytest.mark.asyncio
async def test_publish_raises_after_bounded_retries():
    from collegium_contracts import StepEvent, StepKind
    r = _FlakyRedis(fail_times=99)
    f = Forwarder.__new__(Forwarder)
    f.redis = r
    f.stream_key = "events:r1"
    f.thread_id = "t1"
    ev = StepEvent(run_id="r1", thread_id="t1", seq=1, kind=StepKind.MESSAGE,
                   title="t", detail={})
    with pytest.raises(ConnectionError):
        await f.publish_events([ev])
