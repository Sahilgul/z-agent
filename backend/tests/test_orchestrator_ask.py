import asyncio

import pytest
from zagent_contracts import RunStage

from app.db.models.lane import Lane
from app.db.models.mode import Mode
from app.db.models.repo import Repo
from app.db.models.run import Run
from app.db.models.trajectory import TrajectorySummary
from app.orchestrator.blueprints.ask import AskBlueprint
from app.orchestrator.blueprints.base import BlueprintContext


class _FakeLaneManager:
    def __init__(self, lane):
        self._lane = lane
        self.spawned = []
        self.settled = []

    async def spawn(self, run, persona, prompt, persona_prompt, writable_repo, context_repos):
        self.spawned.append({"persona": persona, "prompt": prompt, "persona_prompt": persona_prompt})
        return self._lane

    async def settle_cost(self, lane_id):
        self.settled.append(lane_id)


def _ctx(run, services=None, artifacts=None):
    return BlueprintContext(run=run, services=services or {}, artifacts=artifacts or {})


# --------------------------------------------------------------- _hydrate
async def test_hydrate_resolves_repo_and_guidebook(session, make_user):
    u = make_user("alice", role="member", status="active")
    run = Run(id="r1", created_by=u.id, mode="ask", stage="queued", repo="ServerApp", title="t")
    repo = Repo(name="ServerApp", integration_branch="main")
    session.add_all([run, repo]); session.commit()
    bp = AskBlueprint()
    ctx = _ctx(run)
    await bp._hydrate(ctx)
    assert ctx.artifacts["repo_row"].name == "ServerApp"
    assert "guidebook" in ctx.artifacts


async def test_hydrate_uses_artifacts_repo_over_run_repo(session, make_user):
    u = make_user("alice", role="member", status="active")
    run = Run(id="r1", created_by=u.id, mode="ask", stage="queued", repo="Other", title="t")
    repo = Repo(name="ServerApp", integration_branch="main")
    session.add_all([run, repo]); session.commit()
    bp = AskBlueprint()
    ctx = _ctx(run, artifacts={"repo": "ServerApp"})
    await bp._hydrate(ctx)
    assert ctx.artifacts["repo_row"].name == "ServerApp"


async def test_hydrate_defaults_to_serverapp(session, make_user):
    u = make_user("alice", role="member", status="active")
    run = Run(id="r1", created_by=u.id, mode="ask", stage="queued", title="t")
    repo = Repo(name="ServerApp", integration_branch="main")
    session.add_all([run, repo]); session.commit()
    bp = AskBlueprint()
    ctx = _ctx(run)
    await bp._hydrate(ctx)
    assert ctx.artifacts["repo_row"].name == "ServerApp"


async def test_hydrate_missing_repo_raises(session, make_user):
    u = make_user("alice", role="member", status="active")
    run = Run(id="r1", created_by=u.id, mode="ask", stage="queued", repo="Ghost", title="t")
    session.add(run); session.commit()
    bp = AskBlueprint()
    ctx = _ctx(run)
    with pytest.raises(RuntimeError, match="repo 'Ghost' not registered"):
        await bp._hydrate(ctx)


# --------------------------------------------------------------- _await_lane
async def test_await_lane_returns_when_completed(session, make_user, monkeypatch):
    u = make_user("alice", role="member", status="active")
    run = Run(id="r1", created_by=u.id, mode="ask", stage="investigating")
    lane = Lane(id="l1", run_id="r1", persona="researcher", status="completed")
    session.add_all([run, lane]); session.commit()
    bp = AskBlueprint()
    # Should return immediately (status already terminal); no sleep invoked.
    await bp._await_lane("l1", poll_seconds=0)


async def test_await_lane_polls_until_terminal(session, make_user, monkeypatch):
    u = make_user("alice", role="member", status="active")
    run = Run(id="r1", created_by=u.id, mode="ask", stage="investigating")
    lane = Lane(id="l1", run_id="r1", persona="researcher", status="running")
    session.add_all([run, lane]); session.commit()

    transitions = iter(["running", "running", "idle"])

    real_get = type(bp) if False else None

    async def fake_sleep(seconds):
        # advance the lane status on each poll
        session.expire_all()
        ln = session.get(Lane, "l1")
        try:
            ln.status = next(transitions)
            session.commit()
        except StopIteration:
            ln.status = "idle"
            session.commit()

    monkeypatch.setattr("app.orchestrator.blueprints.ask.asyncio.sleep", fake_sleep)
    bp = AskBlueprint()
    await bp._await_lane("l1", poll_seconds=0)


async def test_await_lane_missing_lane_treated_as_failed(session, make_user):
    u = make_user("alice", role="member", status="active")
    run = Run(id="r1", created_by=u.id, mode="ask", stage="investigating")
    session.add(run); session.commit()
    bp = AskBlueprint()
    # No lane row → status "failed" → terminal → returns immediately.
    await bp._await_lane("ghost-lane", poll_seconds=0)


# --------------------------------------------------------------- _investigate
async def test_investigate_spawns_lane_and_awaits(session, make_user, monkeypatch):
    u = make_user("alice", role="member", status="active")
    run = Run(id="r1", created_by=u.id, mode="ask", stage="investigating", title="explore")
    repo = Repo(name="ServerApp", integration_branch="main")
    mode = Mode(name="ask", autonomy_default="supervised", enabled=True, persona_prompt="You are a researcher.")
    lane = Lane(id="l1", run_id="r1", persona="researcher", status="completed")
    session.add_all([run, repo, mode, lane]); session.commit()

    lm = _FakeLaneManager(lane)
    bp = AskBlueprint()
    ctx = _ctx(run, services={"lane_manager": lm}, artifacts={"repo_row": repo, "guidebook": "GB", "task": "explore"})

    # Short-circuit the await so we don't poll.
    async def fake_await(self, lane_id, poll_seconds=2.0):
        return None
    monkeypatch.setattr(AskBlueprint, "_await_lane", fake_await)

    await bp._investigate(ctx)
    assert ctx.artifacts["lane_id"] == "l1"
    assert lm.spawned[0]["persona"] == "researcher"
    assert "You are a researcher." in lm.spawned[0]["persona_prompt"]
    assert "GB" in lm.spawned[0]["persona_prompt"]
    assert lm.spawned[0]["prompt"] == "explore"


async def test_investigate_uses_run_title_when_no_task_artifact(session, make_user, monkeypatch):
    u = make_user("alice", role="member", status="active")
    run = Run(id="r1", created_by=u.id, mode="ask", stage="investigating", title="my title")
    repo = Repo(name="ServerApp", integration_branch="main")
    lane = Lane(id="l1", run_id="r1", persona="researcher", status="completed")
    session.add_all([run, repo, lane]); session.commit()

    lm = _FakeLaneManager(lane)
    bp = AskBlueprint()
    ctx = _ctx(run, services={"lane_manager": lm}, artifacts={"repo_row": repo, "guidebook": ""})

    async def fake_await(self, lane_id, poll_seconds=2.0):
        return None
    monkeypatch.setattr(AskBlueprint, "_await_lane", fake_await)

    await bp._investigate(ctx)
    assert lm.spawned[0]["prompt"] == "my title"


async def test_investigate_without_mode_row(session, make_user, monkeypatch):
    u = make_user("alice", role="member", status="active")
    run = Run(id="r1", created_by=u.id, mode="ask", stage="investigating", title="t")
    repo = Repo(name="ServerApp", integration_branch="main")
    lane = Lane(id="l1", run_id="r1", persona="researcher", status="completed")
    session.add_all([run, repo, lane]); session.commit()

    lm = _FakeLaneManager(lane)
    bp = AskBlueprint()
    ctx = _ctx(run, services={"lane_manager": lm}, artifacts={"repo_row": repo, "guidebook": ""})

    async def fake_await(self, lane_id, poll_seconds=2.0):
        return None
    monkeypatch.setattr(AskBlueprint, "_await_lane", fake_await)

    await bp._investigate(ctx)
    assert "Repo guidebook" in lm.spawned[0]["persona_prompt"]


# --------------------------------------------------------------- _complete
async def test_complete_writes_trajectory_and_settles(session, make_user):
    u = make_user("alice", role="member", status="active")
    run = Run(id="r1", created_by=u.id, mode="ask", stage="investigating", title="t", repo="ServerApp")
    lane = Lane(id="l1", run_id="r1", persona="researcher", status="completed")
    session.add_all([run, lane]); session.commit()
    lm = _FakeLaneManager(lane)
    bp = AskBlueprint()
    ctx = _ctx(run, services={"lane_manager": lm}, artifacts={"lane_id": "l1"})
    await bp._complete(ctx)
    finished_at = ctx.run.finished_at
    session.expire_all()
    assert session.query(TrajectorySummary).filter_by(run_id="r1").one().summary.startswith("Ask run")
    assert finished_at is not None
    assert lm.settled == ["l1"]


async def test_complete_uses_auto_summary_when_present(session, make_user):
    u = make_user("alice", role="member", status="active")
    run = Run(id="r1", created_by=u.id, mode="ask", stage="investigating", title="t",
              auto_summary="custom summary here")
    lane = Lane(id="l1", run_id="r1", persona="researcher", status="completed")
    session.add_all([run, lane]); session.commit()
    lm = _FakeLaneManager(lane)
    bp = AskBlueprint()
    ctx = _ctx(run, services={"lane_manager": lm}, artifacts={"lane_id": "l1"})
    await bp._complete(ctx)
    session.expire_all()
    assert session.query(TrajectorySummary).one().summary == "custom summary here"


async def test_complete_failed_lane_transitions_run_failed(session, make_user):
    u = make_user("alice", role="member", status="active")
    run = Run(id="r1", created_by=u.id, mode="ask", stage="investigating", title="t")
    lane = Lane(id="l1", run_id="r1", persona="researcher", status="failed")
    session.add_all([run, lane]); session.commit()
    lm = _FakeLaneManager(lane)
    bp = AskBlueprint()
    ctx = _ctx(run, services={"lane_manager": lm}, artifacts={"lane_id": "l1"})
    await bp._complete(ctx)
    assert ctx.run.stage == RunStage.FAILED.value
    session.expire_all()
    assert session.query(TrajectorySummary).filter_by(run_id="r1").count() == 0
    assert lm.settled == []


async def test_complete_without_lane_id(session, make_user):
    u = make_user("alice", role="member", status="active")
    run = Run(id="r1", created_by=u.id, mode="ask", stage="investigating", title="t")
    session.add(run); session.commit()
    lm = _FakeLaneManager(None)
    bp = AskBlueprint()
    ctx = _ctx(run, services={"lane_manager": lm}, artifacts={})
    await bp._complete(ctx)
    assert ctx.run.finished_at is not None
    assert lm.settled == []


# --------------------------------------------------------------- nodes
def test_nodes_returns_three_stages():
    bp = AskBlueprint()
    nodes = bp.nodes()
    assert [n.name for n in nodes] == ["hydrate", "investigate", "complete"]
    assert nodes[0].deterministic is True
    assert nodes[1].deterministic is False
    assert nodes[2].deterministic is True
    assert nodes[0].stage == RunStage.PROVISIONING
    assert nodes[1].stage == RunStage.INVESTIGATING
    assert nodes[2].stage == RunStage.COMPLETED
