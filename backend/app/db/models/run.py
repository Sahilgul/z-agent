"""runs + plans + plan_steps. created_by is MANDATORY — sessions are
PRIVATE per teammate; every query hard-scopes by created_by at the API layer.
available_actions is computed by the orchestrator and rendered as-is by the UI.
Indexed by created_by+status+repo.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.db.models.event import Event
    from app.db.models.thread import Thread


def utcnow() -> datetime:
    return datetime.now(UTC)


class Run(Base):
    __tablename__ = "runs"
    __table_args__ = (
        sa.Index("ix_runs_owner_status_repo", "created_by", "stage", "repo"),
        # Session list + tab strip: WHERE created_by = ? ORDER BY last_active_at
        # DESC LIMIT 100. The filter-first index above can't serve the sort.
        sa.Index("ix_runs_owner_active", "created_by", "last_active_at"),
        # External-write idempotency: at most one run per (owner, key).
        # Partial — NULL keys (most runs) don't participate.
        sa.Index("uq_runs_owner_idem", "created_by", "idempotency_key",
                 unique=True,
                 sqlite_where=sa.text("idempotency_key IS NOT NULL"),
                 postgresql_where=sa.text("idempotency_key IS NOT NULL")),
    )

    id: Mapped[str] = mapped_column(sa.String(36), primary_key=True)  # uuid
    created_by: Mapped[int] = mapped_column(sa.ForeignKey("users.id"), nullable=False)
    source: Mapped[str] = mapped_column(sa.String(16), default="button")  # intent-bus source
    mode: Mapped[str] = mapped_column(sa.String(32))
    autonomy: Mapped[str] = mapped_column(sa.String(16), default="supervised")
    stage: Mapped[str] = mapped_column(sa.String(24), default="queued", index=True)
    title: Mapped[str] = mapped_column(sa.String(256), default="")
    auto_summary: Mapped[str] = mapped_column(sa.Text, default="")
    repo: Mapped[str | None] = mapped_column(sa.String(128), nullable=True)  # primary target
    work_item_id: Mapped[int | None] = mapped_column(nullable=True)
    # Client-supplied dedupe key for POST /runs retries (unique per owner
    # when present — see uq_runs_owner_idem).
    idempotency_key: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    delivery_id: Mapped[int | None] = mapped_column(sa.ForeignKey("deliveries.id"), nullable=True, index=True)  # campaign group
    available_actions: Mapped[list] = mapped_column(sa.JSON, default=list)
    session_volume_path: Mapped[str | None] = mapped_column(sa.String(512), nullable=True)
    cost_usd: Mapped[float] = mapped_column(default=0.0)
    tokens: Mapped[int] = mapped_column(default=0)
    legal_hold: Mapped[bool] = mapped_column(default=False)
    started_at: Mapped[datetime | None] = mapped_column(nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(nullable=True)
    last_active_at: Mapped[datetime] = mapped_column(default=utcnow)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)

    # Explicit ORM relationships REQUIRED for UoW insert ordering — table-level
    # FKs alone do not order batched parent+child inserts (verified empirically).
    threads: Mapped[list[Thread]] = relationship(back_populates="run")
    events: Mapped[list[Event]] = relationship(back_populates="run")


class Plan(Base):
    __tablename__ = "plans"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[str] = mapped_column(sa.ForeignKey("runs.id"), index=True)
    structured: Mapped[dict] = mapped_column(sa.JSON)  # contracts.Plan payload
    status: Mapped[str] = mapped_column(sa.String(16), default="draft")  # draft|approved|rejected
    decided_by: Mapped[int | None] = mapped_column(sa.ForeignKey("users.id"), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)

    steps: Mapped[list[PlanStep]] = relationship(back_populates="plan", cascade="all, delete-orphan")


class PlanStep(Base):
    __tablename__ = "plan_steps"

    id: Mapped[int] = mapped_column(primary_key=True)
    plan_id: Mapped[int] = mapped_column(sa.ForeignKey("plans.id"), index=True)
    index: Mapped[int] = mapped_column()
    title: Mapped[str] = mapped_column(sa.String(256))
    description: Mapped[str] = mapped_column(sa.Text, default="")
    repo: Mapped[str | None] = mapped_column(sa.String(128), nullable=True)
    files: Mapped[list] = mapped_column(sa.JSON, default=list)
    success_criterion: Mapped[str] = mapped_column(sa.Text, default="")
    status: Mapped[str] = mapped_column(sa.String(16), default="pending")

    plan: Mapped[Plan] = relationship(back_populates="steps")
