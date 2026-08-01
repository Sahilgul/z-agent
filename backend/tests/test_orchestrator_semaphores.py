import pytest

from app.db.models.lane import Lane
from app.orchestrator import semaphores


def _make_lane(session, run_id="r1", lane_id="l1", status="running", repo_scope=None):
    lane = Lane(id=lane_id, run_id=run_id, persona="researcher", status=status,
                repo_scope=repo_scope)
    session.add(lane)
    session.commit()


async def test_try_acquire_under_global_cap(session, monkeypatch):
    cap = semaphores.Capacity()
    monkeypatch.setattr(semaphores.get_settings(), "global_lane_cap", 2)
    _make_lane(session, status="running")
    ok, reason = await cap.try_acquire(None)
    assert ok is True
    assert reason == ""
    _make_lane(session, lane_id="l2", status="queued")
    ok, reason = await cap.try_acquire(None)
    assert ok is False
    assert "global lane cap" in reason


async def test_try_acquire_writable_repo_lock(session):
    cap = semaphores.Capacity()
    _make_lane(session, status="running", repo_scope="ServerApp")
    ok, reason = await cap.try_acquire("ServerApp")
    assert ok is False
    assert "writable lane already active" in reason
    ok, reason = await cap.try_acquire("OtherRepo")
    assert ok is True


async def test_try_acquire_read_only_repo_unlimited(session):
    cap = semaphores.Capacity()
    _make_lane(session, status="running", repo_scope="ServerApp")
    ok, reason = await cap.try_acquire(None)
    assert ok is True


async def test_active_lane_count(session):
    cap = semaphores.Capacity()
    _make_lane(session, status="running")
    _make_lane(session, lane_id="l2", status="completed")
    _make_lane(session, lane_id="l3", status="interrupted")
    count = await cap.active_lane_count()
    assert count == 2  # running + interrupted are in ACTIVE_STATUSES


def test_active_statuses_includes_idle_and_interrupted():
    assert set(semaphores.ACTIVE_STATUSES) == {"queued", "running", "idle", "interrupted"}


async def test_module_level_capacity_singleton():
    assert isinstance(semaphores.capacity, semaphores.Capacity)


# --------------------------------------------------------------- reservations
async def test_reservation_counts_toward_cap_before_row_exists(session, monkeypatch):
    """Swarm race guard: a held reservation must consume cap even though no Lane
    row exists yet — otherwise N concurrent spawns all pass the check."""
    cap = semaphores.Capacity()
    monkeypatch.setattr(semaphores.get_settings(), "global_lane_cap", 2)
    ok, _ = await cap.try_acquire(None)
    assert ok is True  # reservation 1 held (no rows at all)
    _make_lane(session, status="running")
    ok, reason = await cap.try_acquire(None)
    assert ok is False  # 1 row + 1 reservation == cap 2
    assert "global lane cap" in reason


async def test_commit_reservation_frees_placeholder_once_row_counted(session, monkeypatch):
    cap = semaphores.Capacity()
    monkeypatch.setattr(semaphores.get_settings(), "global_lane_cap", 1)
    ok, _ = await cap.try_acquire("ServerApp")
    assert ok is True
    _make_lane(session, status="queued", repo_scope="ServerApp")
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
    assert "writable lane already active" in reason
    cap.release("ServerApp")
    ok, _ = await cap.try_acquire("ServerApp")
    assert ok is True
