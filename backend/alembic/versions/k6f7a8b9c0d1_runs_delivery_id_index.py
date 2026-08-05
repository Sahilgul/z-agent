"""Index runs.delivery_id (filtered in campaigns).

Revision ID: k6f7a8b9c0d1
Revises: j5e6f7a8b9c0
Create Date: 2026-08-05 18:30:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "k6f7a8b9c0d1"
down_revision = "j5e6f7a8b9c0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # L-23: runs.delivery_id is filtered in campaign views (WHERE
    # delivery_id = ?) but had no index — add one. Guard for fresh DBs that
    # may already have it (e.g. created inline by a fresh full-schema build).
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    indexes = {idx["name"] for idx in inspector.get_indexes("runs")}
    if "ix_runs_delivery_id" not in indexes:
        op.create_index("ix_runs_delivery_id", "runs", ["delivery_id"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    indexes = {idx["name"] for idx in inspector.get_indexes("runs")}
    if "ix_runs_delivery_id" in indexes:
        op.drop_index("ix_runs_delivery_id", table_name="runs")
