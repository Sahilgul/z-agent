"""Repo-onboarding service (plan §1b/§7 — repos-as-data).

State machine: registered -> validating -> cloning -> indexing -> ready
(ready-no-map when the Phase 2 map generator hasn't covered the language yet).
ls-remote validation, dedupe by URL, progress as system events, repo_added WS
event invalidates the repo-list query — no refresh, no restart, ever.

New repos enter ONLY through this pipeline (tier invariants): cloned into golden,
checked out at origin/<integrationBranch>, adopted by the fetcher — NEVER cloned
into a task working directory.
"""

from __future__ import annotations

import asyncio
import base64
import subprocess
from datetime import datetime, timezone

from app.core.config import get_settings
from app.core.logging import get_logger
from app.db.base import get_session
from app.db.models.repo import Repo, RepoProfile, RepoStatus

log = get_logger(service="repo_onboarding")


class OnboardingError(RuntimeError):
    pass


def _set_status(repo_id: int, status: str, detail: str = "") -> None:
    session = get_session()
    try:
        repo = session.get(Repo, repo_id)
        if repo:
            repo.status = status
            repo.status_detail = detail
            session.commit()
    finally:
        session.close()


def _pat_auth_env(pat: str) -> dict[str, str]:
    """git has NO environment variable for http.extraHeader — the header is a
    config key only. GIT_CONFIG_COUNT/KEY/VALUE (git >= 2.31) injects it as
    config without putting the PAT in argv, where `ps` would show it."""
    auth = base64.b64encode(f":{pat}".encode()).decode()
    return {
        **_env(),
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.extraHeader",
        "GIT_CONFIG_VALUE_0": f"Authorization: Basic {auth}",
    }


def validate_remote(remote_url: str, pat: str) -> list[str]:
    """ls-remote validation — the branch list is FETCHED, never free-typed."""
    result = subprocess.run(
        ["git", "ls-remote", "--heads", remote_url],
        capture_output=True, text=True, timeout=60,
        env=_pat_auth_env(pat),
    )
    if result.returncode != 0:
        raise OnboardingError(f"remote unreachable or PAT lacks Code:Read — {result.stderr[:200]}")
    return [line.rsplit("/", 1)[-1] for line in result.stdout.splitlines()]


def register_repo(name: str, remote_url: str, integration_branch: str,
                  added_by: int | None) -> Repo:
    """Dedupe by URL: re-registering an existing remote returns the live row."""
    session = get_session()
    try:
        existing = session.query(Repo).filter(
            (Repo.remote_url == remote_url) | (Repo.name == name)
        ).one_or_none()
        if existing:
            if existing.status == RepoStatus.ARCHIVED:
                existing.status = RepoStatus.REGISTERED
                existing.archived_at = None
                session.commit()
            return existing
        repo = Repo(name=name, remote_url=remote_url, integration_branch=integration_branch,
                    added_by=added_by, status=RepoStatus.REGISTERED)
        session.add(repo)
        session.commit()
        session.refresh(repo)
        return repo
    finally:
        session.close()


async def onboard(repo_id: int, relay=None) -> None:
    """The pipeline itself. Runs as a background task; progress streams as
    system events; repo_added closes it out."""
    settings = get_settings()
    session = get_session()
    try:
        repo = session.get(Repo, repo_id)
        if repo is None:
            return
        name, url, branch = repo.name, repo.remote_url, repo.integration_branch
    finally:
        session.close()

    try:
        _set_status(repo_id, RepoStatus.VALIDATING)
        branches = await asyncio.to_thread(validate_remote, url, settings.fetch_pat)
        if branch not in branches:
            raise OnboardingError(
                f"origin/{branch} not on remote (has: {', '.join(branches[:12])})"
            )

        _set_status(repo_id, RepoStatus.CLONING)
        dest = settings.golden_dir / name
        if not dest.exists():
            settings.golden_dir.mkdir(parents=True, exist_ok=True)
            clone = await asyncio.to_thread(
                subprocess.run,
                ["git", "clone", "--quiet", url, str(dest)],
                capture_output=True, text=True,
                env=_pat_auth_env(settings.fetch_pat),
            )
            if clone.returncode != 0:
                raise OnboardingError(f"clone failed — {clone.stderr[:200]}")
        helper = str(settings.fleet_config_dir.parent / "scripts" / "git-credential-zagent")
        auth = base64.b64encode(f":{settings.fetch_pat}".encode()).decode()
        for args in (
            ["config", "credential.helper", ""],
            ["config", "credential.helper", f"!python3 {helper}"],
            ["config", "http.extraHeader", f"Authorization: Basic {auth}"],
            ["checkout", "--quiet", "-B", branch, f"origin/{branch}"],
        ):
            await asyncio.to_thread(subprocess.run, ["git", "-C", str(dest), *args])

        _set_status(repo_id, RepoStatus.INDEXING)
        # Map generator lands Phase 2; until then every onboarded repo is ready-no-map.
        session = get_session()
        try:
            row = session.get(Repo, repo_id)
            row.status = RepoStatus.READY_NO_MAP
            row.last_fetch_at = datetime.now(timezone.utc)
            head = subprocess.run(["git", "-C", str(dest), "rev-parse", "HEAD"],
                                  capture_output=True, text=True)
            row.last_fetch_head = head.stdout.strip() if head.returncode == 0 else None
            if session.query(RepoProfile).filter_by(repo_id=repo_id).one_or_none() is None:
                session.add(RepoProfile(repo_id=repo_id))
            session.commit()
        finally:
            session.close()
        if relay:
            await relay.publish_global({"type": "repo_added", "repo": name})
        log.info("repo onboarded", repo=name, branch=branch)
    except Exception as exc:
        _set_status(repo_id, RepoStatus.ERROR, str(exc)[:300])
        log.error("repo onboarding failed", repo=name, error=str(exc)[:200])


def archive_repo(repo_id: int) -> None:
    """Archive: fetcher stops, hidden from the scope picker, old sessions still
    replay, golden dir shredded (no archive path = accumulation forever)."""
    settings = get_settings()
    import shutil
    session = get_session()
    try:
        repo = session.get(Repo, repo_id)
        if repo is None:
            return
        repo.status = RepoStatus.ARCHIVED
        repo.archived_at = datetime.now(timezone.utc)
        name = repo.name
        session.commit()
    finally:
        session.close()
    golden = settings.golden_dir / name
    if golden.exists():
        shutil.rmtree(golden, ignore_errors=True)


def _env() -> dict[str, str]:
    import os
    return dict(os.environ)
