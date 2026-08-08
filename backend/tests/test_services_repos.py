
import pytest

from app.db.models.repo import Repo, RepoProfile, RepoStatus
from app.services import repos
from app.services.repos import (
    OnboardingError,
    archive_repo,
    register_repo,
    validate_remote,
)


def test_register_repo_creates_new(session, make_user):
    u = make_user("adder")
    r = register_repo("ServerApp", "https://dev.azure.com/x/_git/ServerApp", "pg-main", u.id)
    assert r.id is not None
    assert r.status == RepoStatus.REGISTERED
    assert r.added_by == u.id


def test_register_repo_dedupe_by_url(session):
    r1 = register_repo("ServerApp", "https://dev.azure.com/x/_git/ServerApp", "pg-main", None)
    r2 = register_repo("ServerApp2", "https://dev.azure.com/x/_git/ServerApp", "main", None)
    assert r1.id == r2.id


def test_register_repo_dedupe_by_name(session):
    r1 = register_repo("ServerApp", "https://dev.azure.com/x/_git/ServerApp", "pg-main", None)
    r2 = register_repo("ServerApp", "https://dev.azure.com/x/_git/Other", "main", None)
    assert r1.id == r2.id


def test_register_repo_revives_archived(session):
    r1 = register_repo("Arch", "https://dev.azure.com/x/_git/Arch", "pg-main", None)
    archive_repo(r1.id)
    got = session.get(Repo, r1.id)
    assert got.status == RepoStatus.ARCHIVED
    r2 = register_repo("Arch", "https://dev.azure.com/x/_git/Arch", "main", None)
    assert r2.id == r1.id
    session.expire_all()
    got = session.get(Repo, r1.id)
    assert got.status == RepoStatus.REGISTERED
    assert got.archived_at is None


def test_validate_remote_success(monkeypatch):
    class R:
        returncode = 0
        stderr = ""
        stdout = "abc123\trefs/heads/pg-main\ndef456\trefs/heads/main\n"
    monkeypatch.setattr(repos.subprocess, "run", lambda *a, **k: R())
    branches = validate_remote("https://x", "pat")
    # W9-M7: canonical HEAD names sort first so the picker's default is the
    # integration line, not whatever the remote listed alphabetically.
    assert branches == ["main", "pg-main"]


def test_validate_remote_head_first_ordering(monkeypatch):
    class R:
        returncode = 0
        stderr = ""
        stdout = ("a\trefs/heads/zeta\nb\trefs/heads/develop\nc\trefs/heads/alpha\n"
                  "d\trefs/heads/master\n")
    monkeypatch.setattr(repos.subprocess, "run", lambda *a, **k: R())
    assert validate_remote("https://x", "pat") == ["master", "develop", "alpha", "zeta"]


def test_validate_remote_failure(monkeypatch):
    class R:
        returncode = 1
        stderr = "boom"
        stdout = ""
    monkeypatch.setattr(repos.subprocess, "run", lambda *a, **k: R())
    with pytest.raises(OnboardingError):
        validate_remote("https://x", "pat")


def test_archive_repo_marks_archived_and_shreds(tmp_path, session, monkeypatch):
    from app.core.config import get_settings
    golden = tmp_path / "golden"
    monkeypatch.setattr(get_settings(), "golden_dir", golden)
    r = register_repo("ToArchive", "https://x/_git/ToArchive", "pg-main", None)
    repo_golden = golden / "ToArchive"
    repo_golden.mkdir(parents=True)
    (repo_golden / "f.txt").write_text("x")
    archive_repo(r.id)
    got = session.get(Repo, r.id)
    assert got.status == RepoStatus.ARCHIVED
    assert got.archived_at is not None
    assert not repo_golden.exists()


def test_archive_repo_missing_is_noop(session):
    archive_repo(999999)


async def test_onboard_happy_path(session, monkeypatch, tmp_path):
    from app.core.config import get_settings
    settings = get_settings()
    monkeypatch.setattr(settings, "golden_dir", tmp_path / "golden")
    monkeypatch.setattr(settings, "fleet_config_dir", tmp_path / "fleet-config")
    (tmp_path / "fleet-config").mkdir()
    (tmp_path / "fleet-config" / "scripts").mkdir()

    r = register_repo("Happy", "https://x/_git/Happy", "pg-main", None)

    def fake_validate(url, pat):
        return ["pg-main", "main"]
    monkeypatch.setattr(repos, "validate_remote", fake_validate)

    runs = []

    def fake_run(args, **kw):
        runs.append(args)
        class R:
            returncode = 0
            stderr = ""
            stdout = "deadbeef\n" if "rev-parse" in args else ""
        return R()
    monkeypatch.setattr(repos.subprocess, "run", fake_run)

    class FakeRelay:
        async def publish_global(self, msg, user_id=None):
            self.msg = msg
            self.msg_user_id = user_id
    relay = FakeRelay()
    await repos.onboard(r.id, relay)

    got = session.get(Repo, r.id)
    assert got.status == RepoStatus.READY_NO_MAP
    assert got.last_fetch_head == "deadbeef"
    assert got.last_fetch_at is not None
    prof = session.query(RepoProfile).filter_by(repo_id=r.id).one()
    assert prof is not None
    assert relay.msg == {"type": "repo_added", "repo": "Happy"}


async def test_onboard_missing_branch_failure_path(session, monkeypatch, tmp_path):
    from app.core.config import get_settings
    monkeypatch.setattr(get_settings(), "golden_dir", tmp_path / "golden")
    r = register_repo("Sad", "https://x/_git/Sad", "nope", None)
    monkeypatch.setattr(repos, "validate_remote", lambda url, pat: ["pg-main"])
    await repos.onboard(r.id, None)
    got = session.get(Repo, r.id)
    assert got.status == RepoStatus.ERROR
    assert "nope" in got.status_detail


async def test_onboard_missing_repo_returns(session):
    await repos.onboard(999999, None)
