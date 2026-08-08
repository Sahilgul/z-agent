"""repos.remote_url unique — one remote, one identity (J5)

Revision ID: p0d1e2f3a4b5
Revises: o9c0d1e2f3a4
Create Date: 2026-08-08

Dedupes any pre-existing duplicate remote_urls (keeps the earliest row)
before adding the unique constraint.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "p0d1e2f3a4b5"
down_revision = "o9c0d1e2f3a4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(sa.text(
        "DELETE FROM repos WHERE remote_url != '' AND id NOT IN ("
        "  SELECT MIN(id) FROM repos WHERE remote_url != '' GROUP BY remote_url)"))
    uniques = {c["name"] for c in sa.inspect(op.get_bind()).get_unique_constraints("repos")}
    indexes = {i["name"] for i in sa.inspect(op.get_bind()).get_indexes("repos")}
    if "uq_repos_remote_url" not in uniques | indexes:
        op.create_index("uq_repos_remote_url", "repos", ["remote_url"],
                        unique=True,
                        sqlite_where=sa.text("remote_url != ''"),
                        postgresql_where=sa.text("remote_url != ''"))


def downgrade() -> None:
    op.drop_index("uq_repos_remote_url", table_name="repos")
