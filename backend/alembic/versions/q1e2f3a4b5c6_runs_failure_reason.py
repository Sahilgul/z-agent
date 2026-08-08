"""runs.failure_reason — persist WHY a run failed (W-H13)

The console used to flip a run to "failed" with no explanation anywhere on
the surface — the only trace was a best-effort relay note that a client
opening the session later never saw. Stamping the reason on the row lets
the UI render an inline banner on every subsequent open.

Revision ID: q1e2f3a4b5c6
Revises: p0d1e2f3a4b5
Create Date: 2026-08-08
"""

import sqlalchemy as sa

from alembic import op

revision = "q1e2f3a4b5c6"
down_revision = "p0d1e2f3a4b5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("runs", sa.Column("failure_reason", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("runs", "failure_reason")
