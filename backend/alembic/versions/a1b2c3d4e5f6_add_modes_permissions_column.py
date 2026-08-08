"""add modes permissions column

Revision ID: a1b2c3d4e5f6
Revises: 0e64fa6df16b
Create Date: 2026-08-01 06:10:00.000000

Adds the JSON ``permissions`` column to ``modes`` (plan §6 Phase 2 — modes as
data): the writable/repos scope for a lane spawned under the mode. Topology
stays code; persona/permissions/playbooks are data on the row. Dialect-neutral
batch ALTER so it applies on SQLite and Postgres.
"""
import sqlalchemy as sa

from alembic import op

revision = 'a1b2c3d4e5f6'
down_revision = '0e64fa6df16b'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('modes', schema=None) as batch_op:
        batch_op.add_column(sa.Column('permissions', sa.JSON(), nullable=False,
                                      server_default='{}'))


def downgrade() -> None:
    with op.batch_alter_table('modes', schema=None) as batch_op:
        batch_op.drop_column('permissions')
