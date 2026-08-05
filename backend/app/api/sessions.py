"""Session browser routes: replay + resume. Replay hydrates the SAME
EventStream in read-only mode; Resume appears only while the session volume
exists (30d TTL; after expiry the run is replay-only).
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

from app.core.deps import current_user
from app.db.base import get_session
from app.db.models.thread import Thread
from app.db.models.user import User
from app.services import transcript
from app.services.intents import load_run_for_user
from app.services.sessions import replay_events, session_volume_exists

router = APIRouter(prefix="/sessions", tags=["sessions"])


@router.get("/{run_id}/replay")
def replay(run_id: str, user: User = Depends(current_user)):
    if load_run_for_user(run_id, user.id) is None:
        raise HTTPException(status_code=404, detail="session not found")
    return {"run_id": run_id, "events": replay_events(run_id, user.id, limit=5000)}


@router.get("/{run_id}/transcript")
def transcript_jsonl(run_id: str, after_seq: int | None = None,
                     user: User = Depends(current_user)):
    """The run's flat JSONL transcript — one event per line, ingest order.

    Streams from disk so a long session never buffers in memory. Falls back to
    the events table when no file exists (runs that predate the transcript
    writer, or a transcript purged ahead of its rows)."""
    if load_run_for_user(run_id, user.id) is None:
        raise HTTPException(status_code=404, detail="session not found")

    def lines():
        wrote = False
        for record in transcript.read(run_id, after_seq=after_seq):
            wrote = True
            yield json.dumps(record, ensure_ascii=False, default=str) + "\n"
        if not wrote:
            for record in replay_events(run_id, user.id, after_seq=after_seq, limit=50_000):
                yield json.dumps(record, ensure_ascii=False, default=str) + "\n"

    return StreamingResponse(
        lines(),
        media_type="application/x-ndjson",
        headers={"Content-Disposition": f'attachment; filename="{run_id}.jsonl"'},
    )


@router.get("/{run_id}/resumable")
def resumable(run_id: str, user: User = Depends(current_user)):
    run = load_run_for_user(run_id, user.id)
    if run is None:
        raise HTTPException(status_code=404, detail="session not found")
    session = get_session()
    try:
        threads = session.query(Thread).filter_by(run_id=run_id).all()
        return {
            "run_id": run_id,
            "threads": [{
                "thread_id": l.id, "persona": l.persona,
                "resumable": l.session_id is not None and session_volume_exists(run_id, l.id),
            } for l in threads],
        }
    finally:
        session.close()


@router.post("/{run_id}/resume")
async def resume(run_id: str, request: Request, user: User = Depends(current_user)):
    """Resume CONTINUES the same run row (new stage transition): re-stamp from
    golden, mount the thread's session subpath, resume with its session_id."""
    run = load_run_for_user(run_id, user.id)
    if run is None:
        raise HTTPException(status_code=404, detail="session not found")
    # H-22: resume the SAME run row so the worker inherits the prior session
    # (resume_from_thread_id -> inherited session_id + mounted session
    # volume). The old code called create_run — a fresh run with no link to
    # the prior session, so every resume started a stranger.
    run_manager = request.app.state.run_manager
    resumed = await run_manager.resume_run(run_id, user.id)
    if resumed is None:
        raise HTTPException(status_code=404, detail="session not found")
    return {"run_id": run_id, "continues": run_id}
