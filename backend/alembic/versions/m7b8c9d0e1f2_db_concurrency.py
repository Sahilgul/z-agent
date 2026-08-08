"""DB-backed concurrency: capacity_reservations + one-writer partial index.

Revision ID: m7b8c9d0e1f2
Revises: l7a8b9c0d1e2
Create Date: 2026-08-08 04:30:00.000000

H1/H2: reservation rows make capacity/repo-write checks serialize at the
database across backend replicas (the in-process set only guarded one
process). I4: uq_threads_writable_repo_active enforces one ACTIVE writable
thread per repo at the DB, so a cross-replica race cannot double-mount a
repo even if both app-level checks pass.
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "m7b8c9d0e1f2"
down_revision = "l7a8b9c0d1e2"
branch_labels = None
depends_on = None

_ACTIVE = "('queued','running','idle','interrupted','input_required')"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = set(inspector.get_table_names())
    if "capacity_reservations" not in existing:
        op.create_table(
            "capacity_reservations",
            sa.Column("token", sa.String(36), primary_key=True),
            sa.Column("repo_scope", sa.String(128), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False,
                      server_default=sa.func.now()),
        )
    dialect = bind.dialect.name
    where = sa.text(f"repo_scope IS NOT NULL AND status IN {_ACTIVE}")
    kw = {}
    if dialect == "sqlite":
        kw["sqlite_where"] = where
    elif dialect == "postgresql":
        kw["postgresql_where"] = where
    else:
        return
    indexes = {ix["name"] for ix in sa.inspect(bind).get_indexes("threads")}
    if "uq_threads_writable_repo_active" not in indexes:
        op.create_index("uq_threads_writable_repo_active", "threads",
                        ["repo_scope"], unique=True, **kw)
    rindexes = {ix["name"]
                for ix in sa.inspect(bind).get_indexes("capacity_reservations")}
    rwhere = sa.text("repo_scope IS NOT NULL")
    rkw = {"sqlite_where": rwhere} if dialect == "sqlite" else {"postgresql_where": rwhere}
    if "uq_reservation_repo" not in rindexes:
        op.create_index("uq_reservation_repo", "capacity_reservations",
                        ["repo_scope"], unique=True, **rkw)
    # D1: event idempotency at the DB. Dedupe any pre-existing duplicates
    # first (keep the earliest row) or the constraint creation fails.
    op.execute(sa.text(
        "DELETE FROM events WHERE id NOT IN ("
        "  SELECT MIN(id) FROM events GROUP BY run_id, thread_id, seq)"))
    uniques = {c["name"] for c in sa.inspect(bind).get_unique_constraints("events")}
    eindexes = {ix["name"] for ix in sa.inspect(bind).get_indexes("events")}
    if "uq_events_run_thread_seq" not in uniques | eindexes:
        # SQLite can't ALTER a constraint in — a unique INDEX enforces the
        # same invariant on both dialects (and matches the model's
        # __table_args__ UniqueConstraint for create_all test paths).
        if dialect == "sqlite":
            op.create_index("uq_events_run_thread_seq", "events",
                            ["run_id", "thread_id", "seq"], unique=True)
        else:
            op.create_unique_constraint("uq_events_run_thread_seq", "events",
                                        ["run_id", "thread_id", "seq"])


def downgrade() -> None:
    op.drop_constraint("uq_events_run_thread_seq", "events", type_="unique")
    op.drop_index("uq_threads_writable_repo_active", table_name="threads")
    op.drop_index("uq_reservation_repo", table_name="capacity_reservations")
    op.drop_table("capacity_reservations")
