"""PIN auth (plan §1b — FINAL identity model, no SSO ever).

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
from app.db.base import get_session
from app.db.models.user import User

MAX_FAILED_ATTEMPTS = 5
LOCKOUT_MINUTES = 15
ALGORITHM = "HS256"


def hash_pin(pin: str) -> str:
    if not (pin.isdigit() and 4 <= len(pin) <= 6):
        raise ValueError("PIN must be 4-6 digits")
    return bcrypt.hashpw(pin.encode(), bcrypt.gensalt()).decode()


def verify_pin(pin: str, pin_hash: str) -> bool:
    return bcrypt.checkpw(pin.encode(), pin_hash.encode())


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
        user.failed_pin_attempts = 0
    session.commit()


def record_success(session: Session, user: User) -> None:
    user.failed_pin_attempts = 0
    user.locked_until = None
    session.commit()


# ------------------------------------------------------------------ deps

def current_user(request: Request) -> User:
    """created_by scoping starts here (§7a): every session query hard-scopes by
    this user's id at the API layer, not just in the UI."""
    token = request.cookies.get("zagent_token")
    if not token:
        raise HTTPException(status_code=401, detail="not authenticated")
    try:
        payload = decode_token(token)
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="invalid token") from None
    session = get_session()
    try:
        user = session.get(User, int(payload["sub"]))
        if user is None or user.status != "active":
            raise HTTPException(status_code=401, detail="account inactive")
        if user.token_version != payload.get("token_version"):
            raise HTTPException(status_code=401, detail="session revoked")
        return user
    finally:
        session.close()


def admin_user(user: User = Depends(current_user)) -> User:
    settings = get_settings()
    if user.username not in settings.admins and user.role != "admin":
        raise HTTPException(status_code=403, detail="admin only")
    return user
