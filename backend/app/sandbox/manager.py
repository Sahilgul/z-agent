"""Sandbox manager (plan §3/§8): Docker SDK — stamp, run, destroy, session volumes.

Stamping (DECIDED): WRITABLE lanes get a self-contained local clone from golden
(same-fs hardlinks — seconds; the lane owns its .git; shredding is rm -rf).
READ-ONLY context repos are read-only bind mounts of golden. Worktrees are
rejected for writable lanes (absolute .git pointer, shared object store writes,
prune lifecycle — three collisions with the mount rules).

Durable session volume (BUG-1 fix): ~/.claude mounts PER-LANE to
sessions/<run_id>/<lane_id>/ so resume/fork_session survive workspace shredding.
Retention 30d default; after expiry the run is replay-only.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import docker
from docker.errors import DockerException

from app.core.config import get_settings
from app.core.logging import get_logger
from app.db.models.lane import Lane
from app.db.models.repo import Repo
from app.db.models.run import Run

log = get_logger(service="sandbox")


class SandboxUnavailable(RuntimeError):
    pass


def _docker() -> docker.DockerClient:
    try:
        return docker.from_env()
    except DockerException as exc:
        raise SandboxUnavailable(str(exc)) from exc


def session_subpath(run_id: str, lane_id: str) -> Path:
    settings = get_settings()
    path = settings.sessions_dir / run_id / lane_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def stamp_clone(repo: Repo, run_id: str, lane_id: str) -> Path:
    """Synchronous final fetch (agent always starts on latest), then a
    self-contained clone stamp at origin/<integration_branch> (plan §3)."""
    settings = get_settings()
    golden_repo = settings.golden_dir / repo.name
    dest = settings.workspaces_dir / run_id / repo.name
    if dest.exists():
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "-C", str(golden_repo), "fetch", "--quiet", "origin"],
                   check=True, timeout=300, env={"GIT_TERMINAL_PROMPT": "0",
                                                 "ZAGENT_CREDENTIAL_SCOPE": "fetch",
                                                 **_env()})
    subprocess.run(["git", "clone", "--quiet", str(golden_repo), str(dest)], check=True, timeout=600)
    subprocess.run(
        ["git", "-C", str(dest), "checkout", "--quiet", "-B", f"lane/{lane_id[:8]}",
         f"origin/{repo.integration_branch}"],
        check=True, timeout=120,
    )
    return dest


def _env() -> dict[str, str]:
    import os
    return dict(os.environ)


def stamp_mcp_config(workspace: Path, repo: Repo) -> bool:
    """Playwright MCP wiring (plan §2/WU3): when the repo profile opts in
    (``repo.profile.extra["playwright_mcp"]`` — the UI-repo flag), write a
    .mcp.json into the stamped workspace so the lane's agent SDK picks up the
    playwright-mcp server at session start. The backend never RUNS a Playwright
    server — it only stamps the config. Returns True when the file was written.
    """
    try:
        extra = (repo.profile.extra if repo.profile else None) or {}
    except Exception:  # detached ORM instance without a loaded profile
        extra = {}
    if not extra.get("playwright_mcp"):
        return False
    config = {
        "mcpServers": {
            "playwright": {
                "command": "npx",
                "args": ["@playwright/mcp@latest"],
            }
        }
    }
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / ".mcp.json").write_text(json.dumps(config, indent=2), encoding="utf-8")
    log.info("playwright mcp config stamped", repo=repo.name, workspace=str(workspace))
    return True


class SandboxManager:
    def __init__(self) -> None:
        self.settings = get_settings()

    def lane_env(self, run: Run, lane: Lane, prompt: str, persona_prompt: str,
                 permission_mode: str, writable: bool) -> dict[str, str]:
        env = {
            "RUN_ID": run.id,
            "LANE_ID": lane.id,
            "TASK_PROMPT": prompt,
            "PERSONA_PROMPT": persona_prompt,
            "PERMISSION_MODE": permission_mode,
            "BUDGET_USD": str(lane.budget_usd),
            "REDIS_URL": self.settings.worker_redis_url,
            "ANTHROPIC_BASE_URL": self.settings.worker_gateway_url,
            "ANTHROPIC_AUTH_TOKEN": lane.gateway_key or "",
            "WORKSPACE_DIR": "/workspace",
        }
        if lane.session_id:
            env["RESUME_SESSION_ID"] = lane.session_id
        if self.settings.package_proxy_url:
            # Phase 2: lanes install deps through the allowlisting proxy only.
            proxy = self.settings.package_proxy_url
            env["HTTP_PROXY"] = proxy
            env["HTTPS_PROXY"] = proxy
            env["PIP_CACHE_DIR"] = "/cache/pip"
            env["npm_config_cache"] = "/cache/npm"
            env["NO_PROXY"] = "redis,gateway,localhost"
        if writable:
            # FLEET_PAT injected at container start (never baked into the image);
            # the credential helper keeps it out of remote URLs and .git/config.
            env["FLEET_PAT"] = self.settings.fleet_pat
            env["ZAGENT_CREDENTIAL_SCOPE"] = "fleet"
        return env

    def run_lane_container(self, run: Run, lane: Lane, prompt: str, persona_prompt: str,
                           permission_mode: str, writable_repo: Repo | None,
                           context_repos: list[Repo]) -> str:
        """Start the worker container. Phase 1 ladder: Ask = read-only golden
        mounts only; writable clone stamps arrive with Phase 2 coding lanes."""
        client = _docker()
        volumes: dict[str, dict] = {}

        session_path = session_subpath(run.id, lane.id)
        volumes[str(session_path)] = {"bind": "/root/.claude", "mode": "rw"}

        if self.settings.package_proxy_url:
            # Shared dependency caches — lanes never re-download the world.
            volumes[self.settings.pip_cache_volume] = {"bind": "/cache/pip", "mode": "rw"}
            volumes[self.settings.npm_cache_volume] = {"bind": "/cache/npm", "mode": "rw"}

        if writable_repo is not None:
            stamp = stamp_clone(writable_repo, run.id, lane.id)
            # UI repos (ClientApp) get the Playwright MCP config stamped into the
            # workspace — the agent SDK reads .mcp.json at session start.
            stamp_mcp_config(stamp, writable_repo)
            volumes[str(stamp)] = {"bind": f"/workspace/{writable_repo.name}", "mode": "rw"}
        for repo in context_repos:
            golden_repo = self.settings.golden_dir / repo.name
            if writable_repo is not None and repo.name == writable_repo.name:
                continue
            volumes[str(golden_repo)] = {"bind": f"/workspace/{repo.name}", "mode": "ro"}

        env = self.lane_env(run, lane, prompt, persona_prompt, permission_mode,
                            writable=writable_repo is not None)
        container = client.containers.run(
            self.settings.worker_image,
            environment=env,
            volumes=volumes,
            network=self.settings.worker_network,
            detach=True,
            name=f"zagent-lane-{lane.id[:8]}",
            remove=False,
        )
        log.info("lane container started", lane_id=lane.id, container=container.short_id)
        return container.id

    def stop_container(self, container_id: str) -> None:
        try:
            client = _docker()
            container = client.containers.get(container_id)
            container.stop(timeout=5)
            container.remove(force=True)
        except DockerException as exc:
            log.warning("container stop failed", container=container_id[:12], error=str(exc)[:200])

    def shred_workspace(self, run_id: str) -> None:
        """Workspaces are destroyed at run end; survivors are branches/PRs,
        events (DB), knowledge, and the session volume (plan §3)."""
        path = self.settings.workspaces_dir / run_id
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)

    def purge_expired_sessions(self, retention_days: int | None = None) -> int:
        """Two-step decay: 30d = replay-only (session volume purged); the events
        TTL job handles 12mo = deleted."""
        settings = get_settings()
        days = retention_days or settings.session_retention_days
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        purged = 0
        for run_dir in settings.sessions_dir.iterdir() if settings.sessions_dir.exists() else []:
            mtime = datetime.fromtimestamp(run_dir.stat().st_mtime, tz=timezone.utc)
            if mtime < cutoff:
                shutil.rmtree(run_dir, ignore_errors=True)
                purged += 1
        return purged


sandbox_manager = SandboxManager()
