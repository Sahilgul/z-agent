"""N3/N4: executable concurrency/durability races against REAL Postgres.

- Concurrent register_repo with the same remote URL -> exactly one identity
  (the partial unique index is the TOCTOU backstop).
- Concurrent create_run with the same idempotency_key -> exactly one run.
- Events double-insert at the same (run, thread, seq) -> unique constraint
  holds (N4 durability pin: crash-resume replay cannot duplicate an event).
"""

import threading

import pytest
import sqlalchemy as sa


def test_register_repo_race_one_identity(pg_session_factory):
    from app.services import repos

    url = "https://dev.azure.com/org/proj/_git/IntegrationRace"
    results, errors = [], []

    def go(name):
        try:
            results.append(repos.register_repo(name, url, "main", None))
        except Exception as exc:
            errors.append(exc)

    t1 = threading.Thread(target=go, args=("race-a",))
    t2 = threading.Thread(target=go, args=("race-b",))
    t1.start(); t2.start(); t1.join(); t2.join()

    # No 500s: both callers get A row (winner or the dedupe path).
    assert not errors, errors
    assert results[0].id == results[1].id
    session = pg_session_factory()
    try:
        from app.db.models.repo import Repo
        assert session.query(Repo).filter_by(remote_url=url).count() == 1
    finally:
        session.close()


def test_runs_idempotency_unique_index_backstop(pg_session_factory):
    """N3: the partial unique index uq_runs_owner_idem is what run_manager's
    idempotent create relies on — four concurrent inserts with the same
    (owner, key) must yield exactly ONE committed row."""
    import uuid

    from app.db.models.run import Run
    from app.db.models.user import User

    s = pg_session_factory()
    s.add(User(username="it-user"))
    s.commit()
    uid = s.query(User).filter_by(username="it-user").one().id
    s.close()

    committed = []

    def go():
        local = pg_session_factory()
        try:
            local.add(Run(id=str(uuid.uuid4()), created_by=uid, source="api",
                          mode="ask", title="race", idempotency_key="idem-1"))
            local.commit()
            committed.append(True)
        except sa.exc.IntegrityError:
            local.rollback()
        finally:
            local.close()

    threads = [threading.Thread(target=go) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(committed) == 1  # exactly one insert won
    check = pg_session_factory()
    try:
        assert check.query(Run).filter_by(
            created_by=uid, idempotency_key="idem-1").count() == 1
    finally:
        check.close()


def test_events_unique_constraint_blocks_double_insert(pg_engine):
    """N4: replaying a crashed turn's event batch must hit the unique
    constraint, never duplicate the durable record."""
    from app.db.models.event import Event
    from app.db.models.run import Run
    factory = sa.orm.sessionmaker(bind=pg_engine, expire_on_commit=False)

    s = factory()
    from app.db.models.user import User
    u = User(username="it-events")
    s.add(u)
    s.commit()
    s.add(Run(id="r-int", created_by=u.id, source="api", mode="ask",
              title="pin"))
    s.commit()
    s.add(Event(run_id="r-int", thread_id="t-int", seq=0, type="message",
                title="first", payload={}))
    s.commit()
    s.close()

    s2 = factory()
    s2.add(Event(run_id="r-int", thread_id="t-int", seq=0, type="message",
                 title="duplicate from replay", payload={}))
    with pytest.raises(sa.exc.IntegrityError):
        s2.commit()
    s2.rollback()
    assert s2.query(Event).filter_by(run_id="r-int").count() == 1
    s2.close()
