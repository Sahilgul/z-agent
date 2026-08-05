"""Seed the repos registry from fleet-config/repos.json.

For each fleet-config repo: resolve integrationBranch AGAINST THE REMOTE (branch
list fetched, never free-typed), then upsert the `repos` row. The DB row is the
LIVE registry after this seed; repos.json is the bootstrap seed only.

Run (backend cwd, env: ZAGENT_DB_URL, FETCH_PAT, ZAGENT_ADO_ORG/PROJECT):
  python ../scripts/seed_repos.py
"""

from __future__ import annotations

import asyncio
import base64
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "fleet-config"))

from loader import load_repos  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.db.base import get_session  # noqa: E402
from app.db.models.repo import Repo, RepoProfile, RepoStatus  # noqa: E402

LANGUAGE_BY_NAME = {
    "ServerApp": "typescript", "ClientApp": "typescript", "LLMQuality": "python",
    "AIClusterApp": "python", "ClinicalAIServices": "python", "Billing-Engine": "python",
    "PromptFlowApp": "python", "LivekitAgents": "python", "AIMedVision": "python",
    "KnowledgeBase": "docs",
}


def remote_branch_exists(remote_url: str, branch: str, pat: str) -> tuple[bool, list[str]]:
    """Resolve integrationBranch against the REMOTE via ls-remote (no clone
    needed). Returns (exists, all_branches)."""
    auth = base64.b64encode(f":{pat}".encode()).decode()
    result = subprocess.run(
        ["git", "ls-remote", "--heads", remote_url],
        capture_output=True, text=True, timeout=60,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_HTTP_EXTRA_HEADER": f"Authorization: Basic {auth}"},
    )
    if result.returncode != 0:
        return False, []
    branches = [line.rsplit("/", 1)[-1] for line in result.stdout.splitlines()]
    return branch in branches, branches


async def seed() -> None:
    settings = get_settings()
    repos = load_repos(settings.fleet_config_dir)
    session = get_session()
    seeded, skipped = 0, 0
    for spec in repos:
        remote_url = (
            f"https://dev.azure.com/{settings.ado_org}/{settings.ado_project}"
            f"/_git/{spec.name}"
        )
        exists = True
        branches: list[str] = []
        if settings.fetch_pat:
            exists, branches = remote_branch_exists(remote_url, spec.integration_branch, settings.fetch_pat)
            if not exists:
                print(f"[seed] WARNING {spec.name}: origin/{spec.integration_branch} NOT on remote "
                      f"(remote has: {', '.join(branches[:12])}{'...' if len(branches) > 12 else ''})")
        row = session.query(Repo).filter_by(name=spec.name).one_or_none()
        if row is None:
            row = Repo(name=spec.name)
            session.add(row)
        row.remote_url = remote_url
        row.integration_branch = spec.integration_branch
        row.status = RepoStatus.REGISTERED if exists else RepoStatus.ERROR
        row.status_detail = "" if exists else f"origin/{spec.integration_branch} missing on remote"
        session.flush()
        if session.query(RepoProfile).filter_by(repo_id=row.id).one_or_none() is None:
            session.add(RepoProfile(
                repo_id=row.id,
                language=LANGUAGE_BY_NAME.get(spec.name, ""),
                extra={"stack": spec.stack, "notes": spec.notes},
            ))
        seeded += 1
        _ = skipped
    session.commit()
    print(f"[seed] {seeded} repos registered from fleet-config")


if __name__ == "__main__":
    asyncio.run(seed())
