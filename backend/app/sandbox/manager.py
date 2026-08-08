"""Sandbox manager: Docker SDK — stamp, run, destroy, session volumes.

Stamping (DECIDED): WRITABLE threads get a self-contained local clone from golden
(same-fs hardlinks — seconds; the thread owns its .git; shredding is rm -rf).
READ-ONLY context repos are read-only bind mounts of golden. Worktrees are
rejected for writable threads (absolute .git pointer, shared object store writes,
prune lifecycle — three collisions with the mount rules).

Durable session volume: sessions/<run_id>/<thread_id>/ mounts PER-LANE at
/session (custom engine: checkpoint mirror + episodic DB) or /root/.claude
(legacy SDK runtime) so resume/fork_session survive workspace shredding.
Retention 30d default; after expiry the run is replay-only.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import docker
from docker.errors import DockerException, ImageNotFound

from app.core.config import get_settings
from app.core.logging import get_logger
from app.db.models.repo import Repo
from app.db.models.run import Run
from app.db.models.thread import Thread

log = get_logger(service="sandbox")


class SandboxUnavailable(RuntimeError):
    pass


def _docker() -> docker.DockerClient:
    try:
        return docker.from_env()
    except DockerException as exc:
        raise SandboxUnavailable(str(exc)) from exc


def session_subpath(run_id: str, thread_id: str) -> Path:
    settings = get_settings()
    path = settings.sessions_dir / run_id / thread_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def stamp_clone(repo: Repo, run_id: str, thread_id: str, fresh: bool = True) -> Path:
    """Synchronous final fetch (agent always starts on latest), then a
    self-contained clone stamp at origin/<integration_branch>.

    fresh=False is for a run's SECOND writable thread (goal-mode fix loop):
    the dest is keyed by run_id, so a re-stamp would rmtree the previous
    writable thread's implementation. Preserve it instead — the agent starts
    from the run's own work, which is the whole point of a fix round."""
    settings = get_settings()
    golden_repo = settings.golden_dir / repo.name
    dest = settings.workspaces_dir / run_id / repo.name
    if dest.exists():
        if not fresh:
            log.info("preserving existing run workspace", run_id=run_id, repo=repo.name)
            return dest
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "-C", str(golden_repo), "fetch", "--quiet", "origin"],
                   check=True, timeout=300, env={"GIT_TERMINAL_PROMPT": "0",
                                                 "COLLEGIUM_CREDENTIAL_SCOPE": "fetch",
                                                 **_env()})
    subprocess.run(["git", "clone", "--quiet", str(golden_repo), str(dest)], check=True, timeout=600)
    subprocess.run(
        ["git", "-C", str(dest), "checkout", "--quiet", "-B", f"thread/{thread_id[:8]}",
         f"origin/{repo.integration_branch}"],
        check=True, timeout=120,
    )
    return dest


def _env() -> dict[str, str]:
    import os
    return dict(os.environ)


def stamp_mcp_config(workspace: Path, repo: Repo) -> bool:
    """Playwright MCP wiring: when the repo profile opts in
    (``repo.profile.extra["playwright_mcp"]`` — the UI-repo flag), write a
    .mcp.json into the stamped workspace so the thread's agent SDK picks up the
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


def _engine_autonomy(permission_mode: str) -> str:
    """Map the CAS-style permission mode to the engine's autonomy enum."""
    return {
        "bypassPermissions": "autonomous",
        "acceptEdits": "gated",
    }.get(permission_mode, "supervised")


# The custom engine's Mode vocabulary (worker/worker/engine/state.py). Backend
# run.mode values must reconcile to this set BEFORE the container starts —
# passing an unknown mode verbatim used to crash the worker at Mode(...)
# with no diagnosable backend error (C3).
WORKER_MODES = frozenset({"ask", "plan", "development", "debug", "goal"})

# W-B3: the web exposes "agent-rnd" (the swarm mode) as a first-class mode,
# but the worker engine has no such Mode — every UI-started swarm failed at
# spawn with InvalidModeError. Swarm runs are goal-directed (the swarm
# blueprint still keys on run.mode), so threads boot as "goal".
_MODE_TO_WORKER_MODE = {"agent-rnd": "goal"}


class InvalidModeError(ValueError):
    pass


class SandboxManager:
    def __init__(self) -> None:
        self.settings = get_settings()

    def thread_env(self, run: Run, thread: Thread, prompt: str, persona_prompt: str,
                 permission_mode: str, writable: bool) -> dict[str, str]:
        # C3: per-run MODE after vocabulary reconciliation. The old code passed
        # engine_default_mode to every thread, so ask/plan/goal runs booted
        # with the development tool surface. Unknown modes fail loudly here.
        mode = (run.mode or self.settings.engine_default_mode).strip().lower()
        mode = _MODE_TO_WORKER_MODE.get(mode, mode)
        if mode not in WORKER_MODES:
            raise InvalidModeError(
                f"run mode {run.mode!r} is not in the worker engine vocabulary "
                f"{sorted(WORKER_MODES)} — reconcile the backend Mode row or the "
                "worker state.Mode enum before spawning")
        # The lane's model rides spawn_context (set at spawn, replayed on
        # kill/replace); absent on pre-selection rows → the deployment default.
        lane_model = (thread.spawn_context or {}).get("model") or self.settings.gateway_model
        # Per-model pricing for the worker's budget reminders: the registry
        # rate for the lane's model, falling back to the deployment-wide
        # default pair for anything the registry doesn't know.
        option = self.settings.model_option(lane_model)
        price_in = option.price_in_per_mtok if option else self.settings.worker_price_in_per_mtok
        price_out = option.price_out_per_mtok if option else self.settings.worker_price_out_per_mtok
        env = {
            "RUN_ID": run.id,
            "THREAD_ID": thread.id,
            "TASK_PROMPT": prompt,
            "PERSONA_PROMPT": persona_prompt,
            "PERMISSION_MODE": permission_mode,
            "BUDGET_USD": str(thread.budget_usd),
            "REDIS_URL": self.settings.worker_redis_url,
            "ANTHROPIC_BASE_URL": self.settings.worker_gateway_url,
            "ANTHROPIC_AUTH_TOKEN": thread.gateway_key or "",
            # Without this the SDK sends its own default Claude model name, which
            # the gateway does not publish and the thread key is not scoped to.
            "MODEL": lane_model,
            "WORKSPACE_DIR": "/workspace",
            # --- Custom engine ---
            "ENGINE": self.settings.engine_runtime,
            "MODE": mode,
            "AUTONOMY": _engine_autonomy(permission_mode),
            "LITELLM_BASE_URL": self.settings.worker_gateway_url,
            "LITELLM_API_KEY": thread.gateway_key or "",
            # C8: timeout clocks explicit — the backend's approval expiry and
            # the engine's BLPOP/idle watchdog must agree.
            "APPROVAL_TIMEOUT_S": str(self.settings.approval_timeout_seconds),
            "IDLE_TTL_SECONDS": str(self.settings.idle_ttl_seconds),
            # F4: budget-reminder pricing parity — the worker's local estimate
            # prices tokens with THESE rates so its 50%/80% reminders track
            # real gateway spend instead of a hardcoded guess. Per-lane: a
            # compare run mixes models whose rates differ by an order of
            # magnitude, so one global pair would mislead every reminder.
            "MODEL_PRICE_IN_PER_MTOK": str(price_in),
            "MODEL_PRICE_OUT_PER_MTOK": str(price_out),
        }
        # Composer reasoning choice for this lane's model ("off" or an effort
        # like "max"). Absent = provider default (thinking on) — the worker
        # sends no override and the request stays identical to pre-feature.
        reasoning = (thread.spawn_context or {}).get("reasoning")
        if reasoning:
            env["REASONING_EFFORT"] = reasoning
        # Vision lanes: attachments were staged into the session volume at
        # spawn (thread_manager) — the worker builds a multimodal first
        # message from them. Blind lanes never see this var; their prompt
        # already carries the Kimi pre-pass description instead.
        if (thread.spawn_context or {}).get("images"):
            lane_option = self.settings.model_option(lane_model)
            if lane_option and lane_option.vision:
                env["IMAGES_DIR"] = "/session/images"
        if self.settings.engine_canary:
            env["CANARY"] = "1"
        if self.settings.engine_database_url:
            env["DATABASE_URL"] = self.settings.engine_database_url
        if self.settings.engine_runtime == "custom":
            # B3: the checkpoint mirror + episodic DB live on the DURABLE
            # session volume (mounted at /session in run_thread_container) so
            # container removal/replacement never destroys engine state.
            env["CHECKPOINT_MIRROR_DIR"] = "/session"
            if thread.session_id:
                # B2: the engine resumes the SAME checkpoint namespace as the
                # thread it replaces (LangGraph checkpointer key).
                env["RESUME_CONTEXT_ID"] = thread.session_id
        elif thread.session_id:
            env["RESUME_SESSION_ID"] = thread.session_id
        # C6: env-size ceiling — oversized prompts must fail with a clear
        # error here, not a truncated container start.
        total = sum(len(k.encode()) + len(v.encode()) for k, v in env.items())
        if total > self.settings.max_env_payload_bytes:
            raise ValueError(
                f"thread env payload is {total} bytes, over the "
                f"{self.settings.max_env_payload_bytes}-byte container env ceiling "
                f"(prompt={len(prompt)} chars, persona_prompt={len(persona_prompt)} "
                "chars) — shorten the prompt or route the payload through the "
                "session volume")
        if self.settings.package_proxy_url:
            # Threads install deps through the allowlisting proxy only.
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
            env["COLLEGIUM_CREDENTIAL_SCOPE"] = "fleet"
        return env

    def run_thread_container(self, run: Run, thread: Thread, prompt: str, persona_prompt: str,
                             permission_mode: str, writable_repo: Repo | None,
                             context_repos: list[Repo],
                             resume_from_thread_id: str | None = None,
                             preserve_workspace: bool = False) -> str:
        """Start the worker container. Ask ladder: Ask = read-only golden
        mounts only; writable clone stamps arrive with coding threads.

        When resume_from_thread_id is set, the new thread mounts the PREVIOUS
        thread's session volume instead of a fresh one — the SDK's conversation
        state lives there, so the replacement picks up the thread instead of
        starting a stranger. The new thread's session_id is also set from the
        old one (handled in thread_manager.spawn) so RESUME_SESSION_ID is wired."""
        client = _docker()
        # Fail fast with a legible reason when the worker image isn't built
        # on this host: docker-py's containers.run would otherwise try to
        # PULL collegium-worker:<tag> — it isn't published, so the run died
        # on a raw registry 404 ("pull access denied") with no hint that the
        # fix is a local build.
        try:
            client.images.get(self.settings.worker_image)
        except ImageNotFound as exc:
            raise SandboxUnavailable(
                f"worker image {self.settings.worker_image!r} is not present on the "
                "Docker host — build it locally (the image is not published to a "
                "registry) or point COLLEGIUM_WORKER_IMAGE at a built tag") from exc
        volumes: dict[str, dict] = {}

        # Mount the prior thread's session directory when resuming; otherwise the
        # thread's own. session_subpath creates the dir if missing, so a fresh
        # thread still gets a clean volume.
        session_thread_id = resume_from_thread_id or thread.id
        session_path = session_subpath(run.id, session_thread_id)
        if self.settings.engine_runtime == "custom":
            # B3: the custom engine reads/writes ONLY /session
            # (CHECKPOINT_MIRROR_DIR, episodic DB). The /root/.claude mount was
            # an SDK leftover — the custom engine never reads it, so mounting
            # it pretended durability that didn't exist.
            volumes[str(session_path)] = {"bind": "/session", "mode": "rw"}
        else:
            volumes[str(session_path)] = {"bind": "/root/.claude", "mode": "rw"}

        if self.settings.package_proxy_url:
            # Shared dependency caches — threads never re-download the world.
            volumes[self.settings.pip_cache_volume] = {"bind": "/cache/pip", "mode": "rw"}
            volumes[self.settings.npm_cache_volume] = {"bind": "/cache/npm", "mode": "rw"}

        if writable_repo is not None:
            stamp = stamp_clone(writable_repo, run.id, thread.id,
                                fresh=not preserve_workspace)
            # UI repos (ClientApp) get the Playwright MCP config stamped into the
            # workspace — the agent SDK reads .mcp.json at session start.
            stamp_mcp_config(stamp, writable_repo)
            volumes[str(stamp)] = {"bind": f"/workspace/{writable_repo.name}", "mode": "rw"}
        for repo in context_repos:
            golden_repo = self.settings.golden_dir / repo.name
            if writable_repo is not None and repo.name == writable_repo.name:
                continue
            volumes[str(golden_repo)] = {"bind": f"/workspace/{repo.name}", "mode": "ro"}

        env = self.thread_env(run, thread, prompt, persona_prompt, permission_mode,
                            writable=writable_repo is not None)
        container = client.containers.run(
            self.settings.worker_image,
            environment=env,
            volumes=volumes,
            network=self.settings.worker_network,
            detach=True,
            # M-54: the container name used to be truncated to thread.id[:8]
            # (8 hex chars) — birthday-paradox collision at ~65k threads meant
            # Docker refused a duplicate name and thread spawn failed. Use
            # the full UUID (unique by construction) as the name suffix.
            name=f"collegium-thread-{thread.id}",
            remove=False,
        )
        log.info("thread container started", thread_id=thread.id, container=container.short_id)
        return container.id

    def stop_container(self, container_id: str) -> None:
        try:
            client = _docker()
            container = client.containers.get(container_id)
            container.stop(timeout=5)
            container.remove(force=True)
        except DockerException as exc:
            log.warning("container stop failed", container=container_id[:12], error=str(exc)[:200])

    def container_running(self, container_id: str) -> bool:
        """True if the container exists and is still running (H-37)."""
        try:
            client = _docker()
            container = client.containers.get(container_id)
            return container.status.lower() == "running"
        except DockerException:
            return False

    def wait_for_container_exit(self, container_id: str, timeout_s: float = 15.0) -> bool:
        """Poll until the container is gone or not running (H-37). Used by
        kill_replace_thread to guarantee the old container has released the
        session volume before the replacement mounts it — otherwise two
        containers write the same session volume (corruption).

        Returns True when the container is confirmed gone/stopped. On timeout
        it FORCE-STOPS the container and re-checks (A2): a wedged worker must
        fail the replace/resume, not wave it through to double-mount."""
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if not self.container_running(container_id):
                return True
            time.sleep(0.25)
        log.warning("container did not exit in time — force-stopping",
                   container=container_id[:12], timeout=timeout_s)
        self.stop_container(container_id)
        force_deadline = time.monotonic() + 10.0
        while time.monotonic() < force_deadline:
            if not self.container_running(container_id):
                return True
            time.sleep(0.25)
        log.error("container survived force-stop",
                  container=container_id[:12])
        return False

    def shred_workspace(self, run_id: str) -> None:
        """Workspaces are destroyed at run end; survivors are branches/PRs,
        events (DB), knowledge, and the session volume."""
        path = self.settings.workspaces_dir / run_id
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)

    def purge_expired_sessions(self, retention_days: int | None = None) -> int:
        """Two-step decay: 30d = replay-only (session volume purged); the events
        TTL job handles 12mo = deleted."""
        settings = get_settings()
        days = retention_days or settings.session_retention_days
        cutoff = datetime.now(UTC) - timedelta(days=days)
        purged = 0
        # M5: NEVER shred a session volume under a live container — the
        # retention sweep once ran concurrently with active threads, deleting
        # the volume a running worker was checkpointing into. Query for
        # threads that still own this session before touching the dir.
        from app.db.base import get_session
        from app.db.models.thread import Thread
        db = get_session()
        try:
            live_sessions = {
                row[0] for row in db.query(Thread.session_id).filter(
                    Thread.session_id.is_not(None),
                    Thread.status.notin_(
                        ("stopped", "completed", "failed", "replaced"))).all()
            }
        except Exception:
            # If the DB is unreachable, fail SAFE: purge nothing.
            log.warning("retention sweep skipped: live-session query failed",
                        exc_info=True)
            return 0
        finally:
            db.close()
        for run_dir in settings.sessions_dir.iterdir() if settings.sessions_dir.exists() else []:
            if run_dir.name in live_sessions:
                continue  # live thread owns this volume — hands off
            # M-72: use the NEWEST mtime among the dir and its contents.
            # A dir created long ago but written to today (file mtime fresh,
            # dir mtime stale because adding bytes to a file doesn't update
            # the parent dir's mtime on most filesystems) is ACTIVE and must
            # not be purged. The old dir-only check purged such active
            # sessions.
            mtimes = [datetime.fromtimestamp(run_dir.stat().st_mtime, tz=UTC)]
            for child in run_dir.iterdir():
                if child.is_file():
                    mtimes.append(datetime.fromtimestamp(child.stat().st_mtime, tz=UTC))
            mtime = max(mtimes)
            if mtime < cutoff:
                shutil.rmtree(run_dir, ignore_errors=True)
                purged += 1
        return purged


sandbox_manager = SandboxManager()
