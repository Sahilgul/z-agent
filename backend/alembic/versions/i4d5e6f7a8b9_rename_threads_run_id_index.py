"""rename ix_lanes_run_id -> ix_threads_run_id

M-50: the lane->thread table rename (f1a2b3c4d5e6) renamed the `lanes` table
to `threads` but left the `ix_lanes_run_id` index with its OLD name. The
Thread model (run_id index=True) expects `ix_threads_run_id`, so Alembic
autogenerate sees a schema drift on every migrated DB. Rename the index
to match the model.

Guarded for both DB lineages:
- a MIGRATED DB has `ix_lanes_run_id` and no `ix_threads_run_id` -> drop the
  legacy index and create the model-expected name.
- a FRESH DB (created from models) already has `ix_threads_run_id` and no
  `ix_lanes_run_id` -> this migration is a no-op.

Revision ID: i4d5e6f7a8b9
Revises: h3c4d5e6f7a8
Create Date: 2026-08-05 19:00:00.000000
"""
from sqlalchemy import inspect

from alembic import op

revision = 'i4d5e6f7a8b9'
down_revision = 'h3c4d5e6f7a8'
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    names = {i["name"] for i in inspect(bind).get_indexes("threads")}
    if "ix_lanes_run_id" in names:
        op.execute("DROP INDEX IF EXISTS ix_lanes_run_id")
    if "ix_threads_run_id" not in names:
        op.create_index("ix_threads_run_id", "threads", ["run_id"], unique=False)


def downgrade() -> None:
    bind = op.get_bind()
    names = {i["name"] for i in inspect(bind).get_indexes("threads")}
    if "ix_threads_run_id" in names:
        op.drop_index("ix_threads_run_id", table_name="threads")
    if "ix_lanes_run_id" not in names:
        op.create_index("ix_lanes_run_id", "threads", ["run_id"], unique=False)
