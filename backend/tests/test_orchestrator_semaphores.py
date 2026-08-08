import asyncio

from app.db.models.thread import Thread
from app.orchestrator import semaphores


def _make_thread(session, run_id="r1", thread_id="l1", status="running", repo_scope=None):
    thread = Thread(id=thread_id, run_id=run_id, persona="researcher", status=status,
                repo_scope=repo_scope)
    session.add(thread)
    session.commit()


async def test_try_acquire_under_global_cap(session, monkeypatch):
    cap = semaphores.Capacity()
    monkeypatch.setattr(semaphores.get_settings(), "global_thread_cap", 2)
    _make_thread(session, status="running")
    ok, reason = await cap.try_acquire(None)
    assert ok is True
    assert reason == ""
    _make_thread(session, thread_id="l2", status="queued")
    ok, reason = await cap.try_acquire(None)
    assert ok is False
    assert "global thread cap" in reason


async def test_try_acquire_writable_repo_lock(session):
    cap = semaphores.Capacity()
    _make_thread(session, status="running", repo_scope="ServerApp")
    ok, reason = await cap.try_acquire("ServerApp")
    assert ok is False
    assert "writable thread already active" in reason
    ok, reason = await cap.try_acquire("OtherRepo")
    assert ok is True


async def test_try_acquire_read_only_repo_unlimited(session):
    cap = semaphores.Capacity()
    _make_thread(session, status="running", repo_scope="ServerApp")
    ok, reason = await cap.try_acquire(None)
    assert ok is True


async def test_active_thread_count(session):
    cap = semaphores.Capacity()
    _make_thread(session, status="running")
    _make_thread(session, thread_id="l2", status="completed")
    _make_thread(session, thread_id="l3", status="interrupted")
    count = await cap.active_thread_count()
    assert count == 2  # running + interrupted are in ACTIVE_STATUSES


def test_active_statuses_includes_idle_and_interrupted():
    assert set(semaphores.ACTIVE_STATUSES) == {"queued", "running", "idle", "interrupted"}


async def test_module_level_capacity_singleton():
    assert isinstance(semaphores.capacity, semaphores.Capacity)


# --------------------------------------------------------------- reservations
async def test_reservation_counts_toward_cap_before_row_exists(session, monkeypatch):
    """Swarm race guard: a held reservation must consume cap even though no Thread
    row exists yet — otherwise N concurrent spawns all pass the check."""
    cap = semaphores.Capacity()
    monkeypatch.setattr(semaphores.get_settings(), "global_thread_cap", 2)
    ok, _ = await cap.try_acquire(None)
    assert ok is True  # reservation 1 held (no rows at all)
    _make_thread(session, status="running")
    ok, reason = await cap.try_acquire(None)
    assert ok is False  # 1 row + 1 reservation == cap 2
    assert "global thread cap" in reason


async def test_commit_reservation_frees_placeholder_once_row_counted(session, monkeypatch):
    cap = semaphores.Capacity()
    monkeypatch.setattr(semaphores.get_settings(), "global_thread_cap", 1)
    ok, _ = await cap.try_acquire("ServerApp")
    assert ok is True
    _make_thread(session, status="queued", repo_scope="ServerApp")
    cap.commit_reservation("ServerApp")
    assert cap._reserved == 0
    assert "ServerApp" not in cap._reserved_writable
    ok, reason = await cap.try_acquire("ServerApp")
    assert ok is False  # the ROW now enforces the write lock, not the reservation


async def test_writable_reservation_blocks_second_writer_before_row(session):
    cap = semaphores.Capacity()
    ok, _ = await cap.try_acquire("ServerApp")
    assert ok is True
    ok, reason = await cap.try_acquire("ServerApp")
    assert ok is False
    assert "writable thread already active" in reason
    cap.release("ServerApp")
    ok, _ = await cap.try_acquire("ServerApp")
    assert ok is True


async def test_try_acquire_serializes_under_concurrent_gather(monkeypatch):
    """G-10: concurrent try_acquire calls under asyncio.gather must serialize
    on the lock — without the lock the check-then-reserve race would let N
    concurrent spawns all pass the cap check before any reservation is held
    (over-booking). Fire many concurrent acquisitions against a small cap
    and assert exactly `cap` succeed (no over-booking)."""
    cap = semaphores.Capacity()
    monkeypatch.setattr(semaphores.get_settings(), "global_thread_cap", 3)
    # No active rows — the cap is enforced purely by the in-memory reservation
    # under the lock, so this isolates the serialization (no DB timing noise).
    async def _no_rows() -> int:
        return 0
    monkeypatch.setattr(cap, "active_thread_count", _no_rows)
    results = await asyncio.gather(*[cap.try_acquire(None) for _ in range(10)])
    ok_count = sum(1 for ok, _ in results if ok)
    assert ok_count == 3, f"expected exactly 3 acquisitions under the cap, got {ok_count}"
    # The 7 rejected ones all cite the global cap.
    reasons = [reason for ok, reason in results if not ok]
    assert all("global thread cap" in r for r in reasons)
