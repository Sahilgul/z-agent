"""events — the full replayable StepEvent stream; PHI-BEARING STORAGE.
sdk_message_uuid NULLABLE (the edit-and-resend bridge; null = non-message /
pre-instrumentation — free to add now, migration headache later). Durable with a
CONFIGURABLE TTL (default 12 months, legal-hold override per run); a maintenance
job purges expired rows. ~30GB SQLite capacity guardrail pulls Postgres forward.

Renamed thread_id → thread_id (thread→thread mechanical rename).
"""

from __future__ import annotations

from datetime import datetime, timezone

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Event(Base):
    __tablename__ = "events"
    __table_args__ = (
        sa.Index("ix_events_thread_seq", "thread_id", "seq"),
        sa.Index("ix_events_run_ts", "run_id", "ts"),
        # Replay + JSONL transcript fallback: WHERE run_id = ? [AND thread_id = ?]
        # [AND seq > ?] ORDER BY thread_id, seq — filter and sort from one index.
        sa.Index("ix_events_run_thread_seq", "run_id", "thread_id", "seq"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(sa.ForeignKey("runs.id"))
    thread_id: Mapped[str] = mapped_column()
    seq: Mapped[int] = mapped_column()
    ts: Mapped[datetime] = mapped_column(default=utcnow)
    type: Mapped[str] = mapped_column(sa.String(24))  # StepKind value
    title: Mapped[str] = mapped_column(sa.String(512), default="")
    payload: Mapped[dict] = mapped_column(sa.JSON, default=dict)
    sdk_message_uuid: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)

    run: Mapped["Run"] = relationship(back_populates="events")
