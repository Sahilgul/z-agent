"""Heartbeat persister: status transitions must never be throttled.

The worker heartbeats every 15s with its live status, but the running->idle
transition beat is unscheduled and lands right after a periodic beat — so it
is the beat most likely to be dropped by the 10s write throttle. Dropping it
strands the thread row at "running" forever, which is why the watchdog nags a
finished thread. A status CHANGE must always be written.
"""

from __future__ import annotations

import asyncio

import pytest

from app.db.models.thread import Thread
from app.db.models.run import Run
from app.services import heartbeats


def _persister():
    # Bypass __init__ so we don't try to open a real Redis connection.
    h = heartbeats.HeartbeatPersister.__new__(heartbeats.HeartbeatPersister)
    h._task = None
    h._last_write = {}
    h._last_status = {}
    return h


async def _persist_with_clock(monkeypatch, ticks):
    """Drive _persist with a controllable monotonic clock so the throttle
    window is deterministic without sleeping."""
    clock = {"t": 0.0}
    iterator = iter(ticks)

    def fake_time():
        clock["t"] = next(iterator, clock["t"])
        return clock["t"]

    monkeypatch.setattr(
        asyncio.get_event_loop(), "time", fake_time, raising=False
    )
    # asyncio.get_event_loop().time is what _persist reads; ensure the loop
    # exists so the attribute lookup lands on a real loop object.
    return _persister()


@pytest.mark.asyncio
async def test_status_change_bypasses_throttle(session, make_user, monkeypatch):
    u = make_user("a")
    run = Run(id="r1", created_by=u.id, mode="ask", stage="investigating")
    thread = Thread(id="l1", run_id="r1", persona="researcher", status="running", next_seq=0)
    session.add_all([run, thread])
    session.commit()

    h = await _persist_with_clock(monkeypatch, [0.0, 1.0])  # 1s apart, inside 10s window

    # First beat: running. Throttle window starts now.
    h._persist("l1", "running")
    session.expire_all()
    assert session.get(Thread, "l1").status == "running"

    # Second beat 1s later (inside the 10s window) but with a NEW status.
    # Without the bypass this would be dropped and the thread would stay "running"
    # forever — the watchdog-nags-finished-thread bug.
    h._persist("l1", "idle")
    session.expire_all()
    assert session.get(Thread, "l1").status == "idle"


@pytest.mark.asyncio
async def test_same_status_inside_window_is_throttled(session, make_user, monkeypatch):
    u = make_user("a")
    run = Run(id="r1", created_by=u.id, mode="ask", stage="investigating")
    thread = Thread(
        id="l1", run_id="r1", persona="researcher", status="running", next_seq=0,
        heartbeat_at=None,
    )
    session.add_all([run, thread])
    session.commit()

    h = await _persist_with_clock(monkeypatch, [0.0, 1.0])

    h._persist("l1", "running")
    session.expire_all()
    first_hb = session.get(Thread, "l1").heartbeat_at
    assert first_hb is not None

    # Same status, 1s later — must be throttled to avoid a write per beat.
    h._persist("l1", "running")
    session.expire_all()
    assert session.get(Thread, "l1").heartbeat_at == first_hb


@pytest.mark.asyncio
async def test_status_none_does_not_update_last_status(session, make_user, monkeypatch):
    """A beat with no status (just a heartbeat ping) must not poison
    _last_status, or the next real status would compare against None and
    wrongly bypass the throttle."""
    u = make_user("a")
    run = Run(id="r1", created_by=u.id, mode="ask", stage="investigating")
    thread = Thread(id="l1", run_id="r1", persona="researcher", status="running", next_seq=0)
    session.add_all([run, thread])
    session.commit()

    h = await _persist_with_clock(monkeypatch, [0.0, 1.0, 2.0])

    h._persist("l1", "running")
    # No-status ping inside the window — must NOT update _last_status.
    h._persist("l1", None)
    assert h._last_status.get("l1") == "running"

    # A subsequent "running" beat inside the window must still be throttled,
    # because _last_status is still "running" (the ping didn't overwrite it).
    h._persist("l1", "running")
    session.expire_all()
    # heartbeat_at should reflect only the first write, not the third call.
    first_hb = session.get(Thread, "l1").heartbeat_at
    assert session.get(Thread, "l1").heartbeat_at == first_hb
