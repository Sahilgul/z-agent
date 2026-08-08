"""eval_cases + eval_runs — fleet-bench. F2P/P2P scoring;
the Sleep-Time Distiller's bench gate reads these before drafting knowledge diffs.
"""

from __future__ import annotations

from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


def utcnow() -> datetime:
    return datetime.now(UTC)


class EvalCase(Base):
    __tablename__ = "eval_cases"

    id: Mapped[int] = mapped_column(primary_key=True)
    work_item_id: Mapped[int | None] = mapped_column(nullable=True)
    repo: Mapped[str] = mapped_column(sa.String(128))
    title: Mapped[str] = mapped_column(sa.String(256))
    task_text: Mapped[str] = mapped_column(sa.Text)
    base_commit: Mapped[str] = mapped_column(sa.String(64), default="")
    fail_to_pass: Mapped[list] = mapped_column(sa.JSON, default=list)
    pass_to_pass: Mapped[list] = mapped_column(sa.JSON, default=list)
    held_out: Mapped[bool] = mapped_column(default=False)  # distiller bench gate pool
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class EvalRun(Base):
    __tablename__ = "eval_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    case_id: Mapped[int] = mapped_column(sa.ForeignKey("eval_cases.id"), index=True)
    run_id: Mapped[str | None] = mapped_column(nullable=True)
    resolved: Mapped[bool] = mapped_column(default=False)
    f2p_passed: Mapped[int] = mapped_column(default=0)
    p2p_passed: Mapped[int] = mapped_column(default=0)
    report: Mapped[dict] = mapped_column(sa.JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
