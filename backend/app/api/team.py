"""Admin Team settings routes: add teammate -> one-time code (shown
ONCE), regenerate, deactivate, list pending. Admin stats are METADATA-ONLY
(run counts, cost, PRs merged — never message content).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.core.deps import admin_user
from app.db.base import get_session
from app.db.models.run import Run
from app.db.models.user import User
from app.services.team import add_teammate, deactivate_user, regenerate_code

try:
    from app.ado.client import IdentityResolutionError
except ImportError:  # ado client optional in some test envs
    class IdentityResolutionError(Exception):
        """Local fallback when the ado client isn't importable."""

log = logging.getLogger(__name__)

router = APIRouter(prefix="/team", tags=["team"])


class AddTeammateBody(BaseModel):
    username: str
    display_name: str = ""
    ado_email: str = ""


@router.get("/users")
def list_users(_: User = Depends(admin_user)):
    session = get_session()
    try:
        users = session.query(User).all()
        return [{
            "id": u.id, "username": u.username, "display_name": u.display_name,
            "role": u.role, "status": u.status,
            "ado_email": u.ado_email, "ado_bound": u.ado_descriptor is not None,
        } for u in users]
    finally:
        session.close()


@router.post("/users")
async def create_user(body: AddTeammateBody, _: User = Depends(admin_user)):
    try:
        user, code = await add_teammate(body.username, body.display_name, body.ado_email)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except IdentityResolutionError as exc:
        # M-28: identity resolution failed for the client-provided email — a
        # client-input issue, not a gateway-down issue. Surface 422 with a
        # generic message (don't leak internals).
        log.warning("identity resolution failed for %s", body.username)
        raise HTTPException(status_code=422, detail="could not resolve ADO identity") from exc
    except Exception as exc:
        # M-28: the broad except leaked internal error text (`{exc}`) to the
        # client. Log the full error server-side; return a generic 502.
        log.exception("identity binding failed for %s", body.username)
        raise HTTPException(status_code=502, detail="identity binding failed") from exc
    # The code is shown ONCE — send via Slack; it is never retrievable again.
    return {"id": user.id, "username": user.username, "setup_code": code,
            "ado_bound": user.ado_descriptor is not None}


@router.post("/users/{user_id}/regenerate-code")
def regenerate(user_id: int, _: User = Depends(admin_user)):
    try:
        code = regenerate_code(user_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"setup_code": code}


@router.post("/users/{user_id}/deactivate")
def deactivate(user_id: int, actor: User = Depends(admin_user)):
    # L-14: an admin could deactivate themselves (potentially locking out
    # the only admin) and a missing user raised ValueError -> 500. Guard
    # self-deactivation and surface not-found as 404.
    if actor.id == user_id:
        raise HTTPException(status_code=422, detail="cannot deactivate your own account")
    try:
        deactivate_user(user_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"ok": True}


@router.get("/stats")
def metadata_stats(_: User = Depends(admin_user)):
    """METADATA-ONLY: counts/costs — admin never reads content."""
    session = get_session()
    try:
        runs = session.query(Run).all()
        return {
            "total_runs": len(runs),
            "runs_by_stage": _count_by(runs, "stage"),
            "runs_by_mode": _count_by(runs, "mode"),
            "total_cost_usd": round(sum(r.cost_usd for r in runs), 4),
        }
    finally:
        session.close()


def _count_by(rows, attr):
    counts: dict[str, int] = {}
    for row in rows:
        key = getattr(row, attr) or "unknown"
        counts[key] = counts.get(key, 0) + 1
    return counts
