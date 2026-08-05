"""Golden fetch service: keeps golden fresh — fetch every 5 min per
repo on origin/<integrationBranch>, plus one synchronous fetch before stamping.

Runs IN-PROCESS via APScheduler during the SQLite era (single-writer rule); splits
into its own compose service at the Postgres cutover. Skips archived repos.
Fetch failures surface as a golden-staleness signal — the PAT-expiry canary
(1-year PAT max lifetime discovered here, not mid-run).

Tier invariants: only this service ever WRITES to golden. Workers stamp clones
or bind-mount read-only.
"""

from __future__ import annotations

import subprocess
from datetime import datetime, timezone

from apscheduler.schedulers.background import BackgroundScheduler

from app.core.config import get_settings
from app.core.logging import get_logger
from app.db.base import get_session
from app.db.models.repo import Repo, RepoStatus

log = get_logger(service="fetcher")


def fetch_one(repo: Repo, golden_dir) -> tuple[bool, str]:
    """git fetch + record HEAD of origin/<integrationBranch>. Golden checkouts
    never move except through this function."""
    path = golden_dir / repo.name
    if not path.is_dir():
        return False, f"golden clone missing at {path}"
    fetch = subprocess.run(
        ["git", "-C", str(path), "fetch", "--quiet", "origin"],
        capture_output=True, text=True, timeout=300,
        env={"GIT_TERMINAL_PROMPT": "0", "ZAGENT_CREDENTIAL_SCOPE": "fetch", **_env()},
    )
    if fetch.returncode != 0:
        return False, fetch.stderr.strip()[:300]
    head = subprocess.run(
        ["git", "-C", str(path), "rev-parse", f"origin/{repo.integration_branch}"],
        capture_output=True, text=True, timeout=30,
    )
    if head.returncode != 0:
        return False, f"origin/{repo.integration_branch} unresolvable"
    # Keep the local checkout pinned at origin/<integrationBranch> so stamps are
    # same-fs hardlink clones of an always-latest tree.
    checkout = subprocess.run(
        ["git", "-C", str(path), "checkout", "--quiet", "-B", repo.integration_branch,
         f"origin/{repo.integration_branch}"],
        capture_output=True, text=True, timeout=120,
    )
    # L-25: the checkout's returncode was ignored — a broken checkout
    # (conflicts, missing ref) was reported as success. Fail closed.
    if checkout.returncode != 0:
        return False, f"checkout {repo.integration_branch} failed: {checkout.stderr.strip()[:200]}"
    return True, head.stdout.strip()


def _env() -> dict[str, str]:
    import os
    return dict(os.environ)


def fetch_all() -> dict[str, str]:
    settings = get_settings()
    session = get_session()
    results: dict[str, str] = {}
    try:
        repos = session.query(Repo).filter(Repo.status != RepoStatus.ARCHIVED).all()
        for repo in repos:
            ok, detail = fetch_one(repo, settings.golden_dir)
            if ok:
                repo.last_fetch_at = datetime.now(timezone.utc)
                repo.last_fetch_head = detail
                if repo.status == RepoStatus.ERROR:
                    repo.status = RepoStatus.READY
                    repo.status_detail = ""
                results[repo.name] = f"ok {detail[:8]}"
            else:
                repo.status_detail = detail  # staleness/PAT-expiry canary for the Inbox
                results[repo.name] = f"FAIL {detail[:80]}"
                log.warning("golden fetch failed", repo=repo.name, detail=detail[:200])
        session.commit()
    finally:
        session.close()
    return results


_scheduler: BackgroundScheduler | None = None


def start_fetch_loop() -> BackgroundScheduler:
    global _scheduler
    if _scheduler is not None:
        return _scheduler
    settings = get_settings()
    _scheduler = BackgroundScheduler()
    _scheduler.add_job(
        fetch_all, "interval", seconds=settings.fetch_interval_seconds,
        id="golden-fetch", max_instances=1, coalesce=True,
    )
    _scheduler.start()
    log.info("golden fetch loop started", interval_s=settings.fetch_interval_seconds)
    return _scheduler


def stop_fetch_loop() -> None:
    """H-45: shut the fetch scheduler down on app teardown. The old
    lifespan never stopped it, so the BackgroundScheduler kept firing
    fetch_all during shutdown — racing the workspace shred and writing
    to a half-torn-down DB."""
    global _scheduler
    if _scheduler is not None:
        try:
            _scheduler.shutdown(wait=False)
        except Exception as exc:  # noqa: BLE001 — already shutting down
            log.warning("fetch scheduler shutdown failed", error=str(exc)[:200])
        _scheduler = None
