
import pytest
from collegium_contracts import RunStage

from app.db.models.mode import Mode
from app.db.models.repo import Repo
from app.db.models.run import Run
from app.db.models.thread import Thread
from app.db.models.trajectory import TrajectorySummary
from app.orchestrator.blueprints.ask import AskBlueprint
from app.orchestrator.blueprints.base import BlueprintContext


class _FakeLaneManager:
    def __init__(self, thread):
        self._thread = thread
        self.spawned = []
        self.settled = []

    async def spawn(self, run, persona, prompt, persona_prompt, writable_repo, context_repos,
                    resume_session=False, resume_from_thread_id=None):
        self.spawned.append({
            "persona": persona, "prompt": prompt, "persona_prompt": persona_prompt,
            "writable_repo": writable_repo, "context_repos": context_repos,
        })
        return self._thread

    async def settle_cost(self, thread_id):
        self.settled.append(thread_id)


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


async def test_hydrate_mention_sets_context_in_order(session, make_user):
    """A turn-1 @mention pins the target (first mention) and mounts every
    mentioned repo as read-only context, in first-appearance order."""
    u = make_user("alice", role="member", status="active")
    run = Run(id="r1", created_by=u.id, mode="ask", stage="queued", title="compare `@ClientApp` and `@ServerApp`")
    session.add_all([
        Repo(name="ServerApp", integration_branch="main"),
        Repo(name="ClientApp", integration_branch="main"),
    ]); session.commit()
    bp = AskBlueprint()
    ctx = _ctx(run)
    await bp._hydrate(ctx)
    assert ctx.artifacts["repo_row"].name == "ClientApp"  # first mention is the target
    assert [r.name for r in ctx.artifacts["context_repos"]] == ["ClientApp", "ServerApp"]


async def test_hydrate_unknown_mention_raises(session, make_user):
    u = make_user("alice", role="member", status="active")
    run = Run(id="r1", created_by=u.id, mode="ask", stage="queued", title="check `@GhostRepo`")
    session.add(Repo(name="ServerApp", integration_branch="main")); session.commit()
    bp = AskBlueprint()
    ctx = _ctx(run)
    with pytest.raises(RuntimeError, match="repo 'GhostRepo' not registered"):
        await bp._hydrate(ctx)


async def test_hydrate_no_repo_no_mention_is_general_assistant(session, make_user):
    """No default repo: a repo-less ask run is general-assistant chat — the
    agent always responds. The hydrate sets repo_row=None and context_repos=[]
    so _investigate spawns a worker with no file access and a general-assistant
    persona. The old `or "ServerApp"` fallback hid the fleet from the agent;
    the 422 we briefly tried blocked the user from chatting at all."""
    u = make_user("alice", role="member", status="active")
    run = Run(id="r1", created_by=u.id, mode="ask", stage="queued", title="hello")
    repo = Repo(name="ServerApp", integration_branch="main")
    session.add_all([run, repo]); session.commit()
    bp = AskBlueprint()
    ctx = _ctx(run)
    await bp._hydrate(ctx)
    assert ctx.artifacts["repo_row"] is None
    assert ctx.artifacts["context_repos"] == []


async def test_hydrate_missing_repo_raises(session, make_user):
    u = make_user("alice", role="member", status="active")
    run = Run(id="r1", created_by=u.id, mode="ask", stage="queued", repo="Ghost", title="t")
    session.add(run); session.commit()
    bp = AskBlueprint()
    ctx = _ctx(run)
    with pytest.raises(RuntimeError, match="repo 'Ghost' not registered"):
        await bp._hydrate(ctx)


# --------------------------------------------------------------- _await_thread
async def test_await_thread_returns_when_completed(session, make_user, monkeypatch):
    u = make_user("alice", role="member", status="active")
    run = Run(id="r1", created_by=u.id, mode="ask", stage="investigating")
    thread = Thread(id="l1", run_id="r1", persona="researcher", status="completed")
    session.add_all([run, thread]); session.commit()
    bp = AskBlueprint()
    # Should return immediately (status already terminal); no sleep invoked.
    await bp._await_thread("l1", poll_seconds=0)


async def test_await_thread_polls_until_terminal(session, make_user, monkeypatch):
    u = make_user("alice", role="member", status="active")
    run = Run(id="r1", created_by=u.id, mode="ask", stage="investigating")
    thread = Thread(id="l1", run_id="r1", persona="researcher", status="running")
    session.add_all([run, thread]); session.commit()

    transitions = iter(["running", "running", "idle"])

    async def fake_sleep(seconds):
        # advance the thread status on each poll
        session.expire_all()
        ln = session.get(Thread, "l1")
        try:
            ln.status = next(transitions)
            session.commit()
        except StopIteration:
            ln.status = "idle"
            session.commit()

    monkeypatch.setattr("app.orchestrator.blueprints.ask.asyncio.sleep", fake_sleep)
    bp = AskBlueprint()
    await bp._await_thread("l1", poll_seconds=0)


async def test_await_thread_missing_thread_treated_as_failed(session, make_user):
    u = make_user("alice", role="member", status="active")
    run = Run(id="r1", created_by=u.id, mode="ask", stage="investigating")
    session.add(run); session.commit()
    bp = AskBlueprint()
    # No thread row → status "failed" → terminal → returns immediately.
    await bp._await_thread("ghost-thread", poll_seconds=0)


# --------------------------------------------------------------- _investigate
async def test_investigate_spawns_thread_and_awaits(session, make_user, monkeypatch):
    u = make_user("alice", role="member", status="active")
    run = Run(id="r1", created_by=u.id, mode="ask", stage="investigating", title="explore")
    repo = Repo(name="ServerApp", integration_branch="main")
    mode = Mode(name="ask", autonomy_default="supervised", enabled=True, persona_prompt="You are a researcher.")
    thread = Thread(id="l1", run_id="r1", persona="researcher", status="completed")
    session.add_all([run, repo, mode, thread]); session.commit()

    lm = _FakeLaneManager(thread)
    bp = AskBlueprint()
    ctx = _ctx(run, services={"thread_manager": lm}, artifacts={"repo_row": repo, "guidebook": "GB", "task": "explore"})

    # Short-circuit the await so we don't poll.
    async def fake_await(self, thread_id, poll_seconds=2.0):
        return None
    monkeypatch.setattr(AskBlueprint, "_await_thread", fake_await)

    await bp._investigate(ctx)
    assert ctx.artifacts["thread_id"] == "l1"
    assert lm.spawned[0]["persona"] == "researcher"
    assert "You are a researcher." in lm.spawned[0]["persona_prompt"]
    assert "GB" in lm.spawned[0]["persona_prompt"]
    assert lm.spawned[0]["prompt"] == "explore"


async def test_investigate_uses_run_title_when_no_task_artifact(session, make_user, monkeypatch):
    u = make_user("alice", role="member", status="active")
    run = Run(id="r1", created_by=u.id, mode="ask", stage="investigating", title="my title")
    repo = Repo(name="ServerApp", integration_branch="main")
    thread = Thread(id="l1", run_id="r1", persona="researcher", status="completed")
    session.add_all([run, repo, thread]); session.commit()

    lm = _FakeLaneManager(thread)
    bp = AskBlueprint()
    ctx = _ctx(run, services={"thread_manager": lm}, artifacts={"repo_row": repo, "guidebook": ""})

    async def fake_await(self, thread_id, poll_seconds=2.0):
        return None
    monkeypatch.setattr(AskBlueprint, "_await_thread", fake_await)

    await bp._investigate(ctx)
    assert lm.spawned[0]["prompt"] == "my title"


async def test_investigate_without_mode_row(session, make_user, monkeypatch):
    u = make_user("alice", role="member", status="active")
    run = Run(id="r1", created_by=u.id, mode="ask", stage="investigating", title="t")
    repo = Repo(name="ServerApp", integration_branch="main")
    thread = Thread(id="l1", run_id="r1", persona="researcher", status="completed")
    session.add_all([run, repo, thread]); session.commit()

    lm = _FakeLaneManager(thread)
    bp = AskBlueprint()
    ctx = _ctx(run, services={"thread_manager": lm}, artifacts={"repo_row": repo, "guidebook": ""})

    async def fake_await(self, thread_id, poll_seconds=2.0):
        return None
    monkeypatch.setattr(AskBlueprint, "_await_thread", fake_await)

    await bp._investigate(ctx)
    assert "Repo guidebook" in lm.spawned[0]["persona_prompt"]


async def test_investigate_no_repo_spawns_general_assistant(session, make_user, monkeypatch):
    """No repo mentioned → general-assistant mode: the worker spawns with no
    context repos and a persona that tells the agent it has no file access.
    The agent still responds — it answers from training knowledge. An
    @mention in a later turn (remount) opts into file access."""
    u = make_user("alice", role="member", status="active")
    run = Run(id="r1", created_by=u.id, mode="ask", stage="investigating", title="hello")
    mode = Mode(name="ask", autonomy_default="supervised", enabled=True, persona_prompt="You are a researcher.")
    thread = Thread(id="l1", run_id="r1", persona="researcher", status="completed")
    session.add_all([run, mode, thread]); session.commit()

    lm = _FakeLaneManager(thread)
    bp = AskBlueprint()
    ctx = _ctx(run, services={"thread_manager": lm},
               artifacts={"repo_row": None, "context_repos": [], "guidebook": "", "task": "hello"})

    async def fake_await(self, thread_id, poll_seconds=2.0):
        return None
    monkeypatch.setattr(AskBlueprint, "_await_thread", fake_await)

    await bp._investigate(ctx)
    assert ctx.artifacts["thread_id"] == "l1"
    assert lm.spawned[0]["context_repos"] == []
    assert lm.spawned[0]["writable_repo"] is None
    assert "general-assistant" in lm.spawned[0]["persona_prompt"]
    assert "No repo is mounted" in lm.spawned[0]["persona_prompt"]
    assert lm.spawned[0]["prompt"] == "hello"


# --------------------------------------------------------------- _complete
async def test_complete_writes_trajectory_and_settles(session, make_user):
    u = make_user("alice", role="member", status="active")
    run = Run(id="r1", created_by=u.id, mode="ask", stage="investigating", title="t", repo="ServerApp")
    thread = Thread(id="l1", run_id="r1", persona="researcher", status="completed")
    session.add_all([run, thread]); session.commit()
    lm = _FakeLaneManager(thread)
    bp = AskBlueprint()
    ctx = _ctx(run, services={"thread_manager": lm}, artifacts={"thread_id": "l1"})
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
    thread = Thread(id="l1", run_id="r1", persona="researcher", status="completed")
    session.add_all([run, thread]); session.commit()
    lm = _FakeLaneManager(thread)
    bp = AskBlueprint()
    ctx = _ctx(run, services={"thread_manager": lm}, artifacts={"thread_id": "l1"})
    await bp._complete(ctx)
    session.expire_all()
    assert session.query(TrajectorySummary).one().summary == "custom summary here"


async def test_complete_failed_thread_transitions_run_failed(session, make_user):
    u = make_user("alice", role="member", status="active")
    run = Run(id="r1", created_by=u.id, mode="ask", stage="investigating", title="t")
    thread = Thread(id="l1", run_id="r1", persona="researcher", status="failed")
    session.add_all([run, thread]); session.commit()
    lm = _FakeLaneManager(thread)
    bp = AskBlueprint()
    ctx = _ctx(run, services={"thread_manager": lm}, artifacts={"thread_id": "l1"})
    await bp._complete(ctx)
    assert ctx.run.stage == RunStage.FAILED.value
    session.expire_all()
    assert session.query(TrajectorySummary).filter_by(run_id="r1").count() == 0
    assert lm.settled == []


async def test_complete_without_thread_id(session, make_user):
    u = make_user("alice", role="member", status="active")
    run = Run(id="r1", created_by=u.id, mode="ask", stage="investigating", title="t")
    session.add(run); session.commit()
    lm = _FakeLaneManager(None)
    bp = AskBlueprint()
    ctx = _ctx(run, services={"thread_manager": lm}, artifacts={})
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
