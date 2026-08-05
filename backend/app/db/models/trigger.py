"""triggers + trigger_events — triggers-as-data. ADO state vocabulary
(zagent-plan/zagent-dev/zagent-review) lives in ROW FILTERS, never in code; a new
state is config, a new source is one new normalizer. trigger_events is the
idempotency/dedupe log: (source, external_id, revision) unique.
"""

from __future__ import annotations

from datetime import datetime, timezone

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


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
