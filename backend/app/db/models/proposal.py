"""proposals — Janitor/Perfector ranked Improvement Inbox items.
Team-wide readable (cite code, not sessions); NEVER unsolicited PRs at scale.
Accept -> Development run; Dismiss -> preference signal to the flywheel.
"""

from __future__ import annotations

from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


def utcnow() -> datetime:
    return datetime.now(UTC)


class Proposal(Base):
    __tablename__ = "proposals"

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(sa.String(16))  # janitor|perfector
    repo: Mapped[str | None] = mapped_column(sa.String(128), nullable=True)
    title: Mapped[str] = mapped_column(sa.String(256))
    body: Mapped[str] = mapped_column(sa.Text, default="")
    evidence: Mapped[list] = mapped_column(sa.JSON, default=list)  # file:line citations
    impact: Mapped[str] = mapped_column(sa.String(16), default="medium")
    confidence: Mapped[str] = mapped_column(sa.String(16), default="medium")
    status: Mapped[str] = mapped_column(sa.String(16), default="proposed")  # proposed|accepted|dismissed
    promoted_run_id: Mapped[str | None] = mapped_column(nullable=True)
    created_by: Mapped[str] = mapped_column(sa.String(16), default="system")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
