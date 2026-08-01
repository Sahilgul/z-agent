"""Identity resolution service (plan §1b/§6): ADO descriptor -> users.id.
The §1b binding's second consumer (triggers engine); fail-closed by contract —
an unresolved descriptor returns None and the caller MUST NOT start a run.
Names are labels, never keys; the descriptor GUID is the identity key.
"""

from __future__ import annotations

from app.db.base import get_session
from app.db.models.user import User


def resolve_descriptor(descriptor: str | None) -> int | None:
    """users.id for an ADO descriptor, or None when unbound. Never guesses,
    never falls back — the two-Alis rule."""
    if not descriptor:
        return None
    session = get_session()
    try:
        user = (session.query(User)
                .filter_by(ado_descriptor=descriptor, status="active")
                .one_or_none())
        return user.id if user else None
    finally:
        session.close()


def system_user_id() -> int:
    """The only legitimate non-human owner: cron-owned runs (Janitor & co.),
    and only by explicit trigger-row config."""
    session = get_session()
    try:
        user = session.query(User).filter_by(username="system").one()
        return user.id
    finally:
        session.close()
