"""Guidebook seeding tests (Layer 2): deterministic generation from
fleet-config + profile, the <200 line contract, idempotent seeding, per-repo
failure isolation. Git runner + ADO client are fakes — no shells, no sockets.
"""

import pytest

from app.db.models.repo import Repo, RepoProfile, RepoStatus
from app.services import guidebooks

SERVERAPP_SERVICE = {
    "name": "ServerApp",
    "stack": "NestJS 11 + Drizzle ORM + Postgres + Socket.io",
    "role": "Monolithic backend. DB schema source of truth.",
    "calls": [
        {"to": "AIClusterApp", "mechanism": "REST (BACKEND_*_ENDPOINT)",
         "citation": "ServerApp/src/services/intake/intake-summary.service.ts:36"},
    ],
    "calledBy": ["ClientApp"],
    "notes": "Reaches LLM microservices ONLY via AIClusterApp, never directly.",
}

PROMPTFLOW_SERVICE = {
    "name": "PromptFlowApp",
    "role": "Legacy.",
    "liveFlows": ["intake-summary", "after-visit-summary"],
    "staleFlowsIgnore": ["assessment", "treatment-recommendation", "AVS"],
    "calls": [], "calledBy": [],
}

REPO_SEED = {"name": "ServerApp", "integrationBranch": "main",
             "notes": "Local checkout is often personal — never base on it."}


class FakeAdo:
    def __init__(self):
        self.prs = []

    async def create_pull_request(self, repo_id, source_branch, target_branch, title, description):
        self.prs.append({"repo_id": repo_id, "source": source_branch, "target": target_branch})
        return {"pullRequestId": 100 + len(self.prs)}


class FakeGit:
    def __init__(self, fail_on: str | None = None):
        self.calls = []
        self.fail_on = fail_on

    def __call__(self, args, cwd):
        self.calls.append((args, cwd))
        if self.fail_on and self.fail_on in args:
            raise guidebooks.GuidebookError("simulated git failure")


@pytest.fixture
def fleet(monkeypatch):
    services = {"ServerApp": SERVERAPP_SERVICE, "PromptFlowApp": PROMPTFLOW_SERVICE}
    seeds = {"ServerApp": REPO_SEED, "PromptFlowApp": {"integrationBranch": "pg-main"}}
    monkeypatch.setattr(guidebooks, "_fleet_entries", lambda: (services, seeds))


# ---------------------------------------------------------------- generation
def test_render_contains_golden_branch_rule_and_verification(fleet):
    text = guidebooks.render_agents_md(
        "ServerApp", SERVERAPP_SERVICE, REPO_SEED,
        profile_language="typescript", test_cmds=["npm test", "npm run lint"])
    assert "origin/main` after a fresh fetch" in text
    assert "`npm test`" in text
    assert "Calls **AIClusterApp**" in text
    assert "intake-summary.service.ts:36" in text
    assert "Called by: ClientApp" in text
    assert "ONLY via AIClusterApp" in text  # judgment line carried


def test_render_promptflow_anti_context(fleet):
    text = guidebooks.render_agents_md("PromptFlowApp", PROMPTFLOW_SERVICE,
                                       {"integrationBranch": "pg-main"})
    assert "DO NOT load into context" in text
    assert "assessment" in text and "AVS" in text
    assert "intake-summary, after-visit-summary" in text
    assert "origin/pg-main" in text


def test_render_without_service_entry_still_speaks_the_branch_rule(fleet):
    text = guidebooks.render_agents_md("KnowledgeBase", None,
                                       {"integrationBranch": "main"})
    assert "origin/main" in text
    assert "ask before inventing one" in text  # no profile test cmds registered


def test_render_is_deterministic(fleet):
    a = guidebooks.render_agents_md("ServerApp", SERVERAPP_SERVICE, REPO_SEED,
                                    test_cmds=["npm test"])
    b = guidebooks.render_agents_md("ServerApp", SERVERAPP_SERVICE, REPO_SEED,
                                    test_cmds=["npm test"])
    assert a == b
    assert len(a.splitlines()) <= guidebooks.MAX_LINES


def test_claude_bridge_is_one_import_line():
    assert guidebooks.render_claude_md() == "@AGENTS.md\n"


# -------------------------------------------------------------------- seeding
def _repo(session, name="ServerApp", status=RepoStatus.READY, with_profile=True):
    repo = Repo(name=name, integration_branch="main", ado_repo_id=f"ado-{name}",
                status=status)
    session.add(repo)
    session.flush()
    if with_profile:
        session.add(RepoProfile(repo_id=repo.id, language="typescript",
                                test_cmds=["npm test"]))
    session.commit()
    return repo


async def test_seed_writes_files_and_opens_pr(session, fleet, tmp_path):
    _repo(session)
    (tmp_path / "ServerApp").mkdir()
    ado, git = FakeAdo(), FakeGit()
    reports = await guidebooks.seed_guidebooks(ado, golden_dir=tmp_path, git_runner=git)
    assert reports == [{"repo": "ServerApp", "status": "pr_opened",
                        "pr_id": 101, "branch": "agent/guidebook-seed"}]
    content = (tmp_path / "ServerApp" / "AGENTS.md").read_text(encoding="utf-8")
    assert "# ServerApp — agent guidebook" in content
    assert (tmp_path / "ServerApp" / "CLAUDE.md").read_text() == "@AGENTS.md\n"
    assert ado.prs[0]["source"] == "agent/guidebook-seed"
    assert ado.prs[0]["target"] == "main"
    pushed = [args for args, _ in git.calls if "push" in args]
    assert pushed


async def test_seed_is_idempotent_when_content_unchanged(session, fleet, tmp_path):
    _repo(session)
    (tmp_path / "ServerApp").mkdir()
    ado, git = FakeAdo(), FakeGit()
    first = await guidebooks.seed_guidebooks(ado, golden_dir=tmp_path, git_runner=git)
    second = await guidebooks.seed_guidebooks(ado, golden_dir=tmp_path, git_runner=git)
    assert first[0]["status"] == "pr_opened"
    assert second == [{"repo": "ServerApp", "status": "unchanged"}]
    assert len(ado.prs) == 1


async def test_seed_repo_failure_is_recorded_not_raised(session, fleet, tmp_path):
    _repo(session)
    (tmp_path / "ServerApp").mkdir()
    ado = FakeAdo()
    git = FakeGit(fail_on="push")
    reports = await guidebooks.seed_guidebooks(ado, golden_dir=tmp_path, git_runner=git)
    assert reports[0]["status"] == "error"
    assert "simulated git failure" in reports[0]["error"]


async def test_seed_skips_non_ready_repos(session, fleet, tmp_path):
    _repo(session, status=RepoStatus.CLONING)
    reports = await guidebooks.seed_guidebooks(FakeAdo(), golden_dir=tmp_path,
                                               git_runner=FakeGit())
    assert reports == []
