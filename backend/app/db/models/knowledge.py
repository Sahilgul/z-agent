"""knowledge_items + playbooks — the flywheel corpus (SHARED, human-approved).
Privacy boundary: distilled lessons only, no transcripts; drafts from
PHI-bearing runs are scoped user until approved, NEVER global/repo as drafts.
Retrieval: cheap-model rerank by trigger_description at run start; ~200 rows, no
embeddings needed (the RAG ban is for code, not curated rows).
"""

from __future__ import annotations

from datetime import datetime, timezone

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class KnowledgeItem(Base):
    __tablename__ = "knowledge_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    content: Mapped[str] = mapped_column(sa.Text)
    trigger_description: Mapped[str] = mapped_column(sa.Text, default="")
    scope: Mapped[str] = mapped_column(sa.String(16), default="global")  # global|repo|user
    repo: Mapped[str | None] = mapped_column(sa.String(128), nullable=True)
    created_by: Mapped[int | None] = mapped_column(sa.ForeignKey("users.id"), nullable=True)
    source_run_id: Mapped[str | None] = mapped_column(nullable=True)
    status: Mapped[str] = mapped_column(sa.String(16), default="draft")  # draft|approved|rejected
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class Playbook(Base):
    """SKILL.md-format playbooks (SDK-native, preloadable via the skills field),
    versioned in DB and synced into workspaces."""

    __tablename__ = "playbooks"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(sa.String(128), unique=True)
    skill_md: Mapped[str] = mapped_column(sa.Text)
    version: Mapped[int] = mapped_column(default=1)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
