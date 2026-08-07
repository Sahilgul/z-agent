import jwt
import pytest
from datetime import datetime, timedelta, timezone

from app.core import security
from app.core.config import get_settings
from app.db.models.user import User


def test_hash_pin_policy_rejects_bad_pins():
    with pytest.raises(ValueError):
        security.hash_pin("12")
    with pytest.raises(ValueError):
        security.hash_pin("1234567")
    with pytest.raises(ValueError):
        security.hash_pin("abcd")
    with pytest.raises(ValueError):
        security.hash_pin("")


def test_hash_pin_and_verify_roundtrip():
    h = security.hash_pin("1234")
    assert h != "1234"
    assert security.verify_pin("1234", h)
    assert not security.verify_pin("0000", h)


def test_issue_and_decode_token_roundtrip(make_user):
    u = make_user("tok", token_version=3)
    token = security.issue_token(u)
    payload = security.decode_token(token)
    assert payload["sub"] == str(u.id)
    assert payload["username"] == "tok"
    assert payload["token_version"] == 3
    assert payload["exp"] > payload["iat"]


def test_decode_token_rejects_bad_signature(make_user):
    u = make_user("tok2")
    token = security.issue_token(u)
    bad = token + "x"
    with pytest.raises(jwt.PyJWTError):
        security.decode_token(bad)


def test_check_lockout_passes_when_unlocked(session, make_user):
    u = make_user("unlock")
    u.locked_until = None
    session.commit()
    security.check_lockout(u)
    # L-34: the past-time assignment was set on the in-memory object but
    # never committed, so the DB path (a persisted, expired lockout that
    # check_lockout must read as unlocked) was unexercised. Commit the
    # past time, expire the session, and re-fetch so check_lockout reads
    # the value that round-tripped through the DB.
    u.locked_until = datetime.now(timezone.utc) - timedelta(minutes=1)
    session.commit()
    session.expire_all()
    reloaded = session.get(User, u.id)
    security.check_lockout(reloaded)


def test_check_lockout_raises_when_locked(make_user, session):
    u = make_user("locked", status="active")
    u.locked_until = datetime.now(timezone.utc) + timedelta(minutes=10)
    session.commit()
    with pytest.raises(Exception) as exc:
        security.check_lockout(u)
    assert exc.value.status_code == 429


def test_record_failed_attempt_lockout_threshold(session, make_user):
    u = make_user("brute", status="active")
    for _ in range(security.MAX_FAILED_ATTEMPTS - 1):
        security.record_failed_attempt(session, u)
    assert u.failed_pin_attempts == security.MAX_FAILED_ATTEMPTS - 1
    assert u.locked_until is None
    security.record_failed_attempt(session, u)
    # M-55: the counter no longer resets on lockout — it stays at MAX so the
    # next failed attempt (after the lockout expires) immediately re-locks.
    # Only a successful login clears the counter (test_record_success_resets).
    assert u.failed_pin_attempts == security.MAX_FAILED_ATTEMPTS
    assert u.locked_until is not None
    # M-69: verify the lockout duration is LOCKOUT_MINUTES (15), not just
    # "not None" — a regression that changed the window would otherwise pass.
    expected = datetime.now(timezone.utc) + timedelta(minutes=security.LOCKOUT_MINUTES)
    delta = abs((u.locked_until - expected).total_seconds())
    assert delta < 5, f"lockout duration off by {delta}s (expected {security.LOCKOUT_MINUTES}m)"


def test_record_success_resets(session, make_user):
    u = make_user("succ", status="active")
    u.failed_pin_attempts = 3
    u.locked_until = datetime.now(timezone.utc) + timedelta(minutes=5)
    session.commit()
    security.record_success(session, u)
    assert u.failed_pin_attempts == 0
    assert u.locked_until is None


def _req(token: str | None):
    from fastapi import Request
    headers = [(b"cookie", f"collegium_token={token}".encode())] if token else []
    return Request({"type": "http", "headers": headers, "app": {}})


def test_current_user_no_cookie():
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        security.current_user(_req(None))
    assert exc.value.status_code == 401


def test_current_user_invalid_token(session, make_user):
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        security.current_user(_req("garbage"))
    assert exc.value.status_code == 401


def test_current_user_revoked_session(session, make_user):
    u = make_user("rev", token_version=2)
    token = security.issue_token(u)
    u.token_version = 99
    session.commit()
    with pytest.raises(Exception) as exc:
        security.current_user(_req(token), session=session)
    assert exc.value.status_code == 401
    assert "revoked" in exc.value.detail


def test_current_user_inactive(session, make_user):
    u = make_user("inact", status="pending")
    token = security.issue_token(u)
    with pytest.raises(Exception) as exc:
        security.current_user(_req(token), session=session)
    assert exc.value.status_code == 401


def test_current_user_happy(session, make_user):
    u = make_user("happy", status="active")
    token = security.issue_token(u)
    out = security.current_user(_req(token), session=session)
    assert out.id == u.id


def test_admin_user_allows_admin(session, make_user):
    u = make_user("sahil", role="admin", status="active")
    token = security.issue_token(u)
    user = security.current_user(_req(token), session=session)
    assert security.admin_user(user=user).id == u.id


def test_admin_user_rejects_member(session, make_user):
    from fastapi import HTTPException
    u = make_user("norm", role="member", status="active")
    token = security.issue_token(u)
    user = security.current_user(_req(token), session=session)
    with pytest.raises(HTTPException) as exc:
        security.admin_user(user=user)
    assert exc.value.status_code == 403


def test_admin_user_allows_member_with_admin_role(session, make_user):
    u = make_user("root", role="admin", status="active")
    assert security.admin_user(user=u).id == u.id


def test_get_settings_admins_property():
    s = get_settings()
    assert isinstance(s.admins, set)
    assert "sahil" in s.admins
