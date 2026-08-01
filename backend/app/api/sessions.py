"""Session browser routes (plan §7a): replay + resume. Replay hydrates the SAME
EventStream in read-only mode; Resume appears only while the session volume
exists (30d TTL; after expiry the run is replay-only).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from app.core.deps import current_user
from app.db.base import get_session
from app.db.models.lane import Lane
from app.db.models.user import User
from app.services.intents import load_run_for_user
from app.services.sessions import replay_events, session_volume_exists

router = APIRouter(prefix="/sessions", tags=["sessions"])


@router.get("/{run_id}/replay")
def replay(run_id: str, user: User = Depends(current_user)):
    if load_run_for_user(run_id, user.id) is None:
        raise HTTPException(status_code=404, detail="session not found")
    return {"run_id": run_id, "events": replay_events(run_id, user.id, limit=5000)}


@router.get("/{run_id}/resumable")
def resumable(run_id: str, user: User = Depends(current_user)):
    run = load_run_for_user(run_id, user.id)
    if run is None:
        raise HTTPException(status_code=404, detail="session not found")
    session = get_session()
    try:
        lanes = session.query(Lane).filter_by(run_id=run_id).all()
        return {
            "run_id": run_id,
            "lanes": [{
                "lane_id": l.id, "persona": l.persona,
                "resumable": l.session_id is not None and session_volume_exists(run_id, l.id),
            } for l in lanes],
        }
    finally:
        session.close()


@router.post("/{run_id}/resume")
async def resume(run_id: str, request: Request, user: User = Depends(current_user)):
    """Resume CONTINUES the same run row (new stage transition): re-stamp from
    golden, mount the lane's session subpath, resume with its session_id."""
    run = load_run_for_user(run_id, user.id)
    if run is None:
        raise HTTPException(status_code=404, detail="session not found")
    # Phase 1: resume restarts the blueprint with lane session_ids intact — the
    # worker picks RESUME_SESSION_ID up from its env (sandbox/manager.lane_env).
    run_manager = request.app.state.run_manager
    new_run = await run_manager.create_run(
        source="button", initiated_by=user.id, mode_name=run.mode,
        task=run.title, repo=run.repo, work_item_id=run.work_item_id,
        autonomy=run.autonomy,
    )
    return {"run_id": new_run.id, "continues": run_id}
