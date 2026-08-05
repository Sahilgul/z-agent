"""index trajectory_summaries.user_id

M-51: episodic recall filters a user's OWN trajectory_summaries per run
start (privacy-safe: your history is yours), but user_id had no index, so
the per-user filter scanned the whole table as the corpus grew. Add an
index on user_id.

Revision ID: j5e6f7a8b9c0
Revises: i4d5e6f7a8b9
Create Date: 2026-08-05 19:30:00.000000
"""
from alembic import op
from sqlalchemy import inspect


revision = 'j5e6f7a8b9c0'
down_revision = 'i4d5e6f7a8b9'
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    names = {i["name"] for i in inspect(bind).get_indexes("trajectory_summaries")}
    if "ix_trajectory_summaries_user_id" not in names:
        op.create_index("ix_trajectory_summaries_user_id",
                        "trajectory_summaries", ["user_id"], unique=False)


def downgrade() -> None:
    bind = op.get_bind()
    names = {i["name"] for i in inspect(bind).get_indexes("trajectory_summaries")}
    if "ix_trajectory_summaries_user_id" in names:
        op.drop_index("ix_trajectory_summaries_user_id",
                       table_name="trajectory_summaries")
