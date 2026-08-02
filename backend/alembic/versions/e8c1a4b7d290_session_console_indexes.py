"""session console indexes

Covering indexes for the single-screen console's hot reads:
  * approvals docked in the open session (run_id + pending + recency)
  * the session list / tab strip (owner + last activity)
  * replay and the JSONL transcript fallback (run + per-lane seq order)

Revision ID: e8c1a4b7d290
Revises: d75ff5e54b33
Create Date: 2026-08-02 19:15:00.000000

"""
from alembic import op

revision = 'e8c1a4b7d290'
down_revision = 'd75ff5e54b33'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("ix_approvals_run_pending", "approvals",
                    ["run_id", "decision", "created_at"])
    op.create_index("ix_runs_owner_active", "runs",
                    ["created_by", "last_active_at"])
    op.create_index("ix_events_run_lane_seq", "events",
                    ["run_id", "lane_id", "seq"])


def downgrade() -> None:
    op.drop_index("ix_events_run_lane_seq", table_name="events")
    op.drop_index("ix_runs_owner_active", table_name="runs")
    op.drop_index("ix_approvals_run_pending", table_name="approvals")
