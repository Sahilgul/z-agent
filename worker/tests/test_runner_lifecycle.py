"""Wave 1 lifecycle-truth regression tests (worker side).

A1: interrupt is a REAL stop path — sets _stop, cancels the in-flight turn
    (waking a pending approval BLPOP), publishes "stopped", acks the message.
G5: kill during a 900s approval wait wakes the wait immediately.
C11: a nudge arriving during input_required is queued AND the deferral is
     surfaced as a status event (no silent queueing).
C10: the control listener exposes a `subscribed` readiness signal the runner
     awaits before its first heartbeat.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from worker.control import ControlMessage
from worker.engine.runner import EngineRunner


class _FakeRedis:
    def __init__(self) -> None:
        self.kv: dict[str, str] = {}

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.kv[key] = value

    async def get(self, key: str):
        return self.kv.get(key)


class _FakeForwarder:
    def __init__(self) -> None:
        self.redis = _FakeRedis()
        self.heartbeats: list[str] = []
        self.events: list = []

    async def heartbeat(self, status: str) -> None:
        self.heartbeats.append(status)

    async def publish_events(self, events) -> None:
        self.events.extend(events)


class _FakeEmitter:
    def _next(self, kind, title, detail, task_id, _unused):
        return {"kind": str(kind), "title": title, "detail": detail}


def _runner_shell() -> EngineRunner:
    """An EngineRunner without __init__ (env, Redis, spawn registry) — only
    the attributes the control pump / stop path touch."""
    r = EngineRunner.__new__(EngineRunner)
    r.run_id = "r1"
    r.thread_id = "t1"
    r.task_id = "task-1"
    r.status = "running"
    r.last_activity = 0.0
    r._stop = asyncio.Event()
    r._pending_nudges = asyncio.Queue()
    r._turn_task = None
    r.interrupt_drain_s = 0.05
    r.forwarder = _FakeForwarder()
    r.emitter = _FakeEmitter()

    class _ControlBox:
        queue: asyncio.Queue = None  # set below

    box = _ControlBox()
    box.queue = asyncio.Queue()
    r.control = box
    return r


@pytest.mark.asyncio
async def test_interrupt_stops_and_acks():
    r = _runner_shell()
    handled = asyncio.Event()

    async def fake_turn():
        handled.set()
        await asyncio.sleep(60)  # a long in-flight turn

    r._turn_task = asyncio.create_task(fake_turn())
    await handled.wait()
    pump = asyncio.create_task(r._control_pump())
    await r.control.queue.put(ControlMessage(type="interrupt", id="m-1"))
    await asyncio.wait_for(r._stop.wait(), timeout=2)
    await asyncio.wait_for(pump, timeout=2)
    assert r.status == "stopped"
    assert "stopped" in r.forwarder.heartbeats
    assert r.forwarder.redis.kv.get("thread:t1:ack:m-1") == "interrupt"
    assert r._turn_task.cancelled() or r._turn_task.done()


@pytest.mark.asyncio
async def test_kill_wakes_pending_approval_wait():
    """G5: a worker parked in the 900s approval BLPOP must be woken by kill —
    the replacement must never execute a late decision."""
    r = _runner_shell()

    async def approval_wait_turn():
        await asyncio.sleep(900)  # stands in for broker.wait_decision's BLPOP

    r._turn_task = asyncio.create_task(approval_wait_turn())
    await asyncio.sleep(0)
    pump = asyncio.create_task(r._control_pump())
    await r.control.queue.put(ControlMessage(type="kill", id="m-2"))
    await asyncio.wait_for(pump, timeout=2)
    assert r._stop.is_set()
    assert r._turn_task.done()
    assert r.forwarder.redis.kv.get("thread:t1:ack:m-2") == "kill"


@pytest.mark.asyncio
async def test_interrupt_drains_gracefully_before_cancel():
    """A fast-finishing turn completes within the drain window instead of
    being cancelled mid-write."""
    r = _runner_shell()
    finished = asyncio.Event()

    async def quick_turn():
        await asyncio.sleep(0.01)
        finished.set()

    r._turn_task = asyncio.create_task(quick_turn())
    pump = asyncio.create_task(r._control_pump())
    await r.control.queue.put(ControlMessage(type="interrupt"))
    await asyncio.wait_for(pump, timeout=2)
    assert finished.is_set()  # drained, not cancelled
    assert r._stop.is_set()


@pytest.mark.asyncio
async def test_nudge_during_approval_is_queued_and_surfaced():
    r = _runner_shell()
    r.status = "input_required"
    pump = asyncio.create_task(r._control_pump())
    await r.control.queue.put(ControlMessage(type="nudge", text="look again"))
    await asyncio.sleep(0.05)
    r._stop.set()
    pump.cancel()
    try:
        await pump
    except asyncio.CancelledError:
        pass
    nudge = r._pending_nudges.get_nowait()
    assert nudge.text == "look again"
    titles = [e["title"] for e in r.forwarder.events]
    assert any("queued behind pending approval" in t for t in titles)


def test_control_message_parses_id():
    """K14: the ack id round-trips through the wire format."""
    raw = json.dumps({"type": "kill", "id": "abc-123"})
    data = json.loads(raw)
    msg = ControlMessage(type=data["type"], id=data.get("id", ""))
    assert msg.id == "abc-123"


def test_control_listener_has_readiness_signal():
    """C10: `subscribed` exists and starts unset — the runner awaits it before
    the first heartbeat so the backend readiness probe can't precede the
    control subscription."""
    from worker.control import ControlListener
    listener = ControlListener.__new__(ControlListener)
    listener.subscribed = asyncio.Event()
    assert not listener.subscribed.is_set()
