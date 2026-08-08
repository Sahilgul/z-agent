"""capacity_reservations — DB-backed spawn reservations (H1).

The in-process Capacity reservations close the check-then-act race for ONE
backend process only. With feature_db_concurrency on, a reservation is a ROW:
the database serializes concurrent spawns across replicas, so two backends
cannot both pass the cap/writer checks before either's Thread row exists.

Lifecycle: try_acquire inserts a row (commit) -> the spawn either inserts the
Thread row (commit_reservation deletes the reservation — the row now owns
the slot) or fails (release deletes it). Reconcile-on-boot sweeps stale
reservations (a crashed backend's leftovers) by age.
"""

from __future__ import annotations

from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class CapacityReservation(Base):
    __tablename__ = "capacity_reservations"

    token: Mapped[str] = mapped_column(sa.String(36), primary_key=True)  # uuid
    repo_scope: Mapped[str | None] = mapped_column(sa.String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        default=lambda: datetime.now(UTC))

    __table_args__ = (
        # One ACTIVE reservation per writable repo — the reservation-level
        # equivalent of uq_threads_writable_repo_active.
        sa.Index("uq_reservation_repo", "repo_scope", unique=True,
                 sqlite_where=sa.text("repo_scope IS NOT NULL"),
                 postgresql_where=sa.text("repo_scope IS NOT NULL")),
    )
