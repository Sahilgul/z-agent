"""trajectory_summaries — per-thread distilled trajectory, written at run end FROM
DAY ONE so the Sleep-Time Distiller has history to mine.
Episodic recall: a user's OWN trajectory_summaries are in their retrieval search
space (privacy-safe by construction — your own history is yours).
"""

from __future__ import annotations

from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


def utcnow() -> datetime:
    return datetime.now(UTC)


class TrajectorySummary(Base):
    __tablename__ = "trajectory_summaries"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[str] = mapped_column(sa.ForeignKey("runs.id"), index=True)
    thread_id: Mapped[str | None] = mapped_column(nullable=True)
    user_id: Mapped[int] = mapped_column(sa.ForeignKey("users.id"), index=True)
    summary: Mapped[str] = mapped_column(sa.Text)
    key_decisions: Mapped[list] = mapped_column(sa.JSON, default=list)
    lessons: Mapped[list] = mapped_column(sa.JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
