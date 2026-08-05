"""trigger_event_verdicts table — DB-level rate-limit scoping (M-39, coord D).

Revision ID: l7a8b9c0d1e2
Revises: k6f7a8b9c0d1
Create Date: 2026-08-05 20:15:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "l7a8b9c0d1e2"
down_revision = "k6f7a8b9c0d1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # M-39: a child verdict table lets _rate_limited scope by trigger_name at
    # DB level (indexed) instead of loading every matched log and counting
    # in Python. A single trigger_events row can carry verdicts for multiple
    # triggers (H-25), so the per-trigger association can't be a scalar on
    # the log row.
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = set(inspector.get_table_names())
    if "trigger_event_verdicts" not in existing:
        op.create_table(
            "trigger_event_verdicts",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("log_id", sa.Integer(), sa.ForeignKey("trigger_events.id"), nullable=False),
            sa.Column("trigger_name", sa.String(128), nullable=False),
            sa.Column("status", sa.String(16), nullable=False),
            sa.Column("run_id", sa.String(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        )
        op.create_index("ix_trigger_event_verdicts_log_id", "trigger_event_verdicts", ["log_id"])
        op.create_index("ix_trigger_event_verdicts_trigger_name", "trigger_event_verdicts", ["trigger_name"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = set(inspector.get_table_names())
    if "trigger_event_verdicts" in existing:
        op.drop_table("trigger_event_verdicts")
