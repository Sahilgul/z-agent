import time

import pytest

from app.db.models.repo import Repo, RepoStatus
from app.sandbox import fetcher


class _Proc:
    def __init__(self, rc=0, stdout="", stderr=""):
        self.returncode = rc
        self.stdout = stdout
        self.stderr = stderr


def test_fetch_one_missing_golden(tmp_path):
    repo = Repo(name="Ghost", integration_branch="main")
    ok, detail = fetcher.fetch_one(repo, tmp_path)
    assert ok is False
    assert "golden clone missing" in detail


def test_fetch_one_success(tmp_path, monkeypatch):
    repo = Repo(name="ServerApp", integration_branch="main")
    golden = tmp_path / "ServerApp"
    golden.mkdir()
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        if "fetch" in cmd:
            return _Proc(0)
        if "rev-parse" in cmd:
            return _Proc(0, stdout="deadbeef\n")
        if "checkout" in cmd:
            return _Proc(0)
        return _Proc(0)
    monkeypatch.setattr(fetcher.subprocess, "run", fake_run)
    ok, detail = fetcher.fetch_one(repo, tmp_path)
    assert ok is True
    assert detail == "deadbeef"


def test_fetch_one_fetch_failure(tmp_path, monkeypatch):
    repo = Repo(name="ServerApp", integration_branch="main")
    (tmp_path / "ServerApp").mkdir()
    monkeypatch.setattr(fetcher.subprocess, "run",
                        lambda *a, **k: _Proc(1, stderr="auth refused"))
    ok, detail = fetcher.fetch_one(repo, tmp_path)
    assert ok is False
    assert "auth refused" in detail


def test_fetch_one_revparse_failure(tmp_path, monkeypatch):
    repo = Repo(name="ServerApp", integration_branch="dev")
    (tmp_path / "ServerApp").mkdir()
    seq = {"i": 0}

    def fake_run(cmd, **kw):
        seq["i"] += 1
        if "fetch" in cmd:
            return _Proc(0)
        if "rev-parse" in cmd:
            return _Proc(1)
        return _Proc(0)
    monkeypatch.setattr(fetcher.subprocess, "run", fake_run)
    ok, detail = fetcher.fetch_one(repo, tmp_path)
    assert ok is False
    assert "unresolvable" in detail


def test_fetch_all_updates_repos(session, monkeypatch, tmp_path):
    r1 = Repo(name="ServerApp", integration_branch="main", status=RepoStatus.READY)
    r2 = Repo(name="ClientApp", integration_branch="main", status=RepoStatus.ERROR,
              status_detail="boom")
    r3 = Repo(name="Archived", integration_branch="main", status=RepoStatus.ARCHIVED)
    session.add_all([r1, r2, r3])
    session.commit()
    monkeypatch.setattr(fetcher, "fetch_one", lambda repo, gd: (True, "cafe1234") if repo.name != "Archived" else (False, "x"))
    monkeypatch.setattr(fetcher, "get_settings", lambda: type("S", (), {"golden_dir": tmp_path})())
    results = fetcher.fetch_all()
    assert "ServerApp" in results and "ok" in results["ServerApp"]
    assert "ClientApp" in results and "ok" in results["ClientApp"]
    assert "Archived" not in results
    session.expire_all()
    rr2 = session.get(Repo, r2.id)
    assert rr2.status == RepoStatus.READY
    assert rr2.status_detail == ""
    assert rr2.last_fetch_head == "cafe1234"


def test_fetch_all_records_failure(session, monkeypatch, tmp_path):
    r1 = Repo(name="ServerApp", integration_branch="main", status=RepoStatus.READY)
    session.add(r1); session.commit()
    monkeypatch.setattr(fetcher, "fetch_one", lambda repo, gd: (False, "PAT expired"))
    monkeypatch.setattr(fetcher, "get_settings", lambda: type("S", (), {"golden_dir": tmp_path})())
    results = fetcher.fetch_all()
    assert "FAIL" in results["ServerApp"]
    session.expire_all()
    assert "PAT expired" in session.get(Repo, r1.id).status_detail


def test_start_fetch_loop_idempotent(monkeypatch):
    fetcher._scheduler = None
    s1 = fetcher.start_fetch_loop()
    s2 = fetcher.start_fetch_loop()
    assert s1 is s2
    s1.shutdown(wait=False)
    fetcher._scheduler = None
