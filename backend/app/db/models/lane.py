"""lanes — each an independent SDK session in its own worker container.
forked_from_session_id: edit-and-resend preserves the original attempt as a
sibling branch (fork branches share run_id = one continuous timeline).
"""

from __future__ import annotations

from datetime import datetime, timezone

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Lane(Base):
    __tablename__ = "lanes"

    id: Mapped[str] = mapped_column(sa.String(36), primary_key=True)  # uuid
    run_id: Mapped[str] = mapped_column(sa.ForeignKey("runs.id"), index=True)
    persona: Mapped[str] = mapped_column(sa.String(64))
    repo_scope: Mapped[str | None] = mapped_column(sa.String(128), nullable=True)
    session_id: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    forked_from_session_id: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    status: Mapped[str] = mapped_column(sa.String(16), default="queued")  # queued|running|completed|failed|stopped
    budget_usd: Mapped[float] = mapped_column(default=5.0)
    cost_usd: Mapped[float] = mapped_column(default=0.0)
    gateway_key_alias: Mapped[str | None] = mapped_column(sa.String(128), nullable=True)
    gateway_key: Mapped[str | None] = mapped_column(sa.String(128), nullable=True)  # injected at container start
    next_seq: Mapped[int] = mapped_column(default=0)
    # Original prompt/persona_prompt — kill_replace respawns from this, never by
    # re-asking the blueprint (plan §4 lane controls).
    spawn_context: Mapped[dict | None] = mapped_column(sa.JSON, nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(nullable=True)
    container_id: Mapped[str | None] = mapped_column(sa.String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(nullable=True)

    run: Mapped["Run"] = relationship(back_populates="lanes")
