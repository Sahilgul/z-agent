"""deliveries (run groups) + pr_links (plan §7)."""

from __future__ import annotations

from datetime import datetime, timezone

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Delivery(Base):
    __tablename__ = "deliveries"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(sa.String(256), default="")
    created_by: Mapped[int] = mapped_column(sa.ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class PrLink(Base):
    __tablename__ = "pr_links"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[str] = mapped_column(sa.ForeignKey("runs.id"), index=True)
    delivery_id: Mapped[int | None] = mapped_column(sa.ForeignKey("deliveries.id"), nullable=True)
    repo: Mapped[str] = mapped_column(sa.String(128))
    branch: Mapped[str] = mapped_column(sa.String(256))
    ado_pr_id: Mapped[int | None] = mapped_column(nullable=True)
    status: Mapped[str] = mapped_column(sa.String(16), default="open")  # open|merged|abandoned
    evidence: Mapped[dict] = mapped_column(sa.JSON, default=dict)  # tamper-proof package
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    merged_at: Mapped[datetime | None] = mapped_column(nullable=True)
    merged_by: Mapped[int | None] = mapped_column(sa.ForeignKey("users.id"), nullable=True)
