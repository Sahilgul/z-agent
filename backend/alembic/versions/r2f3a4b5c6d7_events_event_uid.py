"""events.event_uid — emission-stamped idempotency key (W-B6)

The deep seam fix: worker and backend used to BOTH persist seqs (worker seq
file + thread.next_seq for backend-direct writes), so a user message and a
concurrent worker event raced for the same seq and the unique constraint
silently ate the worker event. Now the worker stamps a uuid4 per emission;
the ingest consumer dedupes on (run_id, event_uid) and assigns the
authoritative seq from thread.next_seq under the thread row lock.

Data step: none needed — existing rows keep event_uid NULL, and NULL keys
never collide under the unique index (both SQLite and Postgres). A dedupe
pass over (run_id, thread_id, seq) is unnecessary: that constraint already
exists and was the thing doing silent drops; nothing new to clean up.

Revision ID: r2f3a4b5c6d7
Revises: q1e2f3a4b5c6
Create Date: 2026-08-08
"""

import sqlalchemy as sa

from alembic import op

revision = "r2f3a4b5c6d7"
down_revision = "q1e2f3a4b5c6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("events", sa.Column("event_uid", sa.String(64), nullable=True))
    op.create_index("uq_events_run_event_uid", "events",
                    ["run_id", "event_uid"], unique=True)


def downgrade() -> None:
    op.drop_index("uq_events_run_event_uid", table_name="events")
    op.drop_column("events", "event_uid")
