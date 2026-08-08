"""approvals — plan | tool | knowledge | pr. Timeout behavior is deterministic:
timeout = DENY + notify, EXCEPT Autonomous = allow-with-log.
"""

from __future__ import annotations

from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


def utcnow() -> datetime:
    return datetime.now(UTC)


class Approval(Base):
    __tablename__ = "approvals"
    __table_args__ = (
        # The console docks approvals inside the open session and polls:
        # WHERE run_id = ? AND decision IS NULL ORDER BY created_at DESC.
        sa.Index("ix_approvals_run_pending", "run_id", "decision", "created_at"),
    )

    id: Mapped[str] = mapped_column(sa.String(36), primary_key=True)  # uuid
    # M-36: nullable so a user-authored knowledge draft (no source run) can
    # still get a decidable Approval card — otherwise it was orphaned from the
    # card flow (stuck in "draft", never surfaced for review).
    run_id: Mapped[str | None] = mapped_column(sa.ForeignKey("runs.id"), nullable=True, index=True)
    thread_id: Mapped[str | None] = mapped_column(nullable=True)
    kind: Mapped[str] = mapped_column(sa.String(16))  # plan | tool | knowledge | pr
    payload: Mapped[dict] = mapped_column(sa.JSON, default=dict)
    decision: Mapped[str | None] = mapped_column(sa.String(16), nullable=True)  # approved|denied|timeout
    decided_by: Mapped[int | None] = mapped_column(sa.ForeignKey("users.id"), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
