"""repos (the LIVE registry — repos-as-data) + repo_profiles.

integration_branch sourced from fleet-config/repos.json at seed
(golden tracks origin/<integrationBranch>, NEVER origin/HEAD
or the local current branch). repos.json is the bootstrap SEED; the DB row is
the live registry afterwards. Archived repos: fetcher stops, hidden from the scope
picker, old sessions still replay, golden dir shredded.
"""

from __future__ import annotations

from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


def utcnow() -> datetime:
    return datetime.now(UTC)


class RepoStatus:
    REGISTERED = "registered"
    VALIDATING = "validating"
    CLONING = "cloning"
    INDEXING = "indexing"
    READY = "ready"
    READY_NO_MAP = "ready-no-map"
    ERROR = "error"
    ARCHIVED = "archived"

    # Onboarding lands every repo at READY_NO_MAP until the map
    # generator covers its language, so anything selecting "repos an agent can
    # work on" must accept both — filtering on READY alone matches nothing.
    USABLE = (READY, READY_NO_MAP)


class Repo(Base):
    __tablename__ = "repos"
    __table_args__ = (
        sa.Index("uq_repos_remote_url", "remote_url", unique=True,
                 sqlite_where=sa.text("remote_url != ''"),
                 postgresql_where=sa.text("remote_url != ''")),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(sa.String(128), unique=True, index=True)
    ado_repo_id: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    # J5: unique (partial — empty URLs don't participate) — one remote must
    # never onboard into two identities. The app-level dedupe pre-check has
    # a TOCTOU window; this is the backstop.
    remote_url: Mapped[str] = mapped_column(sa.String(512), default="")
    integration_branch: Mapped[str] = mapped_column(sa.String(128))
    status: Mapped[str] = mapped_column(sa.String(24), default=RepoStatus.REGISTERED, index=True)
    status_detail: Mapped[str] = mapped_column(sa.Text, default="")
    added_by: Mapped[int | None] = mapped_column(sa.ForeignKey("users.id"), nullable=True)
    last_fetch_at: Mapped[datetime | None] = mapped_column(nullable=True)
    last_fetch_head: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    archived_at: Mapped[datetime | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)

    profile: Mapped[RepoProfile | None] = relationship(back_populates="repo")


class RepoProfile(Base):
    __tablename__ = "repo_profiles"

    id: Mapped[int] = mapped_column(primary_key=True)
    repo_id: Mapped[int] = mapped_column(sa.ForeignKey("repos.id"), unique=True)
    language: Mapped[str] = mapped_column(sa.String(32), default="")
    test_cmds: Mapped[list] = mapped_column(sa.JSON, default=list)
    map_version: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    extra: Mapped[dict] = mapped_column(sa.JSON, default=dict)

    repo: Mapped[Repo] = relationship(back_populates="profile")
