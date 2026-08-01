"""PWA push notifications (plan Phase 4): well-timed asks, never on landing.

The default sender uses pywebpush when installed (lands with the VM move);
without it, sends are skipped and logged — notify must NEVER fail the action
that triggered it. Tests inject a fake sender. Expired subscriptions (404/410)
are pruned on send.

Deep links point at the specific action card — a tap lands on the approval,
not the inbox.
"""

from __future__ import annotations

import json

from app.core.config import get_settings
from app.core.logging import get_logger
from app.db.base import get_session
from app.db.models.notification import Notification

log = get_logger(service="push")


class PushError(ValueError):
    pass


def save_subscription(user_id: int, endpoint: str, keys: dict) -> dict:
    if not endpoint.startswith("https://"):
        raise PushError("endpoint must be an https push URL")
    session = get_session()
    try:
        row = (session.query(Notification)
               .filter_by(user_id=user_id, endpoint=endpoint).one_or_none())
        if row is None:
            row = Notification(user_id=user_id, endpoint=endpoint, keys=dict(keys))
            session.add(row)
        else:
            row.keys = dict(keys)
        session.commit()
        session.refresh(row)
        return {"id": row.id, "endpoint": endpoint}
    finally:
        session.close()


def remove_subscription(user_id: int, endpoint: str) -> None:
    session = get_session()
    try:
        session.query(Notification).filter_by(user_id=user_id, endpoint=endpoint).delete()
        session.commit()
    finally:
        session.close()


def _default_sender(endpoint: str, keys: dict, payload: dict) -> str:
    """Returns 'sent' | 'skipped' | 'expired'. pywebpush arrives with the VM
    image; until then every send is a logged skip (never a crash)."""
    try:
        from pywebpush import WebPushException, webpush  # type: ignore
    except ImportError:
        log.info("push skipped (pywebpush not installed)", endpoint=endpoint[:40])
        return "skipped"
    settings = get_settings()
    try:
        webpush(subscription_info={"endpoint": endpoint, "keys": keys},
                data=json.dumps(payload),
                vapid_private_key=settings.vapid_private_key,
                vapid_claims={"sub": settings.vapid_subject})
        return "sent"
    except WebPushException as exc:  # noqa: BLE001 — library's own error type
        status = getattr(getattr(exc, "response", None), "status_code", 0)
        return "expired" if status in (404, 410) else "skipped"


def send_to_user(user_id: int, title: str, body: str, url: str = "/", sender=None) -> dict:
    """Fan out to every subscription the user has; prune dead ones. Returns a
    per-outcome tally — callers log it, never branch on it."""
    send = sender or _default_sender
    session = get_session()
    try:
        subs = [(s.id, s.endpoint, dict(s.keys or {}))
                for s in session.query(Notification).filter_by(user_id=user_id).all()]
    finally:
        session.close()
    payload = {"title": title, "body": body[:180], "url": url}
    tally = {"sent": 0, "skipped": 0, "expired": 0}
    for sub_id, endpoint, keys in subs:
        outcome = send(endpoint, keys, payload)
        tally[outcome] = tally.get(outcome, 0) + 1
        if outcome == "expired":
            session = get_session()
            try:
                row = session.get(Notification, sub_id)
                if row:
                    session.delete(row)
                    session.commit()
            finally:
                session.close()
    return tally


def approval_deep_link(run_id: str, approval_id: str) -> str:
    return f"/?screen=approvals&run={run_id}&card={approval_id}"
