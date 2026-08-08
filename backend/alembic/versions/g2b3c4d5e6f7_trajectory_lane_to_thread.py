"""trajectory_summaries lane to thread rename

The f1a2b3c4d5e6 lane→thread rename missed trajectory_summaries.lane_id;
the model declares thread_id, so every read of the table failed with
"no such column". Pure column rename, data unchanged.

Revision ID: g2b3c4d5e6f7
Revises: f1a2b3c4d5e6
Create Date: 2026-08-05 07:30:00.000000

"""
import sqlalchemy as sa

from alembic import op

revision = 'g2b3c4d5e6f7'
down_revision = 'f1a2b3c4d5e6'
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("trajectory_summaries", schema=None) as batch_op:
            batch_op.alter_column("lane_id", new_column_name="thread_id",
                                  existing_type=sa.String(), nullable=True)
    else:
        op.alter_column("trajectory_summaries", "lane_id", new_column_name="thread_id",
                        existing_type=sa.String(), nullable=True)


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("trajectory_summaries", schema=None) as batch_op:
            batch_op.alter_column("thread_id", new_column_name="lane_id",
                                  existing_type=sa.String(), nullable=True)
    else:
        op.alter_column("trajectory_summaries", "thread_id", new_column_name="lane_id",
                        existing_type=sa.String(), nullable=True)
