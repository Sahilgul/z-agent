"""PIN auth — FINAL identity model, no SSO ever.

username + 4-6 digit PIN, bcrypt hash, JWT in httpOnly cookie, token_version bump
kills sessions instantly (offboarding = deactivate, never delete). Brute-force:
5 failed PIN attempts -> 15-min lockout per user.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import bcrypt
import jwt
from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.base import db_session, get_session
from app.db.models.user import User

MAX_FAILED_ATTEMPTS = 5
LOCKOUT_MINUTES = 15
ALGORITHM = "HS256"


def hash_pin(pin: str) -> str:
    if not (pin.isdigit() and 4 <= len(pin) <= 6):
        raise ValueError("PIN must be 4-6 digits")
    return bcrypt.hashpw(pin.encode(), bcrypt.gensalt()).decode()


def verify_pin(pin: str, pin_hash: str) -> bool:
    try:
        return bcrypt.checkpw(pin.encode(), pin_hash.encode())
    except (ValueError, TypeError):
        # M-56: an invalid/non-bcrypt pin_hash (e.g. a corrupted row or a
        # legacy sentinel) used to raise ValueError("Invalid salt") out of
        # the login path as a 500. Treat a malformed hash as "no match" so
        # the caller returns a clean 401.
        return False


def issue_token(user: User) -> str:
    settings = get_settings()
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user.id),
        "username": user.username,
        "token_version": user.token_version,
        "iat": now,
        "exp": now + timedelta(seconds=settings.jwt_ttl_seconds),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=ALGORITHM)


def decode_token(token: str) -> dict:
    settings = get_settings()
    return jwt.decode(token, settings.jwt_secret, algorithms=[ALGORITHM])


def check_lockout(user: User) -> None:
    if user.locked_until and user.locked_until > datetime.now(timezone.utc):
        raise HTTPException(status_code=429, detail="locked out — try again later")


def record_failed_attempt(session: Session, user: User) -> None:
    user.failed_pin_attempts += 1
    if user.failed_pin_attempts >= MAX_FAILED_ATTEMPTS:
        user.locked_until = datetime.now(timezone.utc) + timedelta(minutes=LOCKOUT_MINUTES)
        # M-55: do NOT reset the counter on lockout. Resetting meant that
        # after each lockout expired the attacker got a FRESH full set of
        # attempts (staying at 4/window, never escalating). Leave the counter
        # high so the next failed attempt after a lockout immediately
        # re-locks — only a successful login (record_success) clears it.
    session.commit()


def record_success(session: Session, user: User) -> None:
    user.failed_pin_attempts = 0
    user.locked_until = None
    session.commit()


# ------------------------------------------------------------------ deps

def current_user(request: Request, session: Session = Depends(db_session)) -> User:
    """created_by scoping starts here: every session query hard-scopes by
    this user's id at the API layer, not just in the UI.

    L-24: use the request-scoped db_session (FastAPI caches it per request)
    instead of opening a second session via get_session(). Expunge the user
    before returning so downstream routes see a detached instance (matching
    the old behavior where current_user's session was closed in finally)."""
    token = request.cookies.get("zagent_token")
    if not token:
        raise HTTPException(status_code=401, detail="not authenticated")
    try:
        payload = decode_token(token)
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="invalid token") from None
    try:
        user = session.get(User, int(payload["sub"]))
        if user is None or user.status != "active":
            raise HTTPException(status_code=401, detail="account inactive")
        if user.token_version != payload.get("token_version"):
            raise HTTPException(status_code=401, detail="session revoked")
        # Detach so the route handler (which may use its own session) sees
        # the same detached instance the old code returned.
        session.expunge(user)
        return user
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=401, detail="invalid token") from None


def admin_user(user: User = Depends(current_user)) -> User:
    settings = get_settings()
    if user.username not in settings.admins and user.role != "admin":
        raise HTTPException(status_code=403, detail="admin only")
    return user
