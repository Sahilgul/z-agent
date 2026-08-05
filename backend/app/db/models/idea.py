"""Ideas space — SHARED team-wide by design, never privacy-scoped.
Every member's comments persist permanently; Counsel's comments live alongside
human comments forever (author_type=agent, author_ref='counsel').
"""

from __future__ import annotations

from datetime import datetime, timezone

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class IdeaThread(Base):
    __tablename__ = "idea_threads"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(sa.String(256))
    body: Mapped[str] = mapped_column(sa.Text, default="")
    created_by: Mapped[int] = mapped_column(sa.ForeignKey("users.id"))
    source: Mapped[str] = mapped_column(sa.String(16), default="user")  # user|proposal
    proposal_id: Mapped[int | None] = mapped_column(nullable=True)
    status: Mapped[str] = mapped_column(sa.String(16), default="open")  # open|summarized|promoted|archived
    summary_json: Mapped[dict | None] = mapped_column(sa.JSON, nullable=True)
    promoted_run_id: Mapped[str | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)

    comments: Mapped[list["IdeaComment"]] = relationship(back_populates="thread", cascade="all, delete-orphan")


class IdeaComment(Base):
    __tablename__ = "idea_comments"

    id: Mapped[int] = mapped_column(primary_key=True)
    thread_id: Mapped[int] = mapped_column(sa.ForeignKey("idea_threads.id"), index=True)
    author_type: Mapped[str] = mapped_column(sa.String(8))  # user|agent
    author_ref: Mapped[str] = mapped_column(sa.String(64))  # user id | 'counsel' | 'lead'
    body: Mapped[str] = mapped_column(sa.Text)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)

    thread: Mapped["IdeaThread"] = relationship(back_populates="comments")
