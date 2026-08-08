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

from app.db.models.run import Run
from app.db.models.thread import Thread
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
    session.expire_all()
    # Capture the heartbeat AFTER the first write, BEFORE the throttled third
    # call — the old test captured it after the third call, so the final
    # assert was `heartbeat_at == heartbeat_at` (always True, a vacuous
    # tautology that could never catch a throttle regression) (C-17).
    first_hb = session.get(Thread, "l1").heartbeat_at
    assert first_hb is not None

    # No-status ping inside the window — must NOT update _last_status.
    h._persist("l1", None)
    assert h._last_status.get("l1") == "running"

    # A subsequent "running" beat inside the window must still be throttled,
    # because _last_status is still "running" (the ping didn't overwrite it).
    h._persist("l1", "running")
    session.expire_all()
    # heartbeat_at should reflect only the first write, not the third call.
    assert session.get(Thread, "l1").heartbeat_at == first_hb


async def test_persist_rolls_back_on_db_error(session, make_user, monkeypatch):
    """G-22: a missed beat must never kill the heartbeat loop. _persist wraps
    the commit in try/except: on any DB error it rolls back and logs a warning,
    then returns (the next tick retries). The rollback-on-error path was
    untested. Force commit() to raise and assert _persist swallows it and
    calls rollback()."""
    u = make_user("a")
    session.add(Run(id="r1", created_by=u.id, mode="ask"))
    session.add(Thread(id="l1", run_id="r1", persona="researcher", status="running"))
    session.commit()

    rolled_back = {"n": 0}

    class _BoomSession:
        def __init__(self, real):
            self._real = real
        def get(self, *a, **k):
            return self._real.get(*a, **k)
        def commit(self):
            raise RuntimeError("db down")
        def rollback(self):
            rolled_back["n"] += 1
        def close(self):
            pass
        def expire_all(self):
            pass

    import app.services.heartbeats as hb_mod
    real_session = session
    monkeypatch.setattr(hb_mod, "get_session", lambda: _BoomSession(real_session))

    h = _persister()
    # Must not raise — the error is swallowed + rolled back.
    h._persist("l1", "running")
    assert rolled_back["n"] == 1


@pytest.mark.asyncio
async def test_stale_beat_never_resurrects_terminal_thread(session, make_user, monkeypatch):
    """Once the control plane stamps a terminal status (stop_thread,
    kill_replace, finish_thread), a dying container's last beats are stale by
    definition — they must not flip the row back to idle/running (this race
    would re-lock the repo write semaphore and re-take the capacity slot)."""
    u = make_user("a")
    run = Run(id="r1", created_by=u.id, mode="ask", stage="investigating")
    thread = Thread(id="l1", run_id="r1", persona="developer",
                    status="completed", next_seq=0)
    session.add_all([run, thread])
    session.commit()

    h = await _persist_with_clock(monkeypatch, [0.0, 1.0])
    h._persist("l1", "idle")  # stale beat from the just-killed container
    session.expire_all()
    row = session.get(Thread, "l1")
    assert row.status == "completed"  # terminal stamp wins
    assert row.heartbeat_at is not None  # but liveness still recorded
