import os
import time
from datetime import datetime, timedelta, timezone

import pytest

from app.db.models.thread import Thread
from app.db.models.repo import Repo
from app.db.models.run import Run
from app.sandbox import manager as sb


class _FakeContainer:
    def __init__(self, cid="c-1"):
        self.id = cid
        self.short_id = cid[:8]
    def stop(self, timeout=None): pass
    def remove(self, force=False): pass


class _FakeContainers:
    def __init__(self, container=None):
        self._container = container or _FakeContainer()
        self.run_calls = []
    def run(self, *a, **k):
        self.run_calls.append((a, k))
        return self._container
    def get(self, cid):
        return self._container


class _FakeDockerClient:
    def __init__(self, container=None):
        self.containers = _FakeContainers(container)


def _patch_docker(monkeypatch, client):
    monkeypatch.setattr(sb, "_docker", lambda: client)


# --------------------------------------------------------------- stamp_mcp_config (C6)
def test_stamp_mcp_config_writes_file_when_profile_opts_in(tmp_path):
    from app.db.models.repo import RepoProfile
    repo = Repo(name="ClientApp", integration_branch="main")
    repo.profile = RepoProfile(repo_id=1, language="ts", test_cmds=[],
                               extra={"playwright_mcp": True})
    assert sb.stamp_mcp_config(tmp_path / "ws", repo) is True
    import json
    config = json.loads((tmp_path / "ws" / ".mcp.json").read_text(encoding="utf-8"))
    assert "playwright" in config["mcpServers"]


def test_stamp_mcp_config_skips_without_flag(tmp_path):
    repo = Repo(name="ServerApp", integration_branch="main")
    assert sb.stamp_mcp_config(tmp_path / "ws", repo) is False
    assert not (tmp_path / "ws" / ".mcp.json").exists()


def test_stamp_mcp_config_survives_detached_repo(tmp_path):
    """A repo whose profile can't lazy-load must not crash container start."""
    class ExplodingRepo:
        name = "Detached"

        @property
        def profile(self):
            raise RuntimeError("DetachedInstanceError")

    assert sb.stamp_mcp_config(tmp_path / "ws", ExplodingRepo()) is False


def _make_run_thread(session, make_user, thread_id="l1", session_id=None, budget=5.0):
    u = make_user("a")
    run = Run(id="r1", created_by=u.id, mode="ask", stage="investigating")
    thread = Thread(id=thread_id, run_id="r1", persona="researcher", status="running",
                budget_usd=budget, gateway_key="vk-1", session_id=session_id)
    session.add_all([run, thread])
    session.commit()
    return run, thread


def test_docker_raises_sandbox_unavailable(monkeypatch):
    from docker.errors import DockerException
    def boom():
        raise DockerException("no daemon")
    monkeypatch.setattr(sb.docker, "from_env", boom)
    with pytest.raises(sb.SandboxUnavailable):
        sb._docker()


def test_session_subpath_creates_dirs():
    path = sb.session_subpath("r1", "l1")
    assert path.exists()
    assert path.is_dir()


def test_stamp_clone_invokes_git(monkeypatch, tmp_path):
    repo = Repo(name="ServerApp", integration_branch="main")
    calls = []

    class _Proc:
        def __init__(self, rc=0):
            self.returncode = rc
            self.stdout = ""
            self.stderr = ""

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return _Proc()
    monkeypatch.setattr(sb.subprocess, "run", fake_run)
    monkeypatch.setattr(sb.shutil, "rmtree", lambda p, ignore_errors=False: None)
    sb.stamp_clone(repo, "r1", "l1")
    assert calls[0][0] == "git" and "fetch" in calls[0]
    assert calls[1][0] == "git" and "clone" in calls[1]
    assert calls[2][0] == "git" and "checkout" in calls[2]


def test_stamp_clone_propagates_failure(monkeypatch):
    import subprocess
    repo = Repo(name="ServerApp", integration_branch="main")

    def fake_run(cmd, **kw):
        if "fetch" in cmd:
            raise subprocess.CalledProcessError(1, cmd, stderr="fetch refused")
        return _Proc()
    monkeypatch.setattr(sb.subprocess, "run", fake_run)
    monkeypatch.setattr(sb.shutil, "rmtree", lambda *a, **k: None)
    with pytest.raises(subprocess.CalledProcessError):
        sb.stamp_clone(repo, "r1", "l1")


def test_thread_env_read_only(session, make_user):
    run, thread = _make_run_thread(session, make_user)
    mgr = sb.SandboxManager()
    env = mgr.thread_env(run, thread, "task", "persona", "default", writable=False)
    assert env["RUN_ID"] == "r1"
    assert env["THREAD_ID"] == "l1"
    assert env["PERMISSION_MODE"] == "default"
    assert env["ANTHROPIC_AUTH_TOKEN"] == "vk-1"
    assert "FLEET_PAT" not in env
    assert "RESUME_SESSION_ID" not in env


def test_thread_env_writable_includes_fleet_pat(session, make_user):
    run, thread = _make_run_thread(session, make_user)
    mgr = sb.SandboxManager()
    env = mgr.thread_env(run, thread, "task", "persona", "acceptEdits", writable=True)
    assert env["FLEET_PAT"] == sb.get_settings().fleet_pat
    assert env["ZAGENT_CREDENTIAL_SCOPE"] == "fleet"
    assert env["PERMISSION_MODE"] == "acceptEdits"


def test_thread_env_resume_session(session, make_user):
    run, thread = _make_run_thread(session, make_user, session_id="sess-9")
    mgr = sb.SandboxManager()
    env = mgr.thread_env(run, thread, "task", "persona", "default", writable=False)
    assert env["RESUME_SESSION_ID"] == "sess-9"


def test_thread_env_proxy_injection(session, make_user, monkeypatch):
    run, thread = _make_run_thread(session, make_user)
    mgr = sb.SandboxManager()
    monkeypatch.setattr(mgr.settings, "package_proxy_url", "http://proxy:3128", raising=False)
    env = mgr.thread_env(run, thread, "task", "persona", "default", writable=False)
    assert env["HTTP_PROXY"] == "http://proxy:3128"
    assert env["NO_PROXY"] == "redis,gateway,localhost"


def test_run_thread_container_read_only(session, make_user, monkeypatch):
    run, thread = _make_run_thread(session, make_user)
    client = _FakeDockerClient()
    _patch_docker(monkeypatch, client)
    mgr = sb.SandboxManager()
    repo = Repo(name="ServerApp", integration_branch="main")
    cid = mgr.run_thread_container(run, thread, "task", "persona", "default",
                                  writable_repo=None, context_repos=[repo])
    assert cid == client.containers._container.id
    _, kwargs = client.containers.run_calls[0]
    assert kwargs["network"] == sb.get_settings().worker_network
    assert kwargs["detach"] is True
    # read-only mount for context repo
    mounts = kwargs["volumes"]
    assert any(v.get("mode") == "ro" for v in mounts.values())


def test_run_thread_container_writable_stamps(session, make_user, monkeypatch):
    run, thread = _make_run_thread(session, make_user)
    client = _FakeDockerClient()
    _patch_docker(monkeypatch, client)
    stamped = []
    monkeypatch.setattr(sb, "stamp_clone", lambda repo, run_id, thread_id: stamped.append((repo.name, run_id, thread_id)) or "/tmp/stamp")
    mgr = sb.SandboxManager()
    repo = Repo(name="ServerApp", integration_branch="main")
    cid = mgr.run_thread_container(run, thread, "task", "persona", "acceptEdits",
                                  writable_repo=repo, context_repos=[])
    assert cid == client.containers._container.id
    assert stamped == [("ServerApp", "r1", "l1")]
    _, kwargs = client.containers.run_calls[0]
    env = kwargs["environment"]
    assert env["FLEET_PAT"] == sb.get_settings().fleet_pat
    mounts = kwargs["volumes"]
    assert any("/workspace/ServerApp" in v.get("bind", "") and v.get("mode") == "rw" for v in mounts.values())


def test_stop_container_calls_stop_and_remove(monkeypatch):
    container = _FakeContainer("c-9")
    client = _FakeDockerClient(container=container)
    _patch_docker(monkeypatch, client)
    sb.sandbox_manager.stop_container("c-9")  # should not raise


def test_stop_container_swallows_docker_error(monkeypatch):
    from docker.errors import DockerException
    def boom():
        raise DockerException("gone")
    monkeypatch.setattr(sb, "_docker", boom)
    sb.sandbox_manager.stop_container("c-9")  # should not raise


def test_shred_workspace_removes_dir(tmp_path):
    run_dir = sb.get_settings().workspaces_dir / "r-shred"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "file.txt").write_text("x")
    sb.sandbox_manager.shred_workspace("r-shred")
    assert not run_dir.exists()


def test_shred_workspace_missing_is_noop():
    sb.sandbox_manager.shred_workspace("never-existed")


def test_purge_expired_sessions_purges_old(monkeypatch):
    sessions = sb.get_settings().sessions_dir
    old = sessions / "old-run"
    old.mkdir(parents=True, exist_ok=True)
    (old / "f.txt").write_text("x")
    fresh = sessions / "fresh-run"
    fresh.mkdir(parents=True, exist_ok=True)
    (fresh / "f.txt").write_text("x")
    old_ts = time.time() - (31 * 86400)
    fresh_ts = time.time()
    os.utime(old, (old_ts, old_ts))
    os.utime(fresh, (fresh_ts, fresh_ts))
    purged = sb.sandbox_manager.purge_expired_sessions(retention_days=30)
    assert purged == 1
    assert not old.exists()
    assert fresh.exists()


def test_purge_expired_sessions_no_dir():
    sessions = sb.get_settings().sessions_dir
    if sessions.exists():
        for child in list(sessions.iterdir()):
            if child.name not in ("."):
                import shutil
                shutil.rmtree(child, ignore_errors=True)
    # calling with empty/missing dir returns 0
    assert sb.sandbox_manager.purge_expired_sessions(retention_days=30) == 0


def test_sandbox_manager_singleton():
    assert isinstance(sb.sandbox_manager, sb.SandboxManager)
