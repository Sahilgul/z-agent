"""users + one-time setup codes. Identity binding: names are labels,
never keys — ado_descriptor (Graph-resolved GUID) is the identity key, bound at
provisioning, fail-loud on 0 or 2+ matches. Offboarding = DEACTIVATE + token_version
bump; never delete (shared threads keep attribution).
"""

from __future__ import annotations

from datetime import datetime, timezone

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(sa.String(64), unique=True, index=True)
    pin_hash: Mapped[str | None] = mapped_column(sa.String(128), nullable=True)  # null until forced PIN choice
    display_name: Mapped[str] = mapped_column(sa.String(128), default="")
    role: Mapped[str] = mapped_column(sa.String(16), default="member")  # member | admin
    status: Mapped[str] = mapped_column(sa.String(16), default="pending")  # pending | active | deactivated
    token_version: Mapped[int] = mapped_column(default=0)
    ado_email: Mapped[str | None] = mapped_column(sa.String(256), nullable=True)
    ado_descriptor: Mapped[str | None] = mapped_column(sa.String(128), nullable=True, unique=True)
    byo_pat_encrypted: Mapped[str | None] = mapped_column(sa.Text, nullable=True)  # write-only
    byo_pat_expires_at: Mapped[datetime | None] = mapped_column(nullable=True)
    failed_pin_attempts: Mapped[int] = mapped_column(default=0)
    locked_until: Mapped[datetime | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)

    setup_codes: Mapped[list["SetupCode"]] = relationship(back_populates="user")


class SetupCode(Base):
    """One-time provisioning codes: shown ONCE with a copy button; regenerate
    invalidates the old one. CLI survives only to bootstrap the FIRST admin."""

    __tablename__ = "setup_codes"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(sa.ForeignKey("users.id"), index=True)
    code_hash: Mapped[str] = mapped_column(sa.String(128))
    used_at: Mapped[datetime | None] = mapped_column(nullable=True)
    invalidated_at: Mapped[datetime | None] = mapped_column(nullable=True)
    expires_at: Mapped[datetime] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(default=utcnow)

    user: Mapped["User"] = relationship(back_populates="setup_codes")
