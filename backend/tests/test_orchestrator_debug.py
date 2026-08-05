import json

import pytest
from zagent_contracts import RunStage

from app.db.models.event import Event
from app.db.models.thread import Thread
from app.db.models.mode import Mode
from app.db.models.repo import Repo
from app.db.models.run import Plan, PlanStep, Run
from app.orchestrator.blueprints.base import BlueprintContext
from app.orchestrator.blueprints.debug import DebugBlueprint


class _FakeLaneManager:
    # H-49: accept either a single thread (legacy single-phase tests) or a
    # {persona: thread} map. The full execute test spawns TWO threads
    # (debugger + fixer); a shared thread conflated the diagnose and propose
    # event streams — _last_message_text read the proposal as the diagnosis.
    def __init__(self, thread_or_map):
        if isinstance(thread_or_map, dict):
            self._by_persona = thread_or_map
            self._thread = None
        else:
            self._by_persona = None
            self._thread = thread_or_map
        self.spawned = []

    async def spawn(self, run, persona, prompt, persona_prompt, writable_repo, context_repos,
                    resume_session=False, resume_from_thread_id=None):
        self.spawned.append({"persona": persona, "prompt": prompt, "writable": writable_repo})
        if self._by_persona is not None:
            return self._by_persona[persona]
        return self._thread


def _ctx(run, services=None, artifacts=None):
    return BlueprintContext(run=run, services=services or {}, artifacts=artifacts or {})


async def _async(value):
    return value


PROPOSAL = {
    "schema_version": 1, "title": "Fix dedupe", "summary": "trim trailing ws",
    "steps": [{"index": 0, "title": "Fix normalize", "description": "src/x.ts:5",
               "repo": "ServerApp", "files": ["src/x.ts"],
               "success_criterion": "tests pass", "status": "pending"}],
    "blast_radius": [], "risks": [], "evidence_contract": ["tests_pass"],
}


def _seed(session, make_user, repo_name="ServerApp"):
    u = make_user("alice", role="member", status="active")
    run = Run(id="r1", created_by=u.id, mode="debug", stage="queued", repo=repo_name,
              title="Bug: dedupe drift on normalize")
    repo = Repo(name=repo_name, integration_branch="main")
    mode = Mode(name="debug", persona_prompt="You debug.", permission_mode="default",
                topology="debug", permissions={"writable": False, "repos": []})
    session.add_all([run, repo, mode]); session.commit()
    return run, repo, u


# --------------------------------------------------------------- nodes order
def test_nodes_in_order():
    bp = DebugBlueprint()
    nodes = bp.nodes()
    assert [n.name for n in nodes] == ["hydrate", "reproduce", "diagnose", "propose", "present"]
    assert [n.deterministic for n in nodes] == [True, True, False, False, True]
    assert nodes[-1].stage is None


# --------------------------------------------------------------- hydrate
async def test_hydrate_resolves_repo_and_repro(session, make_user):
    run, repo, u = _seed(session, make_user)
    bp = DebugBlueprint()
    ctx = _ctx(run)
    await bp._hydrate(ctx)
    assert ctx.artifacts["repo_row"].name == "ServerApp"
    assert "dedupe" in ctx.artifacts["repro_signal"]


async def test_hydrate_missing_repo_raises(session, make_user):
    u = make_user("alice")
    run = Run(id="r1", created_by=u.id, mode="debug", stage="queued", repo="Ghost", title="t")
    session.add(run); session.commit()
    bp = DebugBlueprint()
    with pytest.raises(RuntimeError, match="repo 'Ghost' not registered"):
        await bp._hydrate(_ctx(run))


# --------------------------------------------------------------- reproduce
async def test_reproduce_confirms_failure(session, make_user, monkeypatch):
    run, repo, u = _seed(session, make_user)
    from app.services import evidence as evidence_mod
    monkeypatch.setattr(evidence_mod, "run_test_commands",
                        lambda ws, repo, commands=None: _async({"passed": False, "returncode": 1,
                                                                 "stdout": "FAIL", "stderr": "err"}))
    bp = DebugBlueprint()
    ctx = _ctx(run, artifacts={"repo_row": repo})
    await bp._reproduce(ctx)
    assert ctx.artifacts["failure_confirmed"] is True
    assert ctx.artifacts["repro_result"]["returncode"] == 1


async def test_reproduce_no_failure_when_tests_pass(session, make_user, monkeypatch):
    run, repo, u = _seed(session, make_user)
    from app.services import evidence as evidence_mod
    monkeypatch.setattr(evidence_mod, "run_test_commands",
                        lambda ws, repo, commands=None: _async({"passed": True, "returncode": 0,
                                                                 "stdout": "", "stderr": ""}))
    bp = DebugBlueprint()
    ctx = _ctx(run, artifacts={"repo_row": repo})
    await bp._reproduce(ctx)
    assert ctx.artifacts["failure_confirmed"] is False


async def test_reproduce_runs_against_golden_repo_not_empty_cwd(session, make_user, monkeypatch, tmp_path):
    """Regression (A2): debug stamps no writable clone — without a workspace
    artifact the repro must run against the golden repo mount, never cwd=""."""
    run, repo, u = _seed(session, make_user)
    captured = {}

    async def fake_run(ws, repo_name, commands=None):
        captured["workspace"] = ws
        return {"passed": False, "returncode": 1, "stdout": "", "stderr": ""}
    from app.services import evidence as evidence_mod
    monkeypatch.setattr(evidence_mod, "run_test_commands", fake_run)
    from app.orchestrator.blueprints import debug as debug_mod
    monkeypatch.setattr(debug_mod, "_golden_root", lambda: tmp_path)
    bp = DebugBlueprint()
    ctx = _ctx(run, artifacts={"repo_row": repo})
    await bp._reproduce(ctx)
    assert captured["workspace"] == str(tmp_path / "ServerApp")


async def test_reproduce_persists_test_run_event(session, make_user, monkeypatch):
    """C5: the repro signal lands in the evidence trail, not just in artifacts."""
    run, repo, u = _seed(session, make_user)
    from app.services import evidence as evidence_mod
    monkeypatch.setattr(evidence_mod, "run_test_commands",
                        lambda ws, repo, commands=None: _async({"passed": False, "returncode": 1,
                                                                 "stdout": "FAIL", "stderr": ""}))
    bp = DebugBlueprint()
    ctx = _ctx(run, artifacts={"repo_row": repo})
    await bp._reproduce(ctx)
    session.expire_all()
    ev = session.query(Event).filter_by(run_id="r1", type="test_run").one()
    assert ev.thread_id == "control-plane"
    assert ev.payload["passed"] is False


# --------------------------------------------------------------- diagnose
async def test_diagnose_spawns_readonly_thread(session, make_user):
    run, repo, u = _seed(session, make_user)
    thread = Thread(id="l1", run_id="r1", persona="debugger", status="completed")
    session.add(thread); session.commit()
    session.add(Event(run_id="r1", thread_id="l1", seq=0, type="message", title="d",
                      payload={"text": "root cause: normalize.ts:5"}))
    session.commit()
    lm = _FakeLaneManager(thread)
    bp = DebugBlueprint()
    ctx = _ctx(run, services={"thread_manager": lm}, artifacts={"repo_row": repo, "repro_signal": "bug"})
    await bp._diagnose(ctx)
    assert ctx.artifacts["diagnose_thread_id"] == "l1"
    assert "root cause" in ctx.artifacts["diagnosis"]
    assert lm.spawned[0]["persona"] == "debugger"
    assert lm.spawned[0]["writable"] is None
    # C5: the diagnosis's file:line citation was collected and linted (the golden
    # repo isn't on disk in tests, so the report is the "skipped" shape).
    assert ctx.artifacts["diagnosis_lint"]["total"] == 1


# --------------------------------------------------------------- propose
async def test_propose_collects_proposal_text(session, make_user):
    run, repo, u = _seed(session, make_user)
    thread = Thread(id="l2", run_id="r1", persona="fixer", status="completed")
    session.add(thread); session.commit()
    session.add(Event(run_id="r1", thread_id="l2", seq=0, type="message", title="p",
                      payload={"text": "```json\n" + json.dumps(PROPOSAL) + "\n```"}))
    session.commit()
    lm = _FakeLaneManager(thread)
    bp = DebugBlueprint()
    ctx = _ctx(run, services={"thread_manager": lm}, artifacts={
        "repo_row": repo, "diagnosis": "root cause found"})
    await bp._propose(ctx)
    assert ctx.artifacts["propose_thread_id"] == "l2"
    assert "Fix dedupe" in ctx.artifacts["proposal_text"]
    assert lm.spawned[0]["persona"] == "fixer"


# --------------------------------------------------------------- present
async def test_present_persists_draft_plan_and_stages_awaiting(session, make_user):
    run, repo, u = _seed(session, make_user)
    bp = DebugBlueprint()
    ctx = _ctx(run, artifacts={
        "repo_row": repo, "proposal_text": json.dumps(PROPOSAL),
        "diagnosis": "rc", "failure_confirmed": True,
    })
    await bp._present(ctx)
    session.expire_all()
    plan = session.query(Plan).filter_by(run_id="r1").one()
    assert plan.status == "draft"
    assert plan.structured["failure_confirmed"] is True
    assert len(plan.steps) == 1
    assert session.get(Run, "r1").stage == RunStage.AWAITING_USER.value
    assert session.get(Run, "r1").available_actions == ["review_plan", "start_plan"]
    # C5: the handoff summary lands on the inbox card.
    assert session.get(Run, "r1").auto_summary.startswith("Failure reproduced.")


async def test_present_raises_when_no_proposal(session, make_user):
    run, repo, u = _seed(session, make_user)
    bp = DebugBlueprint()
    ctx = _ctx(run, artifacts={"repo_row": repo, "proposal_text": "not json"})
    with pytest.raises(RuntimeError, match="parseable Plan JSON"):
        await bp._present(ctx)


# --------------------------------------------------------------- full execute
async def test_execute_full_blueprint(session, make_user, monkeypatch):
    run, repo, u = _seed(session, make_user)
    # H-49: diagnose and propose run on SEPARATE threads so each phase reads
    # its own event stream. The old test used one shared fake thread, so
    # _last_message_text("l1") returned the PROPOSAL as the diagnosis.
    diag_thread = Thread(id="diag", run_id="r1", persona="debugger", status="completed", next_seq=0)
    fix_thread = Thread(id="fix", run_id="r1", persona="fixer", status="completed", next_seq=0)
    session.add_all([diag_thread, fix_thread]); session.commit()
    session.add(Event(run_id="r1", thread_id="diag", seq=0, type="message", title="d",
                      payload={"text": "rc"}))
    session.add(Event(run_id="r1", thread_id="fix", seq=0, type="message", title="p",
                      payload={"text": json.dumps(PROPOSAL)}))
    session.commit()
    from app.services import evidence as evidence_mod
    monkeypatch.setattr(evidence_mod, "run_test_commands",
                        lambda ws, repo, commands=None: _async({"passed": False, "returncode": 1,
                                                                 "stdout": "", "stderr": ""}))
    lm = _FakeLaneManager({"debugger": diag_thread, "fixer": fix_thread})
    bp = DebugBlueprint()
    ctx = _ctx(run, services={"thread_manager": lm})
    await bp.execute(ctx)
    session.expire_all()
    assert session.get(Run, "r1").stage == RunStage.AWAITING_USER.value
    assert session.get(Run, "r1").available_actions == ["review_plan", "start_plan"]
    assert session.query(Plan).filter_by(run_id="r1").one().status == "draft"
    # C5: repro event + handoff summary persisted through the full chain.
    assert session.query(Event).filter_by(run_id="r1", type="test_run").count() == 1
    assert session.get(Run, "r1").auto_summary
    # H-49: the two phases used distinct threads and read their OWN streams —
    # the diagnosis is "rc" (not the proposal) and the threads differ.
    assert ctx.artifacts["diagnose_thread_id"] == "diag"
    assert ctx.artifacts["propose_thread_id"] == "fix"
    assert ctx.artifacts["diagnosis"] == "rc"
    assert ctx.artifacts["proposal_text"] == json.dumps(PROPOSAL)
