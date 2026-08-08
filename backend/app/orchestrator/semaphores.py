"""Capacity: global thread cap (100) + per-repo WRITE lock (one writable
thread per repo). Read-only threads are unlimited per repo up to the global
cap. Requests beyond the cap queue deterministically.

Two enforcement layers:

  * In-process reservations (always on): close the check-then-act race
    between try_acquire and the Thread row insert within ONE backend.
  * DB-backed reservations (feature_db_concurrency): a capacity_reservations
    ROW per in-flight spawn, so concurrent spawns serialize at the database
    across replicas (H1). The uq_reservation_repo unique partial index makes
    the one-writer-per-repo rule collide at INSERT, not at a racy SELECT.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import sqlalchemy as sa

from app.core.config import get_settings
from app.db.base import get_session
from app.db.models.reservation import CapacityReservation
from app.db.models.thread import Thread

ACTIVE_STATUSES = ("queued", "running", "idle", "interrupted")

# A4: capacity/write-lock accounting is BROADER than liveness. A thread
# parked at input_required (approval card open, blocked escalation) is not
# "active" for the heartbeat mirror, but it still holds a live container —
# releasing its slot or its repo write lock would let a second writable
# thread mount the same repo while the parked one can still resume.
CAPACITY_STATUSES = (*ACTIVE_STATUSES, "input_required")

# A reservation older than this without a Thread row is a crashed backend's
# leftover — sweep it instead of leaking the slot forever.
RESERVATION_TTL_S = 300


class Capacity:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        # In-memory reservations close the check-then-act race: a swarm spawning
        # N threads concurrently would otherwise pass N cap checks before the first
        # Thread row exists to be counted. A reservation is held between try_acquire
        # and the row insert (commit_reservation) or spawn failure (release).
        self._reserved = 0
        self._reserved_writable: set[str] = set()
        # DB-backed reservation tokens held by THIS process (flag-gated path).
        self._db_tokens: list[tuple[str, str | None]] = []

    async def active_thread_count(self) -> int:
        session = get_session()
        try:
            return session.query(Thread).filter(Thread.status.in_(CAPACITY_STATUSES)).count()
        finally:
            session.close()

    async def try_acquire(self, writable_repo: str | None) -> tuple[bool, str]:
        async with self._lock:
            cap = get_settings().global_thread_cap
            active = await self.active_thread_count()
            if active + self._reserved >= cap:
                return False, f"global thread cap ({cap}) reached — queued"
            if writable_repo:
                if writable_repo in self._reserved_writable:
                    return False, f"writable thread already active on {writable_repo}"
                session = get_session()
                try:
                    conflict = (
                        session.query(Thread)
                        .filter(Thread.status.in_(CAPACITY_STATUSES),
                                Thread.repo_scope == writable_repo)
                        .count()
                    )
                finally:
                    session.close()
                if conflict:
                    return False, f"writable thread already active on {writable_repo}"
            if get_settings().feature_db_concurrency:
                ok, reason = self._db_reserve(cap, active, writable_repo)
                if not ok:
                    return False, reason
            self._reserved += 1
            if writable_repo:
                self._reserved_writable.add(writable_repo)
            return True, ""

    def _db_reserve(self, cap: int, active: int,
                    writable_repo: str | None) -> tuple[bool, str]:
        """H1: DB-backed reservation. The INSERT itself is the concurrency
        primitive — SQLite serializes writers; on Postgres the unique partial
        index rejects a second same-repo reservation. Stale rows (crashed
        backend) are swept by age before counting."""
        session = get_session()
        try:
            cutoff = datetime.now(UTC) - timedelta(seconds=RESERVATION_TTL_S)
            session.query(CapacityReservation).filter(
                CapacityReservation.created_at < cutoff).delete()
            held = session.query(CapacityReservation).count()
            if active + held >= cap:
                session.rollback()
                return False, f"global thread cap ({cap}) reached — queued"
            if writable_repo:
                conflict = session.query(CapacityReservation).filter_by(
                    repo_scope=writable_repo).count()
                if conflict:
                    session.rollback()
                    return False, f"writable thread already active on {writable_repo}"
            token = str(uuid.uuid4())
            try:
                session.add(CapacityReservation(token=token, repo_scope=writable_repo))
                session.commit()
            except sa.exc.IntegrityError:
                # Unique partial index collision: another replica reserved this
                # repo between our check and insert. That IS the enforcement.
                session.rollback()
                return False, f"writable thread already active on {writable_repo}"
            self._db_tokens.append((token, writable_repo))
            return True, ""
        finally:
            session.close()

    def _db_release(self, writable_repo: str | None) -> None:
        if not self._db_tokens:
            return
        token, _repo = self._db_tokens.pop()
        session = get_session()
        try:
            session.query(CapacityReservation).filter_by(token=token).delete()
            session.commit()
        finally:
            session.close()

    def commit_reservation(self, writable_repo: str | None) -> None:
        """The Thread row now exists (active_thread_count sees it) — drop the placeholder."""
        self._reserved = max(0, self._reserved - 1)
        if writable_repo:
            self._reserved_writable.discard(writable_repo)
        if get_settings().feature_db_concurrency:
            self._db_release(writable_repo)

    # release == commit: the slot frees either way (row exists, or spawn failed).
    release = commit_reservation


capacity = Capacity()
