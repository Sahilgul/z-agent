import asyncio

import pytest

from app.db.models.delivery import PrLink
from app.db.models.event import Event
from app.db.models.lane import Lane
from app.db.models.repo import Repo
from app.db.models.run import Plan, Run
from app.db.models.trajectory import TrajectorySummary
from app.services import delivery


# --------------------------------------------------------------- branch_name_for
def test_branch_name_for_with_lane():
    run = Run(id="r1234567-aaaa", title="Fix the Scribe Summary Bug!")
    lane = Lane(id="lane1234-aaaa", run_id=run.id, persona="researcher", status="running")
    name = delivery.branch_name_for(run, lane)
    assert name.startswith("agent/r1234567-")
    assert name.endswith("/lane1234")


def test_branch_name_for_without_lane():
    run = Run(id="r1234567-aaaa", title="Fix the Scribe Summary Bug!")
    name = delivery.branch_name_for(run)
    # plan §9: agent/<run_id>-<slug> — the agent/* namespace carries the ADO
    # branch policies (§10); a zagent/* prefix would be rejected on push.
    assert name == "agent/r1234567-fix-the-scribe-summary-bug"


def test_branch_name_for_empty_title():
    run = Run(id="r1234567-aaaa", title="")
    name = delivery.branch_name_for(run)
    assert name.endswith("-change")


def test_branch_name_for_slug_truncation():
    run = Run(id="r1234567-aaaa", title="x" * 100)
    name = delivery.branch_name_for(run)
    parts = name.split("/")
    assert parts[0] == "agent"
    # run8 + dash + slug(≤32)
    assert len(parts[1]) <= 8 + 1 + 32


# --------------------------------------------------------------- evidence_sha256
def test_evidence_sha256_deterministic():
    pkg = {"b": 2, "a": [1, {"z": None}]}
    assert delivery.evidence_sha256(pkg) == delivery.evidence_sha256(
        {"a": [1, {"z": None}], "b": 2})  # key order must not matter


def test_evidence_sha256_changes_with_content():
    assert delivery.evidence_sha256({"x": 1}) != delivery.evidence_sha256({"x": 2})


# --------------------------------------------------------------- build_evidence_package
def test_build_evidence_package_run_not_found(session):
    with pytest.raises(delivery.DeliveryError, match="run not found"):
        delivery.build_evidence_package("ghost")


def test_build_evidence_package_minimal(session, make_user):
    u = make_user("alice", role="member", status="active")
    run = Run(id="r1", created_by=u.id, mode="ask", stage="completed", title="t", cost_usd=1.25, tokens=500)
    session.add(run); session.commit()
    pkg = delivery.build_evidence_package("r1")
    assert pkg["schema_version"] == 1
    assert pkg["run_id"] == "r1"
    assert pkg["plan_steps"] == []
    assert pkg["lanes"] == []
    assert pkg["test_signals"] == []
    assert pkg["trajectory"] == ""
    assert pkg["total_cost_usd"] == 1.25
    assert pkg["total_tokens"] == 500


def test_build_evidence_package_full(session, make_user):
    u = make_user("alice", role="member", status="active")
    run = Run(id="r1", created_by=u.id, mode="ask", stage="completed", title="ship it")
    lane = Lane(id="l1", run_id="r1", persona="researcher", status="completed", next_seq=3, cost_usd=2.0)
    plan = Plan(run_id="r1", structured={"title": "Plan A", "steps": [
        {"index": 0, "title": "s0", "status": "done"},
        {"index": 1, "title": "s1", "status": "skipped"},
    ]}, status="approved")
    ev = Event(run_id="r1", lane_id="l1", seq=0, type="test_run", title="pytest", payload={"pass": 1})
    traj = TrajectorySummary(run_id="r1", lane_id="l1", user_id=u.id, summary="distilled")
    session.add_all([run, lane, plan, ev, traj]); session.commit()
    pkg = delivery.build_evidence_package("r1")
    assert pkg["plan_title"] == "Plan A"
    assert len(pkg["plan_steps"]) == 2
    assert pkg["plan_steps"][0]["status"] == "done"
    assert len(pkg["lanes"]) == 1
    assert pkg["lanes"][0]["persona"] == "researcher"
    assert len(pkg["test_signals"]) == 1
    assert pkg["trajectory"] == "distilled"


# --------------------------------------------------------------- evidence_complete
def test_evidence_complete_empty():
    pkg = {"plan_steps": [], "lanes": []}
    gaps = delivery.evidence_complete(pkg)
    assert "no approved plan on record" in gaps
    assert "no lane completed successfully" in gaps


def test_evidence_complete_unfinished_steps():
    pkg = {"plan_steps": [{"status": "pending"}, {"status": "done"}], "lanes": [{"status": "completed"}]}
    gaps = delivery.evidence_complete(pkg)
    assert any("not done" in g for g in gaps)


def test_evidence_complete_no_completed_lane():
    pkg = {"plan_steps": [{"status": "done"}], "lanes": [{"status": "running"}]}
    gaps = delivery.evidence_complete(pkg)
    assert "no lane completed successfully" in gaps


def test_evidence_complete_cleared():
    pkg = {"plan_steps": [{"status": "done"}, {"status": "skipped"}],
           "lanes": [{"status": "completed"}, {"status": "running"}]}
    assert delivery.evidence_complete(pkg) == []


# --------------------------------------------------------------- _git / push_branch
class _FakeProc:
    def __init__(self, returncode=0, out=b"", err=b""):
        self.returncode = returncode
        self._out = out
        self._err = err

    async def communicate(self):
        return self._out, self._err


def test_git_success(monkeypatch):
    captured = {}

    async def fake_exec(*args, cwd=None, env=None, stdout=None, stderr=None):
        captured["args"] = args
        captured["cwd"] = cwd
        captured["env"] = env
        return _FakeProc(returncode=0, out=b"ok\n", err=b"")

    monkeypatch.setattr(delivery.asyncio, "create_subprocess_exec", fake_exec)
    out = asyncio.run(delivery._git(["status"], cwd="/ws", env_extra={"FLEET_PAT": "p"}))
    assert out == "ok\n"
    assert captured["env"]["FLEET_PAT"] == "p"
    assert captured["env"]["ZAGENT_CREDENTIAL_SCOPE"] == "fleet"
    assert "GIT_CREDENTIAL_HELPER" in captured["env"]


def test_git_failure_raises(monkeypatch):
    async def fake_exec(*args, **kwargs):
        return _FakeProc(returncode=1, out=b"", err=b"boom")
    monkeypatch.setattr(delivery.asyncio, "create_subprocess_exec", fake_exec)
    with pytest.raises(delivery.DeliveryError, match="git .* failed"):
        asyncio.run(delivery._git(["push", "origin"], cwd="/ws", env_extra={}))


def test_push_branch_invokes_git(monkeypatch):
    calls = []

    async def fake_git(args, cwd, env_extra):
        calls.append((args, cwd, env_extra))
        return "done"

    monkeypatch.setattr(delivery, "_git", fake_git)
    repo = Repo(name="ServerApp", integration_branch="main")
    asyncio.run(delivery.push_branch("r1", repo, "/ws", "agent/r1-x"))
    assert calls[0][0] == ["push", "-u", "origin", "agent/r1-x"]
    assert calls[0][1] == "/ws"
    assert "FLEET_PAT" in calls[0][2]


# --------------------------------------------------------------- sync_before_push
def test_sync_before_push_fetches_then_rebases(monkeypatch):
    calls = []

    async def fake_git(args, cwd, env_extra):
        calls.append(args)
        return ""

    monkeypatch.setattr(delivery, "_git", fake_git)
    asyncio.run(delivery.sync_before_push("r1", "/ws", "pg-main"))
    assert calls == [["fetch", "origin", "pg-main"], ["rebase", "origin/pg-main"]]


def test_sync_before_push_conflict_aborts_and_raises(monkeypatch):
    calls = []

    async def fake_git(args, cwd, env_extra):
        calls.append(args)
        if args[0] == "rebase" and args[1] != "--abort":
            raise delivery.DeliveryError("git rebase failed: CONFLICT")
        return ""

    monkeypatch.setattr(delivery, "_git", fake_git)
    with pytest.raises(delivery.DeliveryError, match="conflicted"):
        asyncio.run(delivery.sync_before_push("r1", "/ws", "main"))
    assert ["rebase", "--abort"] in calls  # workspace left clean for the human


# --------------------------------------------------------------- open_pr
class _FakeAdo:
    def __init__(self, pr_id=42):
        self._pr_id = pr_id
        self.created = []

    async def create_pull_request(self, **kwargs):
        self.created.append(kwargs)
        return {"pullRequestId": self._pr_id}


def _seed_complete_run(session, make_user):
    u = make_user("alice", role="member", status="active")
    run = Run(id="r1", created_by=u.id, mode="ask", stage="completed", title="ship it", repo="ServerApp")
    lane = Lane(id="l1", run_id="r1", persona="researcher", status="completed", next_seq=1, cost_usd=1.0)
    plan = Plan(run_id="r1", structured={"title": "Plan A", "steps": [{"index": 0, "title": "s0", "status": "done"}]},
                status="approved")
    repo = Repo(name="ServerApp", integration_branch="main", ado_repo_id="ado-123")
    session.add_all([run, lane, plan, repo]); session.commit()
    return run, repo


def test_open_pr_evidence_incomplete(session, make_user):
    u = make_user("alice", role="member", status="active")
    run = Run(id="r1", created_by=u.id, mode="ask", stage="investigating", title="t")
    session.add(run); session.commit()
    with pytest.raises(delivery.DeliveryError, match="evidence incomplete"):
        asyncio.run(delivery.open_pr("r1", "ServerApp", "/ws"))


def test_open_pr_run_or_repo_missing(session, make_user):
    u = make_user("alice", role="member", status="active")
    run = Run(id="r1", created_by=u.id, mode="ask", stage="completed", title="t")
    lane = Lane(id="l1", run_id="r1", persona="researcher", status="completed")
    plan = Plan(run_id="r1", structured={"title": "P", "steps": [{"status": "done"}]}, status="approved")
    session.add_all([run, lane, plan]); session.commit()
    with pytest.raises(delivery.DeliveryError, match="run or repo not found"):
        asyncio.run(delivery.open_pr("r1", "GhostRepo", "/ws"))


def test_open_pr_success(session, make_user, monkeypatch):
    run, repo = _seed_complete_run(session, make_user)

    order = []

    async def fake_sync(run_id, ws, target):
        order.append(("sync", target))
    async def fake_push(run_id, r, ws, branch):
        order.append(("push", branch))
    monkeypatch.setattr(delivery, "sync_before_push", fake_sync)
    monkeypatch.setattr(delivery, "push_branch", fake_push)

    fake_ado = _FakeAdo(pr_id=77)
    link = asyncio.run(delivery.open_pr("r1", "ServerApp", "/ws", ado_client=fake_ado))
    assert link.ado_pr_id == 77
    assert link.status == "open"
    assert link.repo == "ServerApp"
    assert fake_ado.created[0]["source_branch"].startswith("agent/r1-")
    assert fake_ado.created[0]["target_branch"] == "main"
    assert fake_ado.created[0]["repo_id"] == "ado-123"
    # plan §3: the pre-PR fetch+rebase runs BEFORE the push.
    assert order == [("sync", "main"), ("push", link.branch)]
    # tamper-evidence: the PR body pins the package hash and the DB copy matches.
    assert link.evidence["sha256"] == delivery.evidence_sha256(
        {k: v for k, v in link.evidence.items() if k != "sha256"})
    assert f"evidence sha256: {link.evidence['sha256']}" in fake_ado.created[0]["description"]
    session.expire_all()
    assert session.query(PrLink).filter_by(run_id="r1").one().ado_pr_id == 77


# --------------------------------------------------------------- merge_pr
class _FakeAdoMerge:
    def __init__(self):
        self.completed = []

    async def complete_pull_request(self, **kwargs):
        self.completed.append(kwargs)
        return {"mergeId": 1}


def test_merge_pr_no_open_pr(session, make_user):
    u = make_user("alice", role="member", status="active")
    run = Run(id="r1", created_by=u.id, mode="ask", stage="completed", title="t")
    session.add(run); session.commit()
    with pytest.raises(delivery.DeliveryError, match="no open PR"):
        asyncio.run(delivery.merge_pr("r1", u.id))


def test_merge_pr_no_ado_id(session, make_user):
    u = make_user("alice", role="member", status="active")
    run = Run(id="r1", created_by=u.id, mode="ask", stage="completed", title="t", repo="ServerApp")
    repo = Repo(name="ServerApp", integration_branch="main")
    link = PrLink(run_id="r1", repo="ServerApp", branch="agent/r1-x", ado_pr_id=None, status="open")
    session.add_all([run, repo, link]); session.commit()
    with pytest.raises(delivery.DeliveryError, match="no ADO id"):
        asyncio.run(delivery.merge_pr("r1", u.id))


def _seed_mergeable(session, make_user):
    u = make_user("alice", role="member", status="active")
    run = Run(id="r1", created_by=u.id, mode="ask", stage="pr_ready", title="t", repo="ServerApp")
    repo = Repo(name="ServerApp", integration_branch="main", ado_repo_id="ado-9")
    link = PrLink(run_id="r1", repo="ServerApp", branch="agent/r1-x", ado_pr_id=99, status="open")
    session.add_all([run, repo, link]); session.commit()
    return u


def test_merge_pr_success(session, make_user):
    u = _seed_mergeable(session, make_user)
    fake = _FakeAdoMerge()
    result = asyncio.run(delivery.merge_pr("r1", u.id, ado_client=fake))
    assert result["handoff_url"] is None
    link = result["link"]
    assert link.status == "merged"
    assert link.merged_by == u.id
    assert link.merged_at is not None
    assert fake.completed[0]["pr_id"] == 99
    assert fake.completed[0]["repo_id"] == "ado-9"


def test_merge_pr_native_ui_hands_off_without_completing(session, make_user, monkeypatch):
    """§9 merge-identity lock: compliance disallows bypass-on-complete -> NO
    completion call; the human finishes in ADO under their own identity and the
    handoff is an audit event on the run."""
    from app.core.config import Settings

    u = _seed_mergeable(session, make_user)
    monkeypatch.setattr(delivery, "get_settings",
                        lambda: Settings(ado_org="acme", ado_project="Product",
                                         merge_native_ui=True))
    fake = _FakeAdoMerge()
    result = asyncio.run(delivery.merge_pr("r1", u.id, ado_client=fake))
    assert fake.completed == []  # the service account never touches the merge
    assert result["handoff_url"] == (
        "https://dev.azure.com/acme/Product/_git/ado-9/pullrequest/99")
    session.expire_all()
    link = session.query(PrLink).filter_by(run_id="r1").one()
    assert link.status == "open"  # completion happens out-of-band in ADO
    ev = session.query(Event).filter_by(run_id="r1", type="merge_handoff").one()
    assert ev.payload["decided_by"] == u.id
    assert ev.payload["handoff_url"] == result["handoff_url"]
