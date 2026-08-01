"""Capacity semaphores (plan §4): global lane cap (12), per-repo WRITE lock (1
writable lane per repo — 10 writable lanes means 10 different repos, never 10
writers on one). Read-only lanes are unlimited per repo up to the global cap.
Requests beyond the cap queue deterministically.
"""

from __future__ import annotations

import asyncio

from app.core.config import get_settings
from app.db.base import get_session
from app.db.models.lane import Lane

ACTIVE_STATUSES = ("queued", "running", "idle", "interrupted")


class Capacity:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        # In-memory reservations close the check-then-act race: a swarm spawning
        # N lanes concurrently would otherwise pass N cap checks before the first
        # Lane row exists to be counted. A reservation is held between try_acquire
        # and the row insert (commit_reservation) or spawn failure (release).
        self._reserved = 0
        self._reserved_writable: set[str] = set()

    async def active_lane_count(self) -> int:
        session = get_session()
        try:
            return session.query(Lane).filter(Lane.status.in_(ACTIVE_STATUSES)).count()
        finally:
            session.close()

    async def try_acquire(self, writable_repo: str | None) -> tuple[bool, str]:
        async with self._lock:
            cap = get_settings().global_lane_cap
            active = await self.active_lane_count()
            if active + self._reserved >= cap:
                return False, f"global lane cap ({cap}) reached — queued"
            if writable_repo:
                if writable_repo in self._reserved_writable:
                    return False, f"writable lane already active on {writable_repo}"
                session = get_session()
                try:
                    conflict = (
                        session.query(Lane)
                        .filter(Lane.status.in_(ACTIVE_STATUSES),
                                Lane.repo_scope == writable_repo)
                        .count()
                    )
                finally:
                    session.close()
                if conflict:
                    return False, f"writable lane already active on {writable_repo}"
            self._reserved += 1
            if writable_repo:
                self._reserved_writable.add(writable_repo)
            return True, ""

    def commit_reservation(self, writable_repo: str | None) -> None:
        """The Lane row now exists (active_lane_count sees it) — drop the placeholder."""
        self._reserved = max(0, self._reserved - 1)
        if writable_repo:
            self._reserved_writable.discard(writable_repo)

    # release == commit: the slot frees either way (row exists, or spawn failed).
    release = commit_reservation


capacity = Capacity()
