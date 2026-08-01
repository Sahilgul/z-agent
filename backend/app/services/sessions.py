"""Session browser services (plan §7a): replay hydration, resume, edit-and-resend.

Replay = hydrate the SAME EventStream from the events table in read-only mode —
normalization at the edge means replay and live are one code path. Resume
CONTINUES the same run row (re-stamp + mount the lane's session subpath);
edit-and-resend FORKS sessions within the run (forked_from_session_id) so the
history stays one continuous timeline. Explicit caveat: resume restores the
CONVERSATION; un-pushed file changes are lost by design (workspace shredding).
"""

from __future__ import annotations

from app.db.base import get_session
from app.db.models.event import Event
from app.db.models.lane import Lane
from app.db.models.run import Run
from app.sandbox.manager import session_subpath


def replay_events(run_id: str, user_id: int, lane_id: str | None = None,
                  after_seq: int | None = None, limit: int = 500) -> list[dict]:
    """Ordered by per-lane seq (§1b ordering rule). Hard-scoped to the owner."""
    session = get_session()
    try:
        run = session.get(Run, run_id)
        if run is None or run.created_by != user_id:
            return []
        query = session.query(Event).filter_by(run_id=run_id)
        if lane_id:
            query = query.filter_by(lane_id=lane_id)
        if after_seq is not None:
            query = query.filter(Event.seq > after_seq)
        rows = query.order_by(Event.lane_id, Event.seq).limit(limit).all()
        return [{
            "run_id": r.run_id, "lane_id": r.lane_id, "seq": r.seq, "ts": r.ts.isoformat(),
            "kind": r.type, "title": r.title, "detail": r.payload,
            "sdk_message_uuid": r.sdk_message_uuid,
        } for r in rows]
    finally:
        session.close()


def session_volume_exists(run_id: str, lane_id: str) -> bool:
    """Resume button appears only if the session volume still exists (30d TTL:
    after expiry the run is replay-only)."""
    return session_subpath(run_id, lane_id).exists() and any(session_subpath(run_id, lane_id).iterdir())


def fork_point_before_last_user_message(run_id: str, lane_id: str) -> str | None:
    """Find the sdk_message_uuid to slice at: the event just BEFORE the last user
    message. fork treats a missing UUID as 'cannot fork before this event'
    (replay handles null forever)."""
    session = get_session()
    try:
        rows = (
            session.query(Event)
            .filter_by(run_id=run_id, lane_id=lane_id, type="message")
            .order_by(Event.seq.desc())
            .limit(2)
            .all()
        )
        if len(rows) < 2:
            return None
        return rows[1].sdk_message_uuid
    finally:
        session.close()
