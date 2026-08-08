"""add lanes.spawn_context (kill_replace respawn + session resume)

The original prompt/persona_prompt a lane was spawned with, so kill_replace can
respawn the same work without asking the blueprint for it again.

Revision ID: b7e4c2f19a03
Revises: a1b2c3d4e5f6
Create Date: 2026-08-01
"""
import sqlalchemy as sa

from alembic import op

revision = "b7e4c2f19a03"
down_revision = "a1b2c3d4e5f6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("lanes", sa.Column("spawn_context", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("lanes", "spawn_context")
