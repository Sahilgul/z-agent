"""Session browser services: replay hydration, resume, edit-and-resend.

Replay = hydrate the SAME EventStream from the events table in read-only mode —
normalization at the edge means replay and live are one code path. Resume
CONTINUES the same run row (re-stamp + mount the thread's session subpath);
edit-and-resend FORKS sessions within the run (forked_from_session_id) so the
history stays one continuous timeline. Explicit caveat: resume restores the
CONVERSATION; un-pushed file changes are lost by design (workspace shredding).
"""

from __future__ import annotations

from datetime import UTC

from app.db.base import get_session
from app.db.models.event import Event
from app.db.models.run import Run
from app.sandbox.manager import session_subpath


def _ts_key(ts) -> float:
    # SQLite/Postgres round-trips can return naive datetimes while the default
    # is aware utcnow — normalize before comparison or a mixed set crashes min().
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return ts.timestamp()


def _chronological_merge(rows: list[Event]) -> list[Event]:
    """Merged console view: per-thread seq is the canonical order WITHIN a
    thread, but SQL (thread_id, seq) ordering renders every thread's whole
    history as one UUID-sorted block — a run with thread generations (mode
    switch, mention expansion, kill-replace) or multiple lanes replays with
    later messages before earlier ones. K-way merge the seq-ordered lanes by
    ts so replay matches the live WS arrival order; within a lane, seq stays
    authoritative (worker clock skew never reorders a thread against itself)."""
    lanes: dict[str, list[Event]] = {}
    for r in rows:  # rows arrive (thread_id, seq)-ordered
        lanes.setdefault(r.thread_id, []).append(r)
    heads = {tid: 0 for tid in lanes}
    out: list[Event] = []
    remaining = len(rows)
    while remaining:
        pick = min(
            (tid for tid, h in heads.items() if h < len(lanes[tid])),
            key=lambda tid: _ts_key(lanes[tid][heads[tid]].ts),
        )
        out.append(lanes[pick][heads[pick]])
        heads[pick] += 1
        remaining -= 1
    return out


def replay_events(run_id: str, user_id: int, thread_id: str | None = None,
                  after_seq: int | None = None, limit: int = 500) -> list[dict]:
    """Per-thread seq order within a lane; the merged view interleaves lanes
    chronologically. Hard-scoped to the owner."""
    session = get_session()
    try:
        run = session.get(Run, run_id)
        if run is None or run.created_by != user_id:
            return []
        query = session.query(Event).filter_by(run_id=run_id)
        if thread_id:
            query = query.filter_by(thread_id=thread_id)
        if after_seq is not None:
            query = query.filter(Event.seq > after_seq)
        rows = query.order_by(Event.thread_id, Event.seq).limit(limit).all()
        if not thread_id:
            rows = _chronological_merge(rows)
        return [{
            "run_id": r.run_id, "thread_id": r.thread_id, "seq": r.seq, "ts": r.ts.isoformat(),
            "kind": r.type, "title": r.title, "detail": r.payload,
            "sdk_message_uuid": r.sdk_message_uuid,
        } for r in rows]
    finally:
        session.close()


def session_volume_exists(run_id: str, thread_id: str) -> bool:
    """Resume button appears only if the session volume still exists (30d TTL:
    after expiry the run is replay-only)."""
    return session_subpath(run_id, thread_id).exists() and any(session_subpath(run_id, thread_id).iterdir())


def fork_point_before_last_user_message(run_id: str, thread_id: str) -> str | None:
    """Find the sdk_message_uuid to slice at: the event just BEFORE the last user
    message. fork treats a missing UUID as 'cannot fork before this event'
    (replay handles null forever)."""
    session = get_session()
    try:
        rows = (
            session.query(Event)
            .filter_by(run_id=run_id, thread_id=thread_id, type="message")
            .order_by(Event.seq.desc())
            .limit(2)
            .all()
        )
        if len(rows) < 2:
            return None
        return rows[1].sdk_message_uuid
    finally:
        session.close()
