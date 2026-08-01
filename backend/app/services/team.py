"""Team provisioning (plan §1b — one-time setup codes, no self-service).

Admin Team settings UI: 'Add teammate' = username + display name + ADO email
(the email resolves the ADO descriptor AT CREATION — identity binding happens
here, fail-loud on 0 or 2+ matches) -> one-time setup code shown ONCE.
Regenerate invalidates the old code. Offboarding = DEACTIVATE + token_version
bump; never delete. The add_user CLI survives ONLY to bootstrap the first admin.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from app.ado.client import AdoClient, IdentityResolutionError
from app.core.config import get_settings
from app.db.base import get_session
from app.db.models.user import SetupCode, User

CODE_TTL_HOURS = 72


def _hash_code(code: str) -> str:
    return hashlib.sha256(code.encode()).hexdigest()


async def add_teammate(username: str, display_name: str, ado_email: str) -> tuple[User, str]:
    """Returns (user, ONE-TIME CODE) — the code is shown once with a copy button."""
    settings = get_settings()
    descriptor = None
    if ado_email and settings.ado_org:
        client = AdoClient(pat=settings.fetch_pat)
        try:
            identity = await client.resolve_identity(ado_email)
            descriptor = identity.descriptor
        except IdentityResolutionError:
            raise
        except Exception:
            # ADO unreachable: provision unbound, bind on first BYO-PAT/refresh.
            descriptor = None

    session = get_session()
    try:
        if session.query(User).filter_by(username=username).one_or_none():
            raise ValueError(f"username '{username}' taken")
        role = "admin" if username in settings.admins else "member"
        user = User(username=username, display_name=display_name or username,
                    ado_email=ado_email or None, ado_descriptor=descriptor,
                    role=role, status="pending")
        session.add(user)
        session.commit()
        session.refresh(user)
        code = _issue_code(session, user)
        return user, code
    finally:
        session.close()


def _issue_code(session, user: User) -> str:
    for old in session.query(SetupCode).filter_by(user_id=user.id, used_at=None):
        old.invalidated_at = datetime.now(timezone.utc)
    code = f"{secrets.randbelow(10**8):08d}"
    session.add(SetupCode(
        user_id=user.id, code_hash=_hash_code(code),
        expires_at=datetime.now(timezone.utc) + timedelta(hours=CODE_TTL_HOURS),
    ))
    session.commit()
    return code


def regenerate_code(user_id: int) -> str:
    session = get_session()
    try:
        user = session.get(User, user_id)
        if user is None:
            raise ValueError("user not found")
        return _issue_code(session, user)
    finally:
        session.close()


def deactivate_user(user_id: int) -> None:
    """Offboarding: DEACTIVATE, never delete — token_version bump kills sessions
    instantly; their Ideas comments, knowledge approvals, and runs stay attributed."""
    session = get_session()
    try:
        user = session.get(User, user_id)
        if user is None:
            raise ValueError("user not found")
        user.status = "deactivated"
        user.token_version += 1
        session.commit()
    finally:
        session.close()


def redeem_setup_code(username: str, code: str, new_pin_hash: str) -> User:
    """First login: username + one-time code -> FORCED PIN choice (the owner
    never sees/chooses anyone's PIN)."""
    session = get_session()
    try:
        user = session.query(User).filter_by(username=username).one_or_none()
        if user is None:
            raise ValueError("unknown username")
        now = datetime.now(timezone.utc)
        row = (
            session.query(SetupCode)
            .filter_by(user_id=user.id, used_at=None, code_hash=_hash_code(code))
            .one_or_none()
        )
        if row is None or row.invalidated_at is not None or row.expires_at < now:
            raise ValueError("invalid or expired setup code")
        row.used_at = now
        user.pin_hash = new_pin_hash
        user.status = "active"
        session.commit()
        return user
    finally:
        session.close()
