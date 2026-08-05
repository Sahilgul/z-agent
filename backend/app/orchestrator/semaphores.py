"""Capacity semaphores (plan §4): global thread cap (12), per-repo WRITE lock (1
writable thread per repo — 10 writable threads means 10 different repos, never 10
writers on one). Read-only threads are unlimited per repo up to the global cap.
Requests beyond the cap queue deterministically.
"""

from __future__ import annotations

import asyncio

from app.core.config import get_settings
from app.db.base import get_session
from app.db.models.thread import Thread

ACTIVE_STATUSES = ("queued", "running", "idle", "interrupted")


class Capacity:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        # In-memory reservations close the check-then-act race: a swarm spawning
        # N threads concurrently would otherwise pass N cap checks before the first
        # Thread row exists to be counted. A reservation is held between try_acquire
        # and the row insert (commit_reservation) or spawn failure (release).
        self._reserved = 0
        self._reserved_writable: set[str] = set()

    async def active_thread_count(self) -> int:
        session = get_session()
        try:
            return session.query(Thread).filter(Thread.status.in_(ACTIVE_STATUSES)).count()
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
                        .filter(Thread.status.in_(ACTIVE_STATUSES),
                                Thread.repo_scope == writable_repo)
                        .count()
                    )
                finally:
                    session.close()
                if conflict:
                    return False, f"writable thread already active on {writable_repo}"
            self._reserved += 1
            if writable_repo:
                self._reserved_writable.add(writable_repo)
            return True, ""

    def commit_reservation(self, writable_repo: str | None) -> None:
        """The Thread row now exists (active_thread_count sees it) — drop the placeholder."""
        self._reserved = max(0, self._reserved - 1)
        if writable_repo:
            self._reserved_writable.discard(writable_repo)

    # release == commit: the slot frees either way (row exists, or spawn failed).
    release = commit_reservation


capacity = Capacity()
