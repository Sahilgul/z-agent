"""runs.idempotency_key + partial unique index (POST /runs dedupe)

Revision ID: n8b9c0d1e2f3
Revises: m7b8c9d0e1f2
Create Date: 2026-08-08

A client-supplied idempotency key dedupes retried run creation. Partial
unique index: NULL keys (most runs) do not participate.
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "n8b9c0d1e2f3"
down_revision = "m7b8c9d0e1f2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    cols = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("runs")}
    if "idempotency_key" not in cols:
        op.add_column("runs", sa.Column("idempotency_key", sa.String(64), nullable=True))
    indexes = {i["name"] for i in sa.inspect(op.get_bind()).get_indexes("runs")}
    if "uq_runs_owner_idem" not in indexes:
        op.create_index("uq_runs_owner_idem", "runs",
                        ["created_by", "idempotency_key"], unique=True,
                        sqlite_where=sa.text("idempotency_key IS NOT NULL"),
                        postgresql_where=sa.text("idempotency_key IS NOT NULL"))


def downgrade() -> None:
    op.drop_index("uq_runs_owner_idem", table_name="runs")
    op.drop_column("runs", "idempotency_key")
