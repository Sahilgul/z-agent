import hashlib
from datetime import UTC, datetime, timedelta

import pytest

from app.ado.client import IdentityResolutionError
from app.db.models.user import SetupCode
from app.services import team

CODE_TTL = team.CODE_TTL_HOURS


async def test_add_teammate_issues_code_and_dedupes(session, make_user, monkeypatch):
    async def fake_resolve(self, email):
        from app.ado.client import AdoIdentity
        return AdoIdentity(descriptor="desc-1", display_name="X", mail=email)
    monkeypatch.setattr("app.ado.client.AdoClient.resolve_identity", fake_resolve)
    user, code = await team.add_teammate("newbie", "Newbie", "n@x.com")
    assert user.username == "newbie"
    assert user.role == "member"
    assert user.status == "pending"
    assert user.ado_descriptor == "desc-1"
    assert code.isdigit() and len(code) == 8
    rows = session.query(SetupCode).filter_by(user_id=user.id).all()
    assert len(rows) == 1
    assert rows[0].code_hash == hashlib.sha256(code.encode()).hexdigest()
    with pytest.raises(ValueError):
        await team.add_teammate("newbie", "X", "n@x.com")


async def test_add_teammate_admin_role(session, make_user, monkeypatch):
    async def fake_resolve(self, email):
        from app.ado.client import AdoIdentity
        return AdoIdentity(descriptor="d", display_name="X", mail=email)
    monkeypatch.setattr("app.ado.client.AdoClient.resolve_identity", fake_resolve)
    user, code = await team.add_teammate("sahil", "Sahil", "s@x.com")
    assert user.role == "admin"


async def test_add_teammate_ado_unreachable_provisions_unbound(session, make_user, monkeypatch):
    async def boom(self, email):
        raise RuntimeError("network down")
    monkeypatch.setattr("app.ado.client.AdoClient.resolve_identity", boom)
    user, code = await team.add_teammate("unbound", "U", "u@x.com")
    assert user.ado_descriptor is None
    assert code.isdigit()


async def test_add_teammate_identity_resolution_error_propagates(session, make_user, monkeypatch):
    async def boom(self, email):
        raise IdentityResolutionError("no identity")
    monkeypatch.setattr("app.ado.client.AdoClient.resolve_identity", boom)
    with pytest.raises(IdentityResolutionError):
        await team.add_teammate("noado", "N", "n@x.com")


async def test_add_teammate_skips_ado_when_no_org(monkeypatch, session, make_user):
    from app.core.config import get_settings
    monkeypatch.setattr(get_settings(), "ado_org", "")
    called = {"n": 0}

    class FakeAdo:
        def __init__(self, *a, **k):
            called["n"] += 1  # L-30: prove ADO is never constructed
    monkeypatch.setattr("app.services.team.AdoClient", FakeAdo)
    # L-30: pass a NON-empty email so the `if ado_email and settings.ado_org`
    # guard actually evaluates the (empty) ado_org branch. With an empty email
    # the guard short-circuits on ado_email and the no-org path is never
    # exercised — the test would pass vacuously.
    user, code = await team.add_teammate("noorg", "N", "n@x.com")
    assert user.ado_descriptor is None
    assert code.isdigit()
    assert called["n"] == 0


def test_regenerate_code_invalidates_old(session, make_user):
    u = make_user("regen")
    c1 = team.regenerate_code(u.id)
    rows = session.query(SetupCode).filter_by(user_id=u.id).order_by(SetupCode.id).all()
    assert len(rows) == 1
    assert rows[0].invalidated_at is None
    assert rows[0].code_hash == hashlib.sha256(c1.encode()).hexdigest()
    c2 = team.regenerate_code(u.id)
    assert c1 != c2
    session.expire_all()
    rows = session.query(SetupCode).filter_by(user_id=u.id).order_by(SetupCode.id).all()
    assert rows[0].invalidated_at is not None
    assert rows[1].invalidated_at is None
    assert rows[1].code_hash == hashlib.sha256(c2.encode()).hexdigest()
    fresh = session.query(SetupCode).filter_by(user_id=u.id, invalidated_at=None).all()
    assert len(fresh) == 1


def test_regenerate_code_unknown_user(session):
    with pytest.raises(ValueError):
        team.regenerate_code(999999)


def test_deactivate_user_bumps_token_version(session, make_user):
    u = make_user("deact", status="active", token_version=0)
    team.deactivate_user(u.id)
    session.refresh(u)
    assert u.status == "deactivated"
    assert u.token_version == 1


def test_deactivate_user_unknown(session):
    with pytest.raises(ValueError):
        team.deactivate_user(999999)


async def test_redeem_setup_code_success(session, make_user, monkeypatch):
    async def fake_resolve(self, email):
        from app.ado.client import AdoIdentity
        return AdoIdentity(descriptor="d", display_name="X", mail=email)
    monkeypatch.setattr("app.ado.client.AdoClient.resolve_identity", fake_resolve)
    user, code = await team.add_teammate("redeem", "R", "r@x.com")
    from app.core.security import hash_pin
    out = team.redeem_setup_code("redeem", code, hash_pin("4321"))
    assert out.status == "active"
    assert out.pin_hash is not None
    row = session.query(SetupCode).filter_by(user_id=user.id).one()
    assert row.used_at is not None


async def test_redeem_setup_code_wrong_code(session, make_user, monkeypatch):
    async def fake_resolve(self, email):
        from app.ado.client import AdoIdentity
        return AdoIdentity(descriptor="d", display_name="X", mail=email)
    monkeypatch.setattr("app.ado.client.AdoClient.resolve_identity", fake_resolve)
    await team.add_teammate("redeem2", "R", "r@x.com")
    from app.core.security import hash_pin
    with pytest.raises(ValueError):
        team.redeem_setup_code("redeem2", "00000000", hash_pin("4321"))


async def test_redeem_setup_code_already_used(session, make_user, monkeypatch):
    async def fake_resolve(self, email):
        from app.ado.client import AdoIdentity
        return AdoIdentity(descriptor="d", display_name="X", mail=email)
    monkeypatch.setattr("app.ado.client.AdoClient.resolve_identity", fake_resolve)
    user, code = await team.add_teammate("redeem3", "R", "r@x.com")
    from app.core.security import hash_pin
    team.redeem_setup_code("redeem3", code, hash_pin("4321"))
    with pytest.raises(ValueError):
        team.redeem_setup_code("redeem3", code, hash_pin("4321"))


async def test_redeem_setup_code_expired(session, make_user, monkeypatch):
    async def fake_resolve(self, email):
        from app.ado.client import AdoIdentity
        return AdoIdentity(descriptor="d", display_name="X", mail=email)
    monkeypatch.setattr("app.ado.client.AdoClient.resolve_identity", fake_resolve)
    user, code = await team.add_teammate("redeem4", "R", "r@x.com")
    row = session.query(SetupCode).filter_by(user_id=user.id).one()
    row.expires_at = datetime.now(UTC) - timedelta(hours=1)
    session.commit()
    from app.core.security import hash_pin
    with pytest.raises(ValueError):
        team.redeem_setup_code("redeem4", code, hash_pin("4321"))


def test_redeem_setup_code_unknown_username(session):
    from app.core.security import hash_pin
    with pytest.raises(ValueError):
        team.redeem_setup_code("ghost", "00000000", hash_pin("4321"))


async def test_redeem_setup_code_invalidated(session, make_user, monkeypatch):
    async def fake_resolve(self, email):
        from app.ado.client import AdoIdentity
        return AdoIdentity(descriptor="d", display_name="X", mail=email)
    monkeypatch.setattr("app.ado.client.AdoClient.resolve_identity", fake_resolve)
    user, c1 = await team.add_teammate("redeem5", "R", "r@x.com")
    c2 = team.regenerate_code(user.id)
    from app.core.security import hash_pin
    with pytest.raises(ValueError):
        team.redeem_setup_code("redeem5", c1, hash_pin("4321"))
    team.redeem_setup_code("redeem5", c2, hash_pin("4321"))


def test_hash_code_format():
    assert team._hash_code("123") == hashlib.sha256(b"123").hexdigest()
