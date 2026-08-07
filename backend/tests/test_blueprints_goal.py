"""Goal blueprint tests: the zero-interruption PRD->PR pipeline, node by node."""

import json
from types import SimpleNamespace

import pytest
from collegium_contracts import RunStage

from app.db.models.event import Event
from app.db.models.mode import Mode
from app.db.models.repo import Repo
from app.db.models.run import Plan, PlanStep, Run
from app.db.models.thread import Thread
from app.db.models.trajectory import TrajectorySummary
from app.orchestrator.blueprints.base import BlueprintContext
from app.orchestrator.blueprints.goal import (
    CRITIQUE_ROUNDS,
    MAX_FIX_ROUNDS,
    GoalBlueprint,
    _await_thread,
)

PLAN_DICT = {
    "schema_version": 1,
    "title": "Usage stats page",
    "summary": "Add a team usage-stats page.",
    "steps": [
        {"index": 0, "title": "Add API endpoint", "description": "GET /usage",
         "repo": "ServerApp", "files": ["src/api/usage.ts"],
         "success_criterion": "endpoint returns 200", "status": "pending"},
        {"index": 1, "title": "Add page", "description": "Usage page",
         "repo": "ServerApp", "files": ["src/pages/Usage.tsx"],
         "success_criterion": "page renders", "status": "pending"},
    ],
    "blast_radius": [], "risks": [], "evidence_contract": ["tests_pass"],
}
REVISED_PLAN_DICT = {**PLAN_DICT, "title": "Usage stats page (revised)"}


class _FakeLaneManager:
    """Spawns REAL Thread rows (status completed -> _await_thread returns
    immediately) and plants canned message Events per persona, so
    _last_message_text reads exactly what the test scripts. A persona mapped
    to a LIST pops one text per spawn (critic/reviser rounds)."""

    def __init__(self, session, messages=None):
        self._session = session
        self._messages = messages or {}
        self.spawned = []
        self.settled = []
        self.finished = []
        self._n = 0

    async def spawn(self, run, persona, prompt, persona_prompt, writable_repo,
                    context_repos, resume_session=False, resume_from_thread_id=None,
                    preserve_workspace=False):
        self._n += 1
        tid = f"l{self._n}-{persona}"
        self.spawned.append({"persona": persona, "prompt": prompt,
                             "persona_prompt": persona_prompt,
                             "writable": writable_repo is not None,
                             "preserve_workspace": preserve_workspace})
        thread = Thread(id=tid, run_id=run.id, persona=persona, status="completed",
                        spawn_context={"prompt": prompt})
        self._session.add(thread)
        text = self._messages.get(persona)
        if isinstance(text, list):
            text = text.pop(0) if text else None
        if text is not None:
            self._session.add(Event(run_id=run.id, thread_id=tid, seq=0,
                                    type="message", title="m", payload={"text": text}))
        self._session.commit()
        return thread

    async def spawn_many(self, run, specs, context_repos):
        threads = []
        for spec in specs:
            threads.append(await self.spawn(
                run, persona=spec["persona"], prompt=spec["prompt"],
                persona_prompt=spec["persona_prompt"], writable_repo=None,
                context_repos=context_repos))
        return threads

    async def settle_cost(self, thread_id):
        self.settled.append(thread_id)

    async def finish_thread(self, thread_id, final_status="completed"):
        self.finished.append(thread_id)


def _ctx(run, services=None, artifacts=None):
    return BlueprintContext(run=run, services=services or {}, artifacts=artifacts or {})


def _run(session, make_user, **kw):
    u = make_user("alice", role="member", status="active")
    run = Run(id=kw.pop("id", "r1"), created_by=u.id, mode="goal",
              stage="queued", title=kw.pop("title", "ship usage stats"),
              repo=kw.pop("repo", "ServerApp"), **kw)
    session.add(run)
    session.commit()
    return run


def _green_suite():
    return {"repo": "ServerApp", "passed": True, "checks": [
        {"name": "tests", "skipped": False, "passed": True, "returncode": 0,
         "stdout": "2 passed", "stderr": ""},
        {"name": "ruff", "skipped": False, "passed": True, "returncode": 0,
         "stdout": "", "stderr": ""},
    ]}


def _red_suite(check_name="ruff"):
    return {"repo": "ServerApp", "passed": False, "checks": [
        {"name": "tests", "skipped": False, "passed": True, "returncode": 0,
         "stdout": "2 passed", "stderr": ""},
        {"name": check_name, "skipped": False, "passed": False, "returncode": 1,
         "stdout": "", "stderr": "E501 line too long"},
    ]}


# --------------------------------------------------------------- nodes
def test_nodes_in_pipeline_order():
    bp = GoalBlueprint()
    nodes = bp.nodes()
    assert [n.name for n in nodes] == [
        "hydrate", "explore", "plan", "refine", "present",
        "develop", "verify", "ship",
    ]
    assert [n.stage for n in nodes] == [
        RunStage.PROVISIONING, RunStage.INVESTIGATING, RunStage.PLANNING,
        None, None, RunStage.DEVELOPING, RunStage.VERIFYING, RunStage.PR_READY,
    ]
    # the green gate + ship are pure control-plane code
    assert nodes[6].deterministic and nodes[7].deterministic


# --------------------------------------------------------------- _hydrate
async def test_hydrate_stamps_workspace_branch_and_scope(session, make_user):
    run = _run(session, make_user)
    session.add(Repo(name="ServerApp", integration_branch="main"))
    session.add(Mode(name="goal", persona_prompt="p", permission_mode="bypassPermissions",
                     permissions={"writable": True, "repos": []}))
    session.commit()
    bp = GoalBlueprint()
    ctx = _ctx(run, artifacts={"task": "ship usage stats"})
    await bp._hydrate(ctx)
    assert ctx.artifacts["repo_row"].name == "ServerApp"
    assert ctx.artifacts["branch"].startswith("agent/r1-")
    assert ctx.artifacts["workspace"].endswith("r1/ServerApp")
    assert ctx.artifacts["permissions"]["writable"] is True
    session.expire_all()
    assert session.get(Run, "r1").session_volume_path.endswith("r1/ServerApp")


async def test_hydrate_missing_repo_raises(session, make_user):
    run = _run(session, make_user, repo="Ghost")
    bp = GoalBlueprint()
    with pytest.raises(RuntimeError, match="repo 'Ghost' not registered"):
        await bp._hydrate(_ctx(run))


async def test_hydrate_mounts_full_fleet_as_context(session, make_user):
    """Goal is a fleet mode: ALL usable repos are mounted read-only as
    workspace context, no @mention required. The explicit repo (if any) heads
    the list as the writable target; the rest of the fleet follows read-only."""
    run = _run(session, make_user, repo="ServerApp")
    session.add_all([
        Repo(name="ServerApp", integration_branch="main", status="ready"),
        Repo(name="ClientApp", integration_branch="main", status="ready"),
        Repo(name="Billing-Engine", integration_branch="pg-main", status="ready-no-map"),
    ])
    session.add(Mode(name="goal", persona_prompt="p", permission_mode="bypassPermissions",
                     permissions={"writable": True, "repos": []}))
    session.commit()
    bp = GoalBlueprint()
    ctx = _ctx(run, artifacts={"task": "ship usage stats"})
    await bp._hydrate(ctx)
    assert ctx.artifacts["repo_row"].name == "ServerApp"  # explicit target heads
    names = [r.name for r in ctx.artifacts["context_repos"]]
    assert names[0] == "ServerApp"
    assert set(names) == {"ServerApp", "ClientApp", "Billing-Engine"}


async def test_hydrate_no_fleet_no_target_raises(session, make_user):
    """No usable repos and no explicit target -> goal mode has nothing to run
    against. Fails clearly (no default repo anywhere in the system)."""
    run = _run(session, make_user, repo=None)
    bp = GoalBlueprint()
    with pytest.raises(RuntimeError, match="no usable repos in the fleet"):
        await bp._hydrate(_ctx(run))


# --------------------------------------------------------------- _explore
async def test_explore_single_researcher_by_default(session, make_user):
    run = _run(session, make_user)
    repo = Repo(name="ServerApp", integration_branch="main")
    session.add(repo)
    session.commit()
    lm = _FakeLaneManager(session, {"researcher": "change surface: src/api/usage.ts"})
    bp = GoalBlueprint()
    ctx = _ctx(run, services={"thread_manager": lm},
               artifacts={"task": "ship usage stats", "repo_row": repo, "thread_ids": []})
    await bp._explore(ctx)
    assert [s["persona"] for s in lm.spawned] == ["researcher"]
    assert lm.spawned[0]["writable"] is False
    assert "change surface" in ctx.artifacts["explore_summary"]
    assert len(ctx.artifacts["thread_ids"]) == 1


async def test_explore_fans_out_when_requested(session, make_user):
    run = _run(session, make_user)
    repo = Repo(name="ServerApp", integration_branch="main")
    session.add(repo)
    session.commit()
    lm = _FakeLaneManager(session, {"explorer": "notebook findings"})
    bp = GoalBlueprint()
    ctx = _ctx(run, services={"thread_manager": lm},
               artifacts={"task": "ship usage stats", "repo_row": repo,
                          "thread_ids": [], "fanout": 3})
    await bp._explore(ctx)
    assert [s["persona"] for s in lm.spawned] == ["explorer"] * 3
    # distinct angles, never arithmetic clones
    prompts = [s["prompt"] for s in lm.spawned]
    assert len(set(prompts)) == 3
    assert ctx.artifacts["explore_summary"].count("--- explorer") == 3


# --------------------------------------------------------------- _plan
async def test_plan_drafts_parseable_plan(session, make_user):
    run = _run(session, make_user)
    repo = Repo(name="ServerApp", integration_branch="main")
    session.add(repo)
    session.commit()
    lm = _FakeLaneManager(session, {"planner": json.dumps(PLAN_DICT)})
    bp = GoalBlueprint()
    ctx = _ctx(run, services={"thread_manager": lm},
               artifacts={"task": "ship usage stats", "repo_row": repo,
                          "thread_ids": [], "explore_summary": "ctx"})
    await bp._plan(ctx)
    assert ctx.artifacts["draft_plan"]["title"] == "Usage stats page"
    assert "Exploration context" in lm.spawned[0]["prompt"]


async def test_plan_raises_on_unparseable_draft(session, make_user):
    run = _run(session, make_user)
    repo = Repo(name="ServerApp", integration_branch="main")
    session.add(repo)
    session.commit()
    lm = _FakeLaneManager(session, {"planner": "sorry, I cannot plan this"})
    bp = GoalBlueprint()
    ctx = _ctx(run, services={"thread_manager": lm},
               artifacts={"task": "t", "repo_row": repo, "thread_ids": []})
    with pytest.raises(RuntimeError, match="no parseable Plan JSON"):
        await bp._plan(ctx)


# --------------------------------------------------------------- _refine
async def test_refine_runs_exactly_three_critic_revise_rounds(session, make_user):
    run = _run(session, make_user)
    repo = Repo(name="ServerApp", integration_branch="main")
    session.add(repo)
    session.commit()
    lm = _FakeLaneManager(session, {
        "critic": ["BLOCKING: step 0 criterion vague",
                   "BLOCKING: missing migration step",
                   "no blocking findings"],
        "reviser": [json.dumps(REVISED_PLAN_DICT)] * 3,
    })
    bp = GoalBlueprint()
    ctx = _ctx(run, services={"thread_manager": lm},
               artifacts={"task": "t", "repo_row": repo, "thread_ids": [],
                          "draft_plan": PLAN_DICT})
    await bp._refine(ctx)
    personas = [s["persona"] for s in lm.spawned]
    assert personas == ["critic", "reviser"] * CRITIQUE_ROUNDS
    assert ctx.artifacts["draft_plan"]["title"] == "Usage stats page (revised)"
    notes = ctx.artifacts["critique_notes"]
    assert len([n for n in notes if "critic" in n]) == CRITIQUE_ROUNDS
    # every critic prompt carried the current draft JSON
    assert "Draft Plan" in lm.spawned[0]["prompt"]


async def test_refine_keeps_prior_draft_when_reviser_unparseable(session, make_user):
    run = _run(session, make_user)
    repo = Repo(name="ServerApp", integration_branch="main")
    session.add(repo)
    session.commit()
    lm = _FakeLaneManager(session, {
        "critic": ["BLOCKING: x"] * 3,
        "reviser": ["not json at all"] * 3,
    })
    bp = GoalBlueprint()
    ctx = _ctx(run, services={"thread_manager": lm},
               artifacts={"task": "t", "repo_row": repo, "thread_ids": [],
                          "draft_plan": PLAN_DICT})
    await bp._refine(ctx)
    assert ctx.artifacts["draft_plan"]["title"] == "Usage stats page"
    assert any("unparseable" in n for n in ctx.artifacts["critique_notes"])


# --------------------------------------------------------------- _present
async def test_present_persists_auto_approved_plan(session, make_user):
    run = _run(session, make_user)
    session.add(Repo(name="ServerApp", integration_branch="main"))
    session.commit()
    bp = GoalBlueprint()
    repo = session.query(Repo).filter_by(name="ServerApp").one()
    ctx = _ctx(run, artifacts={"draft_plan": PLAN_DICT, "repo_row": repo,
                               "blast_radius": ["gateway"],
                               "critique_notes": ["round 1 critic: ok"]})
    await bp._present(ctx)
    session.expire_all()
    plan = session.query(Plan).filter_by(run_id="r1").one()
    # goal mode: the pipeline IS the approval — no awaiting_user gate
    assert plan.status == "approved"
    assert plan.structured["auto_approved"] is True
    assert plan.structured["blast_radius"] == ["gateway"]
    assert plan.structured["critic_notes"] == ["round 1 critic: ok"]
    steps = session.query(PlanStep).filter_by(plan_id=plan.id).order_by(PlanStep.index).all()
    assert [s.status for s in steps] == ["pending", "pending"]
    assert steps[0].success_criterion == "endpoint returns 200"
    assert ctx.artifacts["plan_row_id"] == plan.id


# --------------------------------------------------------------- _develop
async def test_develop_spawns_writable_developer_and_marks_steps(session, make_user):
    run = _run(session, make_user)
    repo = Repo(name="ServerApp", integration_branch="main")
    session.add_all([repo, Mode(name="goal", persona_prompt="p",
                                permission_mode="bypassPermissions")])
    session.commit()
    bp = GoalBlueprint()
    ctx = _ctx(run, artifacts={"task": "t", "repo_row": repo,
                               "permissions": {"writable": True, "repos": []},
                               "branch": "agent/r1-x", "workspace": "/ws",
                               "thread_ids": [],
                               "draft_plan": PLAN_DICT, "blast_radius": []})
    await bp._present(ctx)  # persist a real plan so step fallback has rows
    lm = _FakeLaneManager(session)
    ctx.services["thread_manager"] = lm
    await bp._develop(ctx)
    dev = lm.spawned[0]
    assert dev["persona"] == "developer"
    assert dev["writable"] is True
    assert dev["preserve_workspace"] is False
    assert "agent/r1-x" in dev["prompt"]
    session.expire_all()
    steps = session.query(PlanStep).order_by(PlanStep.index).all()
    assert [s.status for s in steps] == ["done", "done"]


# --------------------------------------------------------------- _verify
async def test_verify_green_gate_needs_no_fixer(session, make_user, monkeypatch):
    run = _run(session, make_user)
    repo = Repo(name="ServerApp", integration_branch="main")
    session.add_all([repo, Mode(name="goal", persona_prompt="p",
                                permission_mode="bypassPermissions")])
    session.commit()
    calls = []

    async def fake_suite(workspace, repo_name, test_cmds=None, **kw):
        calls.append((workspace, repo_name))
        return _green_suite()

    monkeypatch.setattr("app.services.evidence.verify_suite", fake_suite)
    lm = _FakeLaneManager(session)
    bp = GoalBlueprint()
    ctx = _ctx(run, services={"thread_manager": lm},
               artifacts={"repo_row": repo, "workspace": "/ws", "test_cmds": None,
                          "thread_ids": [], "develop_thread_id": "control-plane"})
    await bp._verify(ctx)
    assert calls == [("/ws", "ServerApp")]
    assert lm.spawned == []  # no fix rounds on a green gate
    ev = session.query(Event).filter_by(run_id="r1", type="test_run").one()
    assert ev.payload["passed"] is True


async def test_verify_red_spawns_fixer_with_preserved_workspace(session, make_user, monkeypatch):
    run = _run(session, make_user)
    repo = Repo(name="ServerApp", integration_branch="main")
    session.add_all([repo, Mode(name="goal", persona_prompt="p",
                                permission_mode="bypassPermissions")])
    session.commit()
    suites = iter([_red_suite(), _green_suite()])

    async def fake_suite(workspace, repo_name, test_cmds=None, **kw):
        return next(suites)

    monkeypatch.setattr("app.services.evidence.verify_suite", fake_suite)
    lm = _FakeLaneManager(session)
    bp = GoalBlueprint()
    ctx = _ctx(run, services={"thread_manager": lm},
               artifacts={"repo_row": repo, "workspace": "/ws", "test_cmds": None,
                          "thread_ids": [], "branch": "agent/r1-x",
                          "permissions": {"writable": True, "repos": []},
                          "develop_thread_id": "control-plane"})
    await bp._verify(ctx)
    fixers = [s for s in lm.spawned if s["persona"] == "fixer"]
    assert len(fixers) == 1
    assert fixers[0]["preserve_workspace"] is True  # re-stamp would wipe the impl
    assert "E501 line too long" in fixers[0]["prompt"]
    assert ctx.artifacts["fix_rounds"] == 1
    events = session.query(Event).filter_by(run_id="r1", type="test_run").all()
    assert len(events) == 2  # red + green re-gate both persisted


async def test_verify_raises_after_bounded_fix_rounds(session, make_user, monkeypatch):
    run = _run(session, make_user)
    repo = Repo(name="ServerApp", integration_branch="main")
    session.add_all([repo, Mode(name="goal", persona_prompt="p",
                                permission_mode="bypassPermissions")])
    session.commit()

    async def fake_suite(workspace, repo_name, test_cmds=None, **kw):
        return _red_suite()

    monkeypatch.setattr("app.services.evidence.verify_suite", fake_suite)
    lm = _FakeLaneManager(session)
    bp = GoalBlueprint()
    ctx = _ctx(run, services={"thread_manager": lm},
               artifacts={"repo_row": repo, "workspace": "/ws", "test_cmds": None,
                          "thread_ids": [], "branch": "agent/r1-x",
                          "permissions": {"writable": True, "repos": []},
                          "develop_thread_id": "control-plane"})
    with pytest.raises(RuntimeError, match="verification gate still red"):
        await bp._verify(ctx)
    fixers = [s for s in lm.spawned if s["persona"] == "fixer"]
    assert len(fixers) == MAX_FIX_ROUNDS  # bounded — never an infinite fix loop


# --------------------------------------------------------------- _ship
async def test_ship_commits_opens_pr_and_completes(session, make_user, monkeypatch):
    run = _run(session, make_user)
    repo = Repo(name="ServerApp", integration_branch="main")
    session.add(repo)
    session.commit()
    calls = {"commit": [], "open_pr": []}

    async def fake_commit(run_id, workspace, branch, message=None):
        calls["commit"].append((run_id, workspace, branch))
        return True

    async def fake_open_pr(run_id, repo_name, workspace):
        calls["open_pr"].append((run_id, repo_name, workspace))
        return SimpleNamespace(ado_pr_id=42)

    monkeypatch.setattr("app.services.delivery.commit_pending", fake_commit)
    monkeypatch.setattr("app.services.delivery.open_pr", fake_open_pr)
    lm = _FakeLaneManager(session)
    bp = GoalBlueprint()
    ctx = _ctx(run, services={"thread_manager": lm},
               artifacts={"repo_row": repo, "workspace": "/ws",
                          "branch": "agent/r1-ship-usage-stats",
                          "thread_ids": ["l1", "l2"],
                          "develop_thread_id": "l1",
                          "verify_suite": _green_suite(), "fix_rounds": 0})
    await bp._ship(ctx)
    assert calls["commit"] == [("r1", "/ws", "agent/r1-ship-usage-stats")]
    assert calls["open_pr"] == [("r1", "ServerApp", "/ws")]
    session.expire_all()
    shipped = session.get(Run, "r1")
    assert shipped.stage == RunStage.COMPLETED.value
    assert shipped.finished_at is not None
    assert "PR #42" in shipped.auto_summary
    assert session.query(TrajectorySummary).filter_by(run_id="r1").count() == 1
    assert sorted(lm.settled) == ["l1", "l2"]


# --------------------------------------------------------------- node-end finish
async def test_every_agentic_node_finishes_its_threads(session, make_user, monkeypatch):
    """C2/C3/C4: an idle worker lingers for its TTL holding a capacity slot
    AND the per-repo write lock, and the PR evidence gate needs a 'completed'
    thread — so every node finishes its threads the moment its await returns."""
    run = _run(session, make_user)
    repo = Repo(name="ServerApp", integration_branch="main")
    session.add_all([repo, Mode(name="goal", persona_prompt="p",
                                permission_mode="bypassPermissions")])
    session.commit()
    lm = _FakeLaneManager(session, {
        "researcher": "surface",
        "planner": json.dumps(PLAN_DICT),
        "critic": ["ok"] * CRITIQUE_ROUNDS,
        "reviser": [json.dumps(REVISED_PLAN_DICT)] * CRITIQUE_ROUNDS,
        "developer": "done",
    })
    bp = GoalBlueprint()
    ctx = _ctx(run, services={"thread_manager": lm},
               artifacts={"task": "t", "repo_row": repo, "thread_ids": [],
                          "permissions": {"writable": True, "repos": []},
                          "branch": "agent/r1-x", "workspace": "/ws",
                          "blast_radius": []})
    await bp._explore(ctx)
    await bp._plan(ctx)
    await bp._refine(ctx)
    await bp._present(ctx)
    await bp._develop(ctx)
    # 1 researcher + 1 planner + 3 critics + 3 revisers + 1 developer
    assert len(lm.finished) == 9
    assert lm.finished[-1] == ctx.artifacts["develop_thread_id"]


async def test_verify_finishes_fixer_each_round(session, make_user, monkeypatch):
    """The next fix round re-spawns a WRITABLE thread on the same repo — it
    only fits if the previous fixer released the write lock (finished)."""
    run = _run(session, make_user)
    repo = Repo(name="ServerApp", integration_branch="main")
    session.add_all([repo, Mode(name="goal", persona_prompt="p",
                                permission_mode="bypassPermissions")])
    session.commit()
    suites = iter([_red_suite(), _red_suite(), _green_suite()])

    async def fake_suite(workspace, repo_name, test_cmds=None, **kw):
        return next(suites)

    monkeypatch.setattr("app.services.evidence.verify_suite", fake_suite)
    lm = _FakeLaneManager(session, {"fixer": "fixed"})
    bp = GoalBlueprint()
    ctx = _ctx(run, services={"thread_manager": lm},
               artifacts={"repo_row": repo, "workspace": "/ws", "test_cmds": None,
                          "thread_ids": [], "branch": "agent/r1-x",
                          "permissions": {"writable": True, "repos": []},
                          "develop_thread_id": "control-plane"})
    await bp._verify(ctx)
    fixers = [s for s in lm.spawned if s["persona"] == "fixer"]
    assert len(fixers) == 2
    assert len(lm.finished) == 2  # each fixer finished before the next spawn


async def test_swarm_summary_labels_read_from_spawn_context(session, make_user):
    """Explorer labels come from each thread's OWN spawn context — if a spawn
    fails, the remaining labels still match their threads (no index drift)."""
    run = _run(session, make_user)
    repo = Repo(name="ServerApp", integration_branch="main")
    session.add(repo)
    session.commit()

    class _LossyLaneManager(_FakeLaneManager):
        async def spawn_many(self, run, specs, context_repos):
            # first explorer spawn fails (capacity) — the rest survive
            threads = []
            for spec in specs[1:]:
                threads.append(await self.spawn(
                    run, persona=spec["persona"], prompt=spec["prompt"],
                    persona_prompt=spec["persona_prompt"], writable_repo=None,
                    context_repos=context_repos))
            return threads

    lm = _LossyLaneManager(session, {"explorer": "findings"})
    bp = GoalBlueprint()
    ctx = _ctx(run, services={"thread_manager": lm},
               artifacts={"task": "t", "repo_row": repo, "thread_ids": [],
                          "fanout": 3})
    await bp._explore(ctx)
    summary = ctx.artifacts["explore_summary"]
    assert summary.count("--- explorer") == 2
    # surviving threads are angles 2+3 — labels must say so, not "angle 1"
    assert "verification: existing tests" in summary
    assert "dependencies & integrations" in summary
    assert "change surface" not in summary


async def test_await_thread_wedged_raises(session, make_user):
    """An unattended pipeline can't poll a wedged thread forever — bound the
    wait and fail the run with the reason."""
    run = _run(session, make_user)
    session.add(Thread(id="l-stuck", run_id=run.id, persona="developer",
                       status="running"))
    session.commit()
    with pytest.raises(RuntimeError, match="wedged"):
        await _await_thread("l-stuck", poll_seconds=0.01, max_wait_s=0.03)
