"""POST /runs idempotency: a retried create with the same key returns the
original run instead of minting a duplicate that double-spends budget."""

import pytest
from sqlalchemy.exc import IntegrityError

from app.db.models.run import Run
from tests.test_orchestrator_run_manager import _make_manager, _seed_mode


async def _noop(*a, **k):
    pass


async def test_same_key_returns_original_run(session, make_user, monkeypatch):
    u = make_user("a")
    _seed_mode(session)
    rm, ingest, _, _ = _make_manager()
    monkeypatch.setattr(rm, "_execute", _noop)
    first = await rm.create_run(source="button", initiated_by=u.id,
                                mode_name="ask", task="do the thing",
                                idempotency_key="k-1")
    second = await rm.create_run(source="button", initiated_by=u.id,
                                 mode_name="ask", task="do the thing",
                                 idempotency_key="k-1")
    assert second.id == first.id
    assert session.query(Run).filter_by(idempotency_key="k-1").count() == 1
    # Only ONE execution was registered (no duplicate side effects).
    assert ingest.registered == [first.id]


async def test_different_key_or_user_creates_new_run(session, make_user, monkeypatch):
    a = make_user("a")
    b = make_user("b")
    _seed_mode(session)
    rm, _, _, _ = _make_manager()
    monkeypatch.setattr(rm, "_execute", _noop)
    r1 = await rm.create_run(source="button", initiated_by=a.id,
                             mode_name="ask", task="t", idempotency_key="k-1")
    r2 = await rm.create_run(source="button", initiated_by=a.id,
                             mode_name="ask", task="t", idempotency_key="k-2")
    r3 = await rm.create_run(source="button", initiated_by=b.id,
                             mode_name="ask", task="t", idempotency_key="k-1")
    r4 = await rm.create_run(source="button", initiated_by=a.id,
                             mode_name="ask", task="t")  # no key: no dedupe
    r5 = await rm.create_run(source="button", initiated_by=a.id,
                             mode_name="ask", task="t")
    assert len({r1.id, r2.id, r3.id, r4.id, r5.id}) == 5


async def test_unique_index_backstop(session, make_user):
    """The partial unique index guarantees exactly one row even if the
    pre-check is raced (two concurrent commits with the same key); NULL keys
    never participate."""
    u = make_user("a")
    session.add(Run(id="r-a", created_by=u.id, mode="ask",
                    idempotency_key="race"))
    session.commit()
    session.add(Run(id="r-b", created_by=u.id, mode="ask",
                    idempotency_key="race"))
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()
    session.add_all([Run(id="r-c", created_by=u.id, mode="ask"),
                     Run(id="r-d", created_by=u.id, mode="ask")])
    session.commit()
