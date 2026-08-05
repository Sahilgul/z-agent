"""lane to thread rename

Mechanical rename (Plan Phase 1): the `lanes` table becomes `threads`, and
every `lane_id` column becomes `thread_id`. Indexes named with `lane` are
dropped and recreated with `thread`. The data is unchanged — this is a pure
schema rename.

SQLite note: batch_alter_table is used because SQLite has limited ALTER
support; the table is copied. Postgres supports direct RENAME.

Revision ID: f1a2b3c4d5e6
Revises: e8c1a4b7d290
Create Date: 2026-08-05 06:00:00.000000

"""
from alembic import op
import sqlalchemy as sa

revision = 'f1a2b3c4d5e6'
down_revision = 'e8c1a4b7d290'
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    is_sqlite = bind.dialect.name == "sqlite"

    # 1. lanes -> threads (table rename). SQLite supports ALTER TABLE RENAME
    # directly; Postgres too. No batch_alter needed for a pure table rename.
    op.execute("ALTER TABLE lanes RENAME TO threads")

    # 2. events.lane_id -> events.thread_id + index renames
    op.drop_index("ix_events_lane_seq", table_name="events")
    op.drop_index("ix_events_run_lane_seq", table_name="events")
    if is_sqlite:
        with op.batch_alter_table("events", schema=None) as batch_op:
            batch_op.alter_column("lane_id", new_column_name="thread_id",
                                  existing_type=sa.String(), nullable=False)
    else:
        op.alter_column("events", "lane_id", new_column_name="thread_id",
                        existing_type=sa.String(), nullable=False)
    op.create_index("ix_events_thread_seq", "events", ["thread_id", "seq"], unique=False)
    op.create_index("ix_events_run_thread_seq", "events", ["run_id", "thread_id", "seq"], unique=False)

    # 3. approvals.lane_id -> approvals.thread_id
    if is_sqlite:
        with op.batch_alter_table("approvals", schema=None) as batch_op:
            batch_op.alter_column("lane_id", new_column_name="thread_id",
                                  existing_type=sa.String(), nullable=True)
    else:
        op.alter_column("approvals", "lane_id", new_column_name="thread_id",
                        existing_type=sa.String(), nullable=True)


def downgrade() -> None:
    bind = op.get_bind()
    is_sqlite = bind.dialect.name == "sqlite"

    if is_sqlite:
        with op.batch_alter_table("approvals", schema=None) as batch_op:
            batch_op.alter_column("thread_id", new_column_name="lane_id",
                                  existing_type=sa.String(), nullable=True)
    else:
        op.alter_column("approvals", "thread_id", new_column_name="lane_id",
                        existing_type=sa.String(), nullable=True)

    op.drop_index("ix_events_run_thread_seq", table_name="events")
    op.drop_index("ix_events_thread_seq", table_name="events")
    if is_sqlite:
        with op.batch_alter_table("events", schema=None) as batch_op:
            batch_op.alter_column("thread_id", new_column_name="lane_id",
                                  existing_type=sa.String(), nullable=False)
    else:
        op.alter_column("events", "thread_id", new_column_name="lane_id",
                        existing_type=sa.String(), nullable=False)
    op.create_index("ix_events_run_lane_seq", "events", ["run_id", "lane_id", "seq"], unique=False)
    op.create_index("ix_events_lane_seq", "events", ["lane_id", "seq"], unique=False)

    op.execute("ALTER TABLE threads RENAME TO lanes")
