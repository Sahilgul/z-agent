import asyncio
import json

import pytest

from app.events import relay as relay_mod


def _relay(fake_redis):
    r = relay_mod.Relay.__new__(relay_mod.Relay)
    r.redis = fake_redis
    r.subscribers = {}
    r._delta_tasks = {}
    return r


async def test_subscribe_returns_queue(fake_redis):
    r = _relay(fake_redis)
    q = r.subscribe("run-1")
    await asyncio.sleep(0)
    assert "run-1" in r.subscribers
    assert q in r.subscribers["run-1"]
    r.unsubscribe("run-1", q)


async def test_subscribe_starts_delta_loop(fake_redis):
    r = _relay(fake_redis)
    r.subscribe("run-1")
    await asyncio.sleep(0)
    assert "run-1" in r._delta_tasks
    r._delta_tasks["run-1"].cancel()


async def test_unsubscribe_removes_queue_and_cancels_loop(fake_redis):
    r = _relay(fake_redis)
    q = r.subscribe("run-1")
    await asyncio.sleep(0)
    r.unsubscribe("run-1", q)
    assert q not in r.subscribers.get("run-1", set())
    assert "run-1" not in r._delta_tasks


async def test_unsubscribe_unknown_run_is_noop(fake_redis):
    r = _relay(fake_redis)
    r.unsubscribe("ghost", None)


async def test_publish_step_fans_out_to_subscribers(fake_redis):
    r = _relay(fake_redis)
    q = r.subscribe("run-1")
    from zagent_contracts import StepEvent, StepKind
    ev = StepEvent(run_id="run-1", thread_id="l1", seq=1, kind=StepKind.MESSAGE, title="t")
    await r.publish_step("run-1", ev)
    msg = q.get_nowait()
    assert msg["type"] == "step"
    assert msg["event"]["kind"] == "message"
    r.unsubscribe("run-1", q)


async def test_publish_thread_status(fake_redis):
    r = _relay(fake_redis)
    q = r.subscribe("run-1")
    await r.publish_thread_status("run-1", "l1", "running")
    msg = q.get_nowait()
    assert msg == {"type": "thread_status", "thread_id": "l1", "status": "running"}
    r.unsubscribe("run-1", q)


async def test_publish_run_stage(fake_redis):
    r = _relay(fake_redis)
    q = r.subscribe("run-1")
    await r.publish_run_stage("run-1", "verifying", ["review_evidence"])
    msg = q.get_nowait()
    assert msg == {"type": "run_stage", "stage": "verifying", "available_actions": ["review_evidence"]}
    r.unsubscribe("run-1", q)


async def test_fanout_drops_slow_consumer(fake_redis):
    r = _relay(fake_redis)
    q = asyncio.Queue(maxsize=1)
    r.subscribers.setdefault("run-1", set()).add(q)
    await r._fanout("run-1", {"type": "x"})
    # M-67: verify the first message was actually delivered (queued) before
    # the eviction — the old test only asserted the queue was dropped on the
    # second message, so a regression that never queued the first would pass.
    assert q.qsize() == 1
    await r._fanout("run-1", {"type": "y"})  # queue full -> evicted
    assert q not in r.subscribers["run-1"]
    # M-53: the relay pushes a DROP_SENTINEL on eviction (popping the buffered
    # "x" to make room) so the consumer's queue.get() unblocks — verify it.
    from app.events.relay import DROP_SENTINEL
    assert q.get_nowait() is DROP_SENTINEL


async def test_publish_global_fans_to_all_runs(fake_redis):
    r = _relay(fake_redis)
    q1 = r.subscribe("run-1")
    q2 = r.subscribe("run-2")
    await r.publish_global({"type": "repo_added", "repo": "X"})
    assert q1.get_nowait()["type"] == "repo_added"
    assert q2.get_nowait()["type"] == "repo_added"
    r.unsubscribe("run-1", q1)
    r.unsubscribe("run-2", q2)


async def test_delta_loop_forwards_published_deltas(fake_redis, monkeypatch):
    r = _relay(fake_redis)
    fake_redis.pubsub_channels["deltas:run-1"] = []
    started = asyncio.Event()

    class FakePubSub:
        def __init__(self):
            self._unsubscribed = []
        async def subscribe(self, *ch): pass
        async def unsubscribe(self, *ch): self._unsubscribed.extend(ch)
        async def aclose(self): pass
        async def listen(self):
            yield {"type": "subscribe"}
            yield {"type": "message", "data": json.dumps({"text": "hi"})}
    monkeypatch.setattr(fake_redis, "pubsub", lambda: FakePubSub())
    q = r.subscribe("run-1")
    await r._delta_loop("run-1")
    msg = q.get_nowait()
    assert msg["type"] == "delta"
    assert msg["delta"] == {"text": "hi"}
    r.unsubscribe("run-1", q)


async def test_delta_loop_ignores_non_message_and_bad_json(fake_redis, monkeypatch):
    r = _relay(fake_redis)

    class FakePubSub:
        async def subscribe(self, *ch): pass
        async def unsubscribe(self, *ch): pass
        async def aclose(self): pass
        async def listen(self):
            yield {"type": "subscribe"}
            yield {"type": "message", "data": "{bad json"}
            yield {"type": "message", "data": json.dumps({"ok": True})}
    monkeypatch.setattr(fake_redis, "pubsub", lambda: FakePubSub())
    q = r.subscribe("run-1")
    await r._delta_loop("run-1")
    msg = q.get_nowait()
    assert msg["delta"] == {"ok": True}
    r.unsubscribe("run-1", q)


async def test_close_cancels_delta_tasks(fake_redis):
    r = _relay(fake_redis)
    r.subscribe("run-1")
    await asyncio.sleep(0)
    task = r._delta_tasks["run-1"]
    await r.close()
    await asyncio.sleep(0)
    assert task.cancelled() or task.done()
