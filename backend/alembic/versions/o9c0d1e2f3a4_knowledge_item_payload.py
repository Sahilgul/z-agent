"""knowledge_items.payload — no-silent-skip rejection metadata

Revision ID: o9c0d1e2f3a4
Revises: n8b9c0d1e2f3
Create Date: 2026-08-08
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "o9c0d1e2f3a4"
down_revision = "n8b9c0d1e2f3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    cols = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("knowledge_items")}
    if "payload" not in cols:
        op.add_column("knowledge_items",
                      sa.Column("payload", sa.JSON, nullable=True))
    op.execute("UPDATE knowledge_items SET payload = '{}' WHERE payload IS NULL")


def downgrade() -> None:
    op.drop_column("knowledge_items", "payload")
