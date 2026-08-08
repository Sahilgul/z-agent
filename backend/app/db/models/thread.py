"""threads — each an independent agent session in its own worker container.
forked_from_session_id: edit-and-resend preserves the original attempt as a
sibling branch (fork branches share run_id = one continuous timeline).

Renamed from `threads` (thread→thread mechanical rename).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.db.models.run import Run


def utcnow() -> datetime:
    return datetime.now(UTC)


class Thread(Base):
    __tablename__ = "threads"

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
    # M9 (accepted crypto posture, Wave 4): the virtual key is stored
    # PLAINTEXT by design, contained by lifecycle rather than encryption —
    # (1) the key is minted per-thread with a TTL (config.gateway_key_ttl) so
    # it self-expires at the gateway even if the DB row leaks; (2) it is
    # released at the gateway AND cleared here by the unified terminal
    # cleanup on EVERY terminal path, so terminal threads retain no live key
    # material; (3) the value is sk-lk-virtual-... — a gateway-scoped secret
    # with no provider privileges of its own. Encrypting at rest was
    # rejected: the key must be injected into the container env anyway, so
    # an attacker with DB read access and host access gains nothing more.
    gateway_key: Mapped[str | None] = mapped_column(sa.String(128), nullable=True)  # injected at container start
    next_seq: Mapped[int] = mapped_column(default=0)
    # Original prompt/persona_prompt — kill_replace respawns from this, never by
    # re-asking the blueprint (thread controls).
    spawn_context: Mapped[dict | None] = mapped_column(sa.JSON, nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(nullable=True)
    container_id: Mapped[str | None] = mapped_column(sa.String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(nullable=True)

    run: Mapped[Run] = relationship(back_populates="threads")

    # I4/H1: one ACTIVE writable thread per repo, enforced by the database
    # (not just the application-level check) — two backend replicas racing a
    # same-repo writable spawn cannot both win. Partial: read-only threads
    # (repo_scope NULL) are exempt; terminal threads don't hold the lock.
    __table_args__ = (
        sa.Index(
            "uq_threads_writable_repo_active", "repo_scope",
            unique=True,
            sqlite_where=sa.text(
                "repo_scope IS NOT NULL AND status IN "
                "('queued','running','idle','interrupted','input_required')"),
            postgresql_where=sa.text(
                "repo_scope IS NOT NULL AND status IN "
                "('queued','running','idle','interrupted','input_required')"),
        ),
    )
