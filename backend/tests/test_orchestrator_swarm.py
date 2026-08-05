"""Width-swarm blueprint tests: hydrate/decompose/fanout/
collect/synthesize/complete — all threads read-only, Lead decomposes, collect
reads the event stream."""
import json

import pytest

from app.db.models.event import Event
from app.db.models.thread import Thread
from app.db.models.mode import Mode
from app.db.models.repo import Repo
from app.db.models.run import Run
from app.db.models.trajectory import TrajectorySummary
from app.orchestrator.blueprints.base import BlueprintContext
from app.orchestrator.blueprints.swarm import SwarmBlueprint, _await_thread
from app.orchestrator.mode_engine import blueprint_for
from app.db.base import get_session

pytestmark = pytest.mark.asyncio


class _FakeLaneMgr:
    """Spawns DB-backed threads at a terminal status so _await_thread returns
    immediately; records spawn/spawn_many calls for assertions."""

    def __init__(self, session, decompose_reply: str | None = None,
                 synthesis_reply: str | None = None):
        self.session = session
        self.decompose_reply = decompose_reply
        self.synthesis_reply = synthesis_reply
        self.spawned: list[dict] = []
        self.spawn_many_calls: list[dict] = {}
        self.settled: list[str] = []

    def _thread(self, run_id, persona, status="idle"):
        thread = Thread(id=f"thread-{persona}-{len(self.spawned)}", run_id=run_id,
                    persona=persona, status=status)
        self.session.add(thread)
        self.session.commit()
        return thread

    async def spawn(self, run, persona, prompt, persona_prompt, writable_repo, context_repos,
                    resume_session=False, resume_from_thread_id=None):
        self.spawned.append({"persona": persona, "prompt": prompt,
                             "persona_prompt": persona_prompt})
        thread = self._thread(run.id, persona)
        if len(self.spawned) == 1 and self.decompose_reply is not None:
            reply = self.decompose_reply
        else:
            reply = self.synthesis_reply
        if reply is not None:
            self.session.add(Event(run_id=run.id, thread_id=thread.id, seq=0,
                                   type="message", title="final",
                                   payload={"text": reply}))
            self.session.commit()
        return thread

    async def spawn_many(self, run, specs, context_repos, queue_poll_seconds=5.0):
        self.spawn_many_calls["specs"] = specs
        threads = []
        for i, spec in enumerate(specs):
            thread = Thread(id=f"explorer-{i}", run_id=run.id, persona=spec["persona"],
                        status="idle")
            self.session.add(thread)
            self.session.commit()
            self.session.add(Event(run_id=run.id, thread_id=thread.id, seq=0,
                                   type="notebook", title="nb",
                                   payload={"findings": [f"finding {i}"],
                                            "confidence": "high"}))
            self.session.commit()
            threads.append(thread)
        return threads

    async def settle_cost(self, thread_id):
        self.settled.append(thread_id)
        return 0.0


def _ctx(run, services=None, artifacts=None):
    return BlueprintContext(run=run, services=services or {}, artifacts=artifacts or {})


def _seed(session, make_user, fanout_task="map the billing flow"):
    u = make_user("alice", role="member", status="active")
    run = Run(id="r1", created_by=u.id, mode="agent-rnd", stage="queued",
              repo="ServerApp", title=fanout_task)
    repo = Repo(name="ServerApp", integration_branch="main", status="ready")
    mode = Mode(name="agent-rnd", persona_prompt="You lead swarms.",
                permission_mode="default", topology="width-swarm",
                permissions={"writable": False, "repos": []})
    session.add_all([run, repo, mode])
    session.commit()
    return run


DECOMPOSE_JSON = json.dumps({
    "slices": [
        {"title": "webhook leg", "prompt": "trace the webhook ingress", "repo": None, "angle": "ingress"},
        {"title": "billing engine leg", "prompt": "trace Billing-Engine scoring", "repo": None, "angle": "engine"},
    ],
    "counter_proposal": None,
    "rationale": "two legs suffice",
})


# --------------------------------------------------------------- registry
def test_mode_engine_resolves_agent_rnd_via_topology(session):
    session.add(Mode(name="agent-rnd", persona_prompt="p", permission_mode="default",
                     topology="width-swarm", permissions={"writable": False, "repos": []}))
    session.commit()
    bp = blueprint_for("agent-rnd")
    assert isinstance(bp, SwarmBlueprint)
    assert bp.name == "width-swarm"


def test_nodes_in_order():
    names = [n.name for n in SwarmBlueprint().nodes()]
    assert names == ["hydrate", "decompose", "fanout", "collect", "synthesize", "complete"]
    determinism = {n.name: n.deterministic for n in SwarmBlueprint().nodes()}
    assert determinism["fanout"] is True and determinism["collect"] is True
    assert determinism["decompose"] is False and determinism["synthesize"] is False


# --------------------------------------------------------------- hydrate
async def test_hydrate_defaults_and_resolves_repo(session, make_user):
    run = _seed(session, make_user)
    ctx = _ctx(run, artifacts={"task": "map the billing flow"})
    await SwarmBlueprint()._hydrate(ctx)
    assert ctx.artifacts["repo_row"].name == "ServerApp"
    assert ctx.artifacts["requested_fanout"] == 3  # DEFAULT_FANOUT
    assert ctx.artifacts["context_repos"][0].name == "ServerApp"


async def test_hydrate_parses_spawn_count_from_task(session, make_user):
    run = _seed(session, make_user, fanout_task="spawn 5 explorers on the fleet")
    ctx = _ctx(run, artifacts={"task": "spawn 5 explorers on the fleet"})
    await SwarmBlueprint()._hydrate(ctx)
    assert ctx.artifacts["requested_fanout"] == 5


async def test_hydrate_clamps_to_global_cap_and_says_so(session, make_user, monkeypatch):
    from app.orchestrator.blueprints import swarm as swarm_mod
    monkeypatch.setattr(swarm_mod.get_settings(), "global_thread_cap", 4)
    run = _seed(session, make_user)
    relay_msgs = []

    class _Relay:
        async def publish_note(self, run_id, text):
            # L-22: swarm now uses publish_note (run-scoped) instead of
            # misusing publish_thread_status with a fake thread id.
            relay_msgs.append((run_id, text))

    ctx = _ctx(run, services={"relay": _Relay()},
               artifacts={"task": "t", "fanout": 10})
    await SwarmBlueprint()._hydrate(ctx)
    assert ctx.artifacts["requested_fanout"] == 4
    # L-22: the note is run-scoped — assert it carried the run id and the
    # queued sentence (was: fake thread id "swarm" + sentence status).
    assert relay_msgs and relay_msgs[0][0] == run.id and "queued" in relay_msgs[0][1]


async def test_hydrate_unknown_repo_raises(session, make_user):
    run = _seed(session, make_user)
    ctx = _ctx(run, artifacts={"task": "t", "repo": "Ghost"})
    with pytest.raises(RuntimeError, match="not registered"):
        await SwarmBlueprint()._hydrate(ctx)


async def test_hydrate_fleet_wide_when_no_target(session, make_user):
    u = make_user("alice", role="member", status="active")
    run = Run(id="r1", created_by=u.id, mode="agent-rnd", stage="queued", title="t")
    session.add_all([run, Repo(name="A", integration_branch="main", status="ready"),
                     Repo(name="B", integration_branch="main", status="ready"),
                     Mode(name="agent-rnd", persona_prompt="p", topology="width-swarm")])
    session.commit()
    ctx = _ctx(run, artifacts={"task": "t"})
    await SwarmBlueprint()._hydrate(ctx)
    assert ctx.artifacts["repo_row"] is None
    assert {r.name for r in ctx.artifacts["context_repos"]} == {"A", "B"}


# --------------------------------------------------------------- decompose
async def test_decompose_parses_lead_output(session, make_user):
    run = _seed(session, make_user)
    lm = _FakeLaneMgr(session, decompose_reply=f"```json\n{DECOMPOSE_JSON}\n```")
    ctx = _ctx(run, services={"thread_manager": lm},
               artifacts={"task": "map billing", "requested_fanout": 2,
                          "repo_row": session.query(Repo).one(),
                          "context_repos": [], "mode_persona": "lead"})
    await SwarmBlueprint()._decompose(ctx)
    decomp = ctx.artifacts["decomposition"]
    assert len(decomp.slices) == 2
    assert decomp.slices[0].angle == "ingress"
    assert lm.spawned[0]["persona"] == "lead"


async def test_decompose_unparsable_degrades_to_single_slice(session, make_user):
    run = _seed(session, make_user)
    lm = _FakeLaneMgr(session, decompose_reply="I cannot decompose this, sorry.")
    ctx = _ctx(run, services={"thread_manager": lm},
               artifacts={"task": "the whole task", "requested_fanout": 3,
                          "repo_row": session.query(Repo).one(),
                          "context_repos": [], "mode_persona": "lead"})
    await SwarmBlueprint()._decompose(ctx)
    decomp = ctx.artifacts["decomposition"]
    assert len(decomp.slices) == 1
    assert decomp.slices[0].prompt == "the whole task"
    assert "fell back" in decomp.rationale


# --------------------------------------------------------------- fanout + collect
async def test_fanout_spawns_one_read_only_thread_per_slice(session, make_user):
    from zagent_contracts import Decomposition
    run = _seed(session, make_user)
    lm = _FakeLaneMgr(session)
    decomp = Decomposition.model_validate_json(DECOMPOSE_JSON)
    ctx = _ctx(run, services={"thread_manager": lm},
               artifacts={"decomposition": decomp, "context_repos": [],
                          "mode_persona": "lead"})
    await SwarmBlueprint()._fanout(ctx)
    specs = lm.spawn_many_calls["specs"]
    assert [s["persona"] for s in specs] == ["explorer", "explorer"]
    assert specs[0]["prompt"] == "trace the webhook ingress"
    assert "READ-ONLY" in specs[0]["persona_prompt"]
    assert ctx.artifacts["explorer_thread_ids"] == ["explorer-0", "explorer-1"]
    assert ctx.artifacts["fanout_shortfall"] == 0


async def test_collect_gathers_notebooks_from_events(session, make_user):
    run = _seed(session, make_user)
    lm = _FakeLaneMgr(session)
    # two idle threads with notebook events, as spawn_many would leave them
    for i in range(2):
        session.add(Thread(id=f"explorer-{i}", run_id=run.id, persona="explorer", status="idle"))
        session.add(Event(run_id=run.id, thread_id=f"explorer-{i}", seq=0, type="notebook",
                          title="nb", payload={"findings": [f"f{i}"]}))
    session.commit()
    ctx = _ctx(run, artifacts={"explorer_thread_ids": ["explorer-0", "explorer-1"]})
    await SwarmBlueprint()._collect(ctx)
    notebooks = ctx.artifacts["notebooks"]
    assert notebooks[0]["notebook"]["findings"] == ["f0"]
    assert notebooks[1]["notebook"]["findings"] == ["f1"]


# --------------------------------------------------------------- synthesize
async def test_synthesize_sets_auto_summary_with_counter_proposal(session, make_user):
    from zagent_contracts import Decomposition
    run = _seed(session, make_user)
    lm = _FakeLaneMgr(session, synthesis_reply="Billing flows through the engine nightly.")
    decomp = Decomposition(slices=[{"title": "a", "prompt": "p"}],
                           counter_proposal=1, rationale="two would duplicate")
    ctx = _ctx(run, services={"thread_manager": lm},
               artifacts={"task": "t", "notebooks": [{"thread_id": "x", "notebook": {"findings": ["f"]}}],
                          "context_repos": [], "mode_persona": "lead",
                          "decomposition": decomp, "fanout_shortfall": 0})
    await SwarmBlueprint()._synthesize(ctx)
    session.expire_all()
    summary = session.get(Run, "r1").auto_summary
    assert "Billing flows" in summary
    assert "counter-proposed 1" in summary


async def test_synthesize_empty_swarm_does_not_spawn(session, make_user):
    from zagent_contracts import Decomposition
    run = _seed(session, make_user)
    lm = _FakeLaneMgr(session)
    ctx = _ctx(run, services={"thread_manager": lm},
               artifacts={"task": "t", "notebooks": [], "context_repos": [],
                          "mode_persona": "lead",
                          "decomposition": Decomposition(slices=[]), "fanout_shortfall": 2})
    await SwarmBlueprint()._synthesize(ctx)
    assert lm.spawned == []
    session.expire_all()
    assert "no threads" in session.get(Run, "r1").auto_summary


# --------------------------------------------------------------- complete
async def test_complete_all_failed_marks_run_failed(session, make_user):
    run = _seed(session, make_user)
    session.add(Thread(id="e0", run_id=run.id, persona="explorer", status="failed"))
    session.commit()
    ctx = _ctx(run, services={"thread_manager": _FakeLaneMgr(session)},
               artifacts={"explorer_thread_ids": ["e0"]})
    await SwarmBlueprint()._complete(ctx)
    session.expire_all()
    assert session.get(Run, "r1").stage == "failed"


async def test_complete_writes_trajectories_and_settles(session, make_user):
    run = _seed(session, make_user)
    session.add_all([Thread(id="e0", run_id=run.id, persona="explorer", status="idle"),
                     Thread(id="e1", run_id=run.id, persona="explorer", status="failed")])
    session.commit()
    lm = _FakeLaneMgr(session)
    ctx = _ctx(run, services={"thread_manager": lm},
               artifacts={"explorer_thread_ids": ["e0", "e1"]})
    await SwarmBlueprint()._complete(ctx)
    session.expire_all()
    assert session.query(TrajectorySummary).filter_by(run_id="r1").count() == 2
    assert sorted(lm.settled) == ["e0", "e1"]
    assert "1 explorer thread(s) failed" in session.get(Run, "r1").auto_summary


# --------------------------------------------------------------- full chain
async def test_execute_full_blueprint(session, make_user):
    run = _seed(session, make_user)
    lm = _FakeLaneMgr(session,
                      decompose_reply=f"```json\n{DECOMPOSE_JSON}\n```",
                      synthesis_reply="The billing flow has two legs.")
    ctx = _ctx(run, services={"thread_manager": lm}, artifacts={"task": "map billing"})
    await SwarmBlueprint().execute(ctx)
    session.expire_all()
    done = session.get(Run, "r1")
    assert done.stage == "completed"
    assert "two legs" in done.auto_summary
    # Lead decompose + Lead synthesize + 2 explorers = 4 threads
    assert session.query(Thread).filter_by(run_id="r1").count() == 4


# --------------------------------------------------------------- await polling
async def test_await_thread_polls_until_settled(session, make_user):
    """H-51: the fake spawn always returned a terminal `idle` status, so
    _await_thread's polling loop (running -> completed) was never exercised
    — a regression that made it return on the first poll would still pass.
    Drive the loop for real: start the thread `running`, flip it to
    `completed` from a side task, and assert _await_thread actually waited."""
    import asyncio
    run = _seed(session, make_user)
    session.add(Thread(id="e0", run_id=run.id, persona="explorer", status="running"))
    session.commit()

    async def settle():
        await asyncio.sleep(0.05)
        s = get_session()
        try:
            t = s.get(Thread, "e0")
            t.status = "completed"
            s.commit()
        finally:
            s.close()

    asyncio.create_task(settle())
    # poll_seconds=0.01 so the loop spins several times before the flip lands.
    await _await_thread("e0", poll_seconds=0.01)
    session.expire_all()
    assert session.get(Thread, "e0").status == "completed"


async def test_await_thread_missing_thread_is_failed(session, make_user):
    """H-51: a missing thread must read as `failed` (swarm.py:259), not hang
    the poll loop forever waiting for a row that will never exist."""
    run = _seed(session, make_user)
    # No Thread row for "ghost" — _await_thread must treat it as failed/terminal.
    await _await_thread("ghost", poll_seconds=0.01)
