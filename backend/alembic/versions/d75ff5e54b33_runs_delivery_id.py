"""runs_delivery_id

Revision ID: d75ff5e54b33
Revises: b7e4c2f19a03
Create Date: 2026-08-01 14:53:36.007214

"""
import sqlalchemy as sa

from alembic import op

revision = 'd75ff5e54b33'
down_revision = 'b7e4c2f19a03'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("runs") as batch:
        batch.add_column(sa.Column("delivery_id", sa.Integer(), nullable=True))
        batch.create_foreign_key("fk_runs_delivery", "deliveries", ["delivery_id"], ["id"])


def downgrade() -> None:
    with op.batch_alter_table("runs") as batch:
        batch.drop_constraint("fk_runs_delivery", type_="foreignkey")
        batch.drop_column("delivery_id")
