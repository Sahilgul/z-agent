"""approvals.run_id nullable

M-36: a user-authored knowledge draft (no source run) used to get NO approval
card — it was orphaned from the card flow (stuck in "draft", never surfaced
for review). Make approvals.run_id nullable so such a draft can still get a
decidable Approval card (run_id=NULL). The decide endpoint acts by
approval_id, not run_id, so a NULL run_id card is decidable.

SQLite note: batch_alter_table is used because SQLite has limited ALTER
support; the table is copied. Postgres supports direct ALTER.

Revision ID: h3c4d5e6f7a8
Revises: g2b3c4d5e6f7
Create Date: 2026-08-05 18:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = 'h3c4d5e6f7a8'
down_revision = 'g2b3c4d5e6f7'
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("approvals", schema=None) as batch_op:
            batch_op.alter_column("run_id", existing_type=sa.String(),
                                  nullable=True)
    else:
        op.alter_column("approvals", "run_id",
                         existing_type=sa.String(), nullable=True)


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("approvals", schema=None) as batch_op:
            batch_op.alter_column("run_id", existing_type=sa.String(),
                                  nullable=False)
    else:
        op.alter_column("approvals", "run_id",
                         existing_type=sa.String(), nullable=False)
