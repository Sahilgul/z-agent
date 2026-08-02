"""Repo registry routes (repos-as-data, Patch Bay rack). GET /repos feeds the
rack + the run-scope chip source; POST /repos starts onboarding (state machine +
repo_added WS event — no refresh, no restart); branches are FETCHED from the
remote, never free-typed; integrationBranch edits carry an audit note.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app.core.config import get_settings
from app.core.deps import current_user
from app.db.base import get_session
from app.db.models.repo import Repo, RepoStatus
from app.db.models.user import User
from app.services.repos import (
    OnboardingError, archive_repo, onboard, register_repo, validate_remote,
)

router = APIRouter(prefix="/repos", tags=["repos"])


class AddRepoBody(BaseModel):
    name: str
    integration_branch: str


class EditRepoBody(BaseModel):
    integration_branch: str | None = None
    audit_note: str = ""


def _serialize(repo: Repo) -> dict:
    return {
        "id": repo.id, "name": repo.name, "integration_branch": repo.integration_branch,
        "status": repo.status, "status_detail": repo.status_detail,
        "last_fetch_at": repo.last_fetch_at.isoformat() if repo.last_fetch_at else None,
        "last_fetch_head": repo.last_fetch_head,
    }


@router.get("")
def list_repos(user: User = Depends(current_user)):
    """The registry is SHARED team-wide metadata (like the knowledge corpus) —
    not privacy-scoped. Archived repos are hidden from the scope picker."""
    session = get_session()
    try:
        repos = session.query(Repo).filter(Repo.status != RepoStatus.ARCHIVED).all()
        return [_serialize(r) for r in repos]
    finally:
        session.close()


@router.get("/remote-branches")
def remote_branches(name: str, user: User = Depends(current_user)):
    """Branch list for the Add-Repo form — FETCHED from the remote."""
    settings = get_settings()
    url = f"https://dev.azure.com/{settings.ado_org}/{settings.ado_project}/_git/{name}"
    try:
        branches = validate_remote(url, settings.fetch_pat)
    except OnboardingError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"branches": branches}


@router.post("")
async def add_repo(body: AddRepoBody, request: Request, user: User = Depends(current_user)):
    settings = get_settings()
    url = f"https://dev.azure.com/{settings.ado_org}/{settings.ado_project}/_git/{body.name}"
    # Re-registering an ARCHIVED repo revives it (register_repo handles that); a
    # live one is a duplicate — refuse rather than silently re-clone into golden.
    session = get_session()
    try:
        dupe = session.query(Repo).filter(
            ((Repo.remote_url == url) | (Repo.name == body.name))
            & (Repo.status != RepoStatus.ARCHIVED)
        ).one_or_none()
        if dupe is not None:
            raise HTTPException(
                status_code=409,
                detail=f"{dupe.name} is already registered on {dupe.integration_branch}",
            )
    finally:
        session.close()
    repo = register_repo(body.name, url, body.integration_branch, added_by=user.id)
    relay = request.app.state.relay
    asyncio.create_task(onboard(repo.id, relay))
    return _serialize(repo)


@router.patch("/{repo_id}")
def edit_repo(repo_id: int, body: EditRepoBody, user: User = Depends(current_user)):
    """integrationBranch is editable (pg-main today, main after a cutover) with
    an audit note of who changed it."""
    session = get_session()
    try:
        repo = session.get(Repo, repo_id)
        if repo is None:
            raise HTTPException(status_code=404, detail="repo not found")
        if body.integration_branch:
            repo.integration_branch = body.integration_branch
            repo.status_detail = f"branch changed by {user.username}: {body.audit_note}"[:300]
        session.commit()
        return _serialize(repo)
    finally:
        session.close()


@router.post("/{repo_id}/archive")
def archive(repo_id: int, user: User = Depends(current_user)):
    archive_repo(repo_id)
    return {"ok": True}
