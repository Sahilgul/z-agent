"""triggers + trigger_events — triggers-as-data. ADO state vocabulary
(collegium-plan/collegium-dev/collegium-review) lives in ROW FILTERS, never in code; a new
state is config, a new source is one new normalizer. trigger_events is the
idempotency/dedupe log: (source, external_id, revision) unique.
"""

from __future__ import annotations

from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


def utcnow() -> datetime:
    return datetime.now(UTC)


class Trigger(Base):
    __tablename__ = "triggers"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(sa.String(128), unique=True)
    source: Mapped[str] = mapped_column(sa.String(24))  # ado_webhook|cron
    filter_json: Mapped[dict] = mapped_column(sa.JSON, default=dict)
    mode: Mapped[str] = mapped_column(sa.String(32))
    autonomy: Mapped[str] = mapped_column(sa.String(16), default="gated")  # guardrail 4: never autonomous
    owner_resolution: Mapped[str] = mapped_column(sa.String(16), default="changed_by")  # changed_by|system
    enabled: Mapped[bool] = mapped_column(default=True)
    rate_limit_per_hour: Mapped[int] = mapped_column(default=20)  # guardrail 3
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class TriggerEventLog(Base):
    __tablename__ = "trigger_events"
    __table_args__ = (
        sa.UniqueConstraint("source", "external_id", "revision", name="uq_trigger_dedupe"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(sa.String(24))
    external_id: Mapped[str] = mapped_column(sa.String(64))
    revision: Mapped[int] = mapped_column()
    event_type: Mapped[str] = mapped_column(sa.String(64))
    changed_by_descriptor: Mapped[str | None] = mapped_column(sa.String(128), nullable=True)
    resolved_user_id: Mapped[int | None] = mapped_column(sa.ForeignKey("users.id"), nullable=True)
    run_id: Mapped[str | None] = mapped_column(nullable=True)
    status: Mapped[str] = mapped_column(sa.String(16), default="received")  # received|matched|ignored|failed
    payload: Mapped[dict] = mapped_column(sa.JSON, default=dict)
    received_at: Mapped[datetime] = mapped_column(default=utcnow)


class TriggerEventVerdict(Base):
    """M-39 (coord point D): one row per (log, trigger) verdict, so the
    rate-limit check can be scoped at DB level by trigger_name (indexed)
    instead of loading every matched log and counting in Python. A single
    TriggerEventLog can carry verdicts for multiple triggers (H-25), so the
    trigger association can't live as a scalar on the log row — a child table
    keeps the per-trigger count correct under a multi-trigger blast."""
    __tablename__ = "trigger_event_verdicts"

    id: Mapped[int] = mapped_column(primary_key=True)
    log_id: Mapped[int] = mapped_column(sa.ForeignKey("trigger_events.id"), index=True)
    trigger_name: Mapped[str] = mapped_column(sa.String(128), index=True)
    status: Mapped[str] = mapped_column(sa.String(16))  # started|queued|ignored|failed
    run_id: Mapped[str | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
