import json

import pytest
from collegium_contracts import RunStage

from app.db.models.event import Event
from app.db.models.mode import Mode
from app.db.models.repo import Repo
from app.db.models.run import Plan, PlanStep, Run
from app.db.models.thread import Thread
from app.orchestrator.blueprints.base import BlueprintContext
from app.orchestrator.blueprints.development import DevelopmentBlueprint


class _FakeLaneManager:
    def __init__(self, thread, evaluator_thread=None):
        self._thread = thread
        self._evaluator_thread = evaluator_thread or thread
        self.spawned = []

    async def spawn(self, run, persona, prompt, persona_prompt, writable_repo, context_repos,
                    resume_session=False, resume_from_thread_id=None):
        self.spawned.append({
            "persona": persona, "prompt": prompt, "persona_prompt": persona_prompt,
            "writable": writable_repo.name if writable_repo else None,
        })
        return self._evaluator_thread if persona == "evaluator" else self._thread


def _ctx(run, services=None, artifacts=None):
    return BlueprintContext(run=run, services=services or {}, artifacts=artifacts or {})


async def _async(value):
    """Helper: wrap a value in a coroutine so lambdas can stand in for async fns."""
    return value


APPROVED_PLAN = {
    "schema_version": 1, "title": "Fix dedupe", "summary": "normalize ws",
    "steps": [{"index": 0, "title": "Fix normalize", "description": "src/x.ts:5",
               "repo": "ServerApp", "files": ["src/services/scribe/normalize.ts"],
               "success_criterion": "tests pass", "status": "pending"}],
    "blast_radius": [], "risks": [], "evidence_contract": ["tests_pass"],
}


def _seed_approved(session, make_user, repo_name="ServerApp", perms=None,
                    plan_steps=None, plan_status="approved"):
    u = make_user("alice", role="member", status="active")
    run = Run(id="r1", created_by=u.id, mode="development", stage="queued",
              repo=repo_name, title="Fix dedupe")
    repo = Repo(name=repo_name, integration_branch="main")
    mode = Mode(name="development", persona_prompt="You ship.", permission_mode="acceptEdits",
                topology="development", permissions=perms or {"writable": True, "repos": [repo_name]})
    session.add_all([run, repo, mode]); session.commit()
    plan = Plan(run_id="r1", structured=APPROVED_PLAN, status=plan_status)
    session.add(plan); session.commit()
    for s in (plan_steps or APPROVED_PLAN["steps"]):
        session.add(PlanStep(plan_id=plan.id, index=s["index"], title=s["title"],
                             description=s.get("description", ""), repo=s.get("repo"),
                             files=s.get("files", []), success_criterion=s.get("success_criterion", ""),
                             status=s.get("status", "pending")))
    session.commit()
    return run, repo, plan, u


# --------------------------------------------------------------- nodes order
def test_nodes_in_order():
    bp = DevelopmentBlueprint()
    nodes = bp.nodes()
    # stamp (evidence) must precede evaluate so the evaluator judges the stored
    # test signal; evaluate is LAST so no node can roll a failed step back.
    assert [n.name for n in nodes] == ["hydrate", "develop", "stamp", "evaluate"]
    assert [n.deterministic for n in nodes] == [True, False, True, False]
    assert nodes[2].stage == RunStage.VERIFYING
    assert nodes[-1].stage is None


# --------------------------------------------------------------- hydrate
async def test_hydrate_loads_approved_plan_and_stamps_workspace(session, make_user):
    run, repo, plan, u = _seed_approved(session, make_user)
    bp = DevelopmentBlueprint()
    ctx = _ctx(run)
    await bp._hydrate(ctx)
    assert ctx.artifacts["repo_row"].name == "ServerApp"
    assert ctx.artifacts["plan_row_id"] == plan.id
    assert len(ctx.artifacts["plan_steps"]) == 1
    assert ctx.artifacts["branch"].startswith("agent/")
    assert "r1" in ctx.artifacts["workspace"]
    session.expire_all()
    assert session.get(Run, "r1").session_volume_path is not None


async def test_hydrate_missing_repo_raises(session, make_user):
    u = make_user("alice")
    run = Run(id="r1", created_by=u.id, mode="development", stage="queued", repo="Ghost", title="t")
    session.add(run); session.commit()
    bp = DevelopmentBlueprint()
    with pytest.raises(RuntimeError, match="repo 'Ghost' not registered"):
        await bp._hydrate(_ctx(run))


async def test_hydrate_no_repo_no_mention_raises(session, make_user):
    """No default repo: a scoped-mode development run with no explicit repo
    and no @mention fails clearly instead of silently scoping to ServerApp."""
    u = make_user("alice")
    run = Run(id="r1", created_by=u.id, mode="development", stage="queued", title="t")
    session.add(Repo(name="ServerApp", integration_branch="main")); session.commit()
    bp = DevelopmentBlueprint()
    with pytest.raises(RuntimeError, match="no repo targeted"):
        await bp._hydrate(_ctx(run))


async def test_hydrate_no_plan_raises(session, make_user):
    u = make_user("alice")
    run = Run(id="r1", created_by=u.id, mode="development", stage="queued", repo="ServerApp", title="t")
    repo = Repo(name="ServerApp", integration_branch="main")
    session.add_all([run, repo]); session.commit()
    bp = DevelopmentBlueprint()
    with pytest.raises(RuntimeError, match="no approved plan to develop"):
        await bp._hydrate(_ctx(run))


async def test_hydrate_raises_when_plan_not_approved(session, make_user):
    """Strict HITL gate: a draft (unapproved) plan must NOT silently develop."""
    u = make_user("alice")
    run = Run(id="r1", created_by=u.id, mode="development", stage="queued", repo="ServerApp", title="t")
    repo = Repo(name="ServerApp", integration_branch="main")
    session.add_all([run, repo]); session.commit()
    plan = Plan(run_id="r1", structured=APPROVED_PLAN, status="draft")
    session.add(plan); session.commit()
    bp = DevelopmentBlueprint()
    with pytest.raises(RuntimeError, match="no approved plan to develop"):
        await bp._hydrate(_ctx(run))


# --------------------------------------------------------------- develop
async def test_develop_spawns_writable_thread_and_marks_steps_done(session, make_user):
    run, repo, plan, u = _seed_approved(session, make_user)
    thread = Thread(id="l1", run_id="r1", persona="developer", status="completed")
    session.add(thread); session.commit()
    lm = _FakeLaneManager(thread)
    bp = DevelopmentBlueprint()
    # M-61: verify _develop actually awaits the spawned thread. The old test
    # never asserted _await_thread was called, so a regression that skipped
    # the await (returning before the thread finished) would pass silently.
    awaited: list[str] = []

    async def _spy_await(thread_id, poll_seconds=2.0):
        awaited.append(thread_id)
    bp._await_thread = _spy_await
    ctx = _ctx(run, services={"thread_manager": lm}, artifacts={
        "repo_row": repo, "plan_row_id": plan.id, "plan_steps": plan.steps,
        "permissions": {"writable": True, "repos": ["ServerApp"]},
        "workspace": "/ws/r1/ServerApp", "branch": "agent/x",
    })
    await bp._develop(ctx)
    assert ctx.artifacts["develop_thread_id"] == "l1"
    assert lm.spawned[0]["persona"] == "developer"
    assert lm.spawned[0]["writable"] == "ServerApp"
    assert awaited == ["l1"]  # M-61: _await_thread was invoked with the thread id
    session.expire_all()
    assert session.query(PlanStep).filter_by(plan_id=plan.id).one().status == "done"


async def test_develop_read_only_when_mode_denies_writable(session, make_user):
    run, repo, plan, u = _seed_approved(session, make_user,
                                         perms={"writable": False, "repos": []})
    thread = Thread(id="l1", run_id="r1", persona="developer", status="completed")
    session.add(thread); session.commit()
    lm = _FakeLaneManager(thread)
    bp = DevelopmentBlueprint()
    ctx = _ctx(run, services={"thread_manager": lm}, artifacts={
        "repo_row": repo, "plan_row_id": plan.id, "plan_steps": plan.steps,
        "permissions": {"writable": False, "repos": []},
        "workspace": "/ws/r1/ServerApp", "branch": "agent/x",
    })
    await bp._develop(ctx)
    assert lm.spawned[0]["writable"] is None


async def test_develop_read_only_when_repo_not_in_allowed_list(session, make_user):
    run, repo, plan, u = _seed_approved(session, make_user, repo_name="ClientApp",
                                         perms={"writable": True, "repos": ["ServerApp"]})
    thread = Thread(id="l1", run_id="r1", persona="developer", status="completed")
    session.add(thread); session.commit()
    lm = _FakeLaneManager(thread)
    bp = DevelopmentBlueprint()
    ctx = _ctx(run, services={"thread_manager": lm}, artifacts={
        "repo_row": repo, "plan_row_id": plan.id, "plan_steps": plan.steps,
        "permissions": {"writable": True, "repos": ["ServerApp"]},
        "workspace": "/ws", "branch": "agent/x",
    })
    await bp._develop(ctx)
    assert lm.spawned[0]["writable"] is None


# --------------------------------------------------------------- evaluate
async def test_evaluate_spawns_fresh_readonly_thread(session, make_user):
    run, repo, plan, u = _seed_approved(session, make_user)
    dev_thread = Thread(id="l1", run_id="r1", persona="developer", status="completed")
    eval_thread = Thread(id="l2", run_id="r1", persona="evaluator", status="completed")
    session.add_all([dev_thread, eval_thread]); session.commit()
    session.add(Event(run_id="r1", thread_id="l2", seq=0, type="message", title="v",
                      payload={"text": '{"verdict":"pass","steps":[]}'}))
    session.commit()
    lm = _FakeLaneManager(dev_thread, evaluator_thread=eval_thread)
    bp = DevelopmentBlueprint()
    ctx = _ctx(run, services={"thread_manager": lm}, artifacts={
        "repo_row": repo, "plan_row_id": plan.id, "plan_steps": plan.steps,
        "workspace": "/ws", "branch": "agent/x",
    })
    await bp._evaluate(ctx)
    assert ctx.artifacts["evaluator_thread_id"] == "l2"
    assert ctx.artifacts["evaluator_notes"].startswith("{")
    assert lm.spawned[0]["persona"] == "evaluator"
    assert lm.spawned[0]["writable"] is None  # fresh + read-only


async def test_evaluate_failure_rolls_back_failed_steps(session, make_user):
    run, repo, plan, u = _seed_approved(session, make_user)
    dev_thread = Thread(id="l1", run_id="r1", persona="developer", status="completed")
    eval_thread = Thread(id="l2", run_id="r1", persona="evaluator", status="completed")
    session.add_all([dev_thread, eval_thread]); session.commit()
    session.add(Event(run_id="r1", thread_id="l2", seq=0, type="message", title="v",
                      payload={"text": json.dumps({
                          "verdict": "fail",
                          "steps": [{"index": 0, "status": "fail", "note": "no"}],
                      })}))
    session.commit()
    lm = _FakeLaneManager(dev_thread, evaluator_thread=eval_thread)
    bp = DevelopmentBlueprint()
    ctx = _ctx(run, services={"thread_manager": lm}, artifacts={
        "repo_row": repo, "plan_row_id": plan.id, "plan_steps": plan.steps,
        "workspace": "/ws", "branch": "agent/x",
    })
    await bp._evaluate(ctx)
    session.expire_all()
    assert session.query(PlanStep).filter_by(plan_id=plan.id).one().status == "failed"


async def test_evaluate_no_message_records_blank_notes(session, make_user):
    run, repo, plan, u = _seed_approved(session, make_user)
    dev_thread = Thread(id="l1", run_id="r1", persona="developer", status="completed")
    eval_thread = Thread(id="l2", run_id="r1", persona="evaluator", status="completed")
    session.add_all([dev_thread, eval_thread]); session.commit()
    lm = _FakeLaneManager(dev_thread, evaluator_thread=eval_thread)
    bp = DevelopmentBlueprint()
    ctx = _ctx(run, services={"thread_manager": lm}, artifacts={
        "repo_row": repo, "plan_row_id": plan.id, "plan_steps": plan.steps,
        "workspace": "/ws", "branch": "agent/x",
    })
    await bp._evaluate(ctx)
    assert ctx.artifacts["evaluator_notes"] == ""


# --------------------------------------------------------------- stamp
async def test_stamp_runs_tests_persists_event_without_touching_steps(session, make_user, monkeypatch):
    """Stamp persists the tamper-proof signal but must NOT roll step statuses —
    the evaluator (which runs after stamp) is the authority on step failure."""
    run, repo, plan, u = _seed_approved(session, make_user)
    thread = Thread(id="l1", run_id="r1", persona="developer", status="completed", next_seq=0)
    session.add(thread); session.commit()
    from app.services import evidence as evidence_mod
    monkeypatch.setattr(evidence_mod, "run_test_commands",
                        lambda ws, repo, commands=None: _async({"passed": True, "returncode": 0,
                                                                 "stdout": "ok", "stderr": ""}))
    monkeypatch.setattr(evidence_mod, "stamp_screenshots",
                        lambda run_id, ws, routes: _async([]))
    bp = DevelopmentBlueprint()
    ctx = _ctx(run, artifacts={
        "repo_row": repo, "plan_row_id": plan.id, "plan_steps": plan.steps,
        "workspace": "/ws", "develop_thread_id": "l1",
    })
    await bp._stamp(ctx)
    session.expire_all()
    ev = session.query(Event).filter_by(run_id="r1", type="test_run").one()
    assert ev.payload["passed"] is True
    assert session.get(Thread, "l1").next_seq == 1
    assert ctx.artifacts["test_signal"]["passed"] is True
    # Step stays pending — stamp never flips statuses.
    assert session.query(PlanStep).filter_by(plan_id=plan.id).one().status == "pending"


async def test_stamp_preserves_evaluator_failed_steps(session, make_user, monkeypatch):
    """Regression (A1): a step the evaluator marked failed must survive any
    later deterministic pass — nothing may roll it back to done."""
    run, repo, plan, u = _seed_approved(
        session, make_user,
        plan_steps=[{"index": 0, "title": "s0", "files": [], "success_criterion": "x",
                     "status": "failed"}])
    thread = Thread(id="l1", run_id="r1", persona="developer", status="completed", next_seq=0)
    session.add(thread); session.commit()
    from app.services import evidence as evidence_mod
    monkeypatch.setattr(evidence_mod, "run_test_commands",
                        lambda ws, repo, commands=None: _async({"passed": True, "returncode": 0,
                                                                 "stdout": "", "stderr": ""}))
    monkeypatch.setattr(evidence_mod, "stamp_screenshots",
                        lambda run_id, ws, routes: _async([]))
    bp = DevelopmentBlueprint()
    ctx = _ctx(run, artifacts={
        "repo_row": repo, "plan_row_id": plan.id, "plan_steps": plan.steps,
        "workspace": "/ws", "develop_thread_id": "l1",
    })
    await bp._stamp(ctx)
    session.expire_all()
    assert session.query(PlanStep).filter_by(plan_id=plan.id).one().status == "failed"


async def test_stamp_persists_screenshot_event_for_ui_files(session, make_user, monkeypatch):
    run, repo, plan, u = _seed_approved(session, make_user, repo_name="ClientApp",
                                         plan_steps=[{"index": 0, "title": "ui",
                                                       "files": ["ClientApp/src/page.tsx"],
                                                       "success_criterion": "renders", "status": "pending"}],
                                         perms={"writable": True, "repos": ["ClientApp"]})
    thread = Thread(id="l1", run_id="r1", persona="developer", status="completed", next_seq=0)
    session.add(thread); session.commit()
    from app.services import evidence as evidence_mod
    monkeypatch.setattr(evidence_mod, "run_test_commands",
                        lambda ws, repo, commands=None: _async({"passed": True, "returncode": 0,
                                                                 "stdout": "", "stderr": ""}))
    monkeypatch.setattr(evidence_mod, "stamp_screenshots",
                        lambda run_id, ws, routes: _async([{"route": "/", "path": "x.png", "captured": False}]))
    bp = DevelopmentBlueprint()
    ctx = _ctx(run, artifacts={
        "repo_row": repo, "plan_row_id": plan.id, "plan_steps": plan.steps,
        "workspace": "/ws", "develop_thread_id": "l1",
    })
    await bp._stamp(ctx)
    session.expire_all()
    assert session.query(Event).filter_by(run_id="r1", type="screenshot").one().payload["routes"][0]["route"] == "/"
    assert session.get(Thread, "l1").next_seq == 2


# --------------------------------------------------------------- helpers
def test_persona_injects_mode_playbooks(session, make_user):
    """WU6: developer/evaluator persona prompts carry the mode's playbooks."""
    from app.db.models.knowledge import Playbook
    run, repo, plan, u = _seed_approved(session, make_user)
    mode = session.query(Mode).filter_by(name="development").one()
    mode.playbook_ids = ["development/serverapp-areas"]
    session.add(Playbook(name="development/serverapp-areas", version=1, skill_md=(
        "---\nname: development/serverapp-areas\nmode: development\n---\n\n"
        "# ServerApp Areas\nOne area owns a domain surface.\n")))
    session.commit()
    bp = DevelopmentBlueprint()
    prompt = bp._persona(_ctx(run), "developer", "You are the DEVELOPER.")
    assert "One area owns a domain surface." in prompt
    assert prompt.endswith("You are the DEVELOPER.")


def test_writable_repo_allows_when_in_list():
    bp = DevelopmentBlueprint()
    repo = Repo(name="ServerApp", integration_branch="main")
    assert bp._writable_repo(repo, {"writable": True, "repos": ["ServerApp"]}) is repo


def test_writable_repo_denies_when_not_writable():
    bp = DevelopmentBlueprint()
    repo = Repo(name="ServerApp", integration_branch="main")
    assert bp._writable_repo(repo, {"writable": False, "repos": []}) is None


def test_writable_repo_denies_when_repo_not_allowed():
    bp = DevelopmentBlueprint()
    repo = Repo(name="ClientApp", integration_branch="main")
    assert bp._writable_repo(repo, {"writable": True, "repos": ["ServerApp"]}) is None


def test_writable_repo_allows_empty_repos_means_any():
    bp = DevelopmentBlueprint()
    repo = Repo(name="Anything", integration_branch="main")
    assert bp._writable_repo(repo, {"writable": True, "repos": []}) is repo


def test_parse_json_fenced_and_raw():
    text = 'pre {"verdict":"pass","steps":[]} post'
    assert DevelopmentBlueprint._parse_json(text)["verdict"] == "pass"
    assert DevelopmentBlueprint._parse_json("") is None
    assert DevelopmentBlueprint._parse_json("no braces") is None


def test_screenshot_routes_detects_ui_files(session, make_user):
    run, repo, plan, u = _seed_approved(session, make_user, repo_name="ClientApp",
                                         plan_steps=[{"index": 0, "title": "ui",
                                                       "files": ["ClientApp/src/page.tsx"],
                                                       "success_criterion": "renders", "status": "pending"}],
                                         perms={"writable": True, "repos": ["ClientApp"]})
    bp = DevelopmentBlueprint()
    ctx = _ctx(run, artifacts={"plan_steps": plan.steps})
    assert bp._screenshot_routes(ctx) == ["/"]


def test_screenshot_routes_empty_for_backend_only(session, make_user):
    run, repo, plan, u = _seed_approved(session, make_user)
    bp = DevelopmentBlueprint()
    ctx = _ctx(run, artifacts={"plan_steps": plan.steps})
    assert bp._screenshot_routes(ctx) == []


def test_evaluator_prompt_includes_stored_test_signal(session, make_user):
    """A1: the evaluator judges the plan against the control-plane evidence,
    not against the developer's self-report — the signal must reach its prompt."""
    run, repo, plan, u = _seed_approved(session, make_user)
    bp = DevelopmentBlueprint()
    ctx = _ctx(run, artifacts={
        "plan_steps": plan.steps, "workspace": "/ws",
        "test_signal": {"passed": False, "returncode": 1, "stdout": "1 failed, 2 passed"},
    })
    prompt = bp._compose_evaluator_prompt(ctx)
    assert "passed=False" in prompt
    assert "1 failed, 2 passed" in prompt


# --------------------------------------------------------------- full execute
async def test_execute_full_blueprint_chains_nodes(session, make_user, monkeypatch):
    run, repo, plan, u = _seed_approved(session, make_user)
    dev_thread = Thread(id="l1", run_id="r1", persona="developer", status="completed", next_seq=0)
    eval_thread = Thread(id="l2", run_id="r1", persona="evaluator", status="completed", next_seq=0)
    session.add_all([dev_thread, eval_thread]); session.commit()
    session.add(Event(run_id="r1", thread_id="l2", seq=0, type="message", title="v",
                      payload={"text": '{"verdict":"pass","steps":[]}'}))
    session.commit()
    from app.services import evidence as evidence_mod
    monkeypatch.setattr(evidence_mod, "run_test_commands",
                        lambda ws, repo, commands=None: _async({"passed": True, "returncode": 0,
                                                                 "stdout": "", "stderr": ""}))
    monkeypatch.setattr(evidence_mod, "stamp_screenshots",
                        lambda run_id, ws, routes: _async([]))
    lm = _FakeLaneManager(dev_thread, evaluator_thread=eval_thread)
    bp = DevelopmentBlueprint()
    ctx = _ctx(run, services={"thread_manager": lm})
    await bp.execute(ctx)
    session.expire_all()
    assert session.get(Run, "r1").stage == RunStage.VERIFYING.value
    assert session.get(Run, "r1").available_actions == ["review_evidence", "create_pr"]
    assert session.query(Event).filter_by(run_id="r1", type="test_run").count() == 1
    # Trajectory summary now rides the evaluator node (it runs last).
    from app.db.models.trajectory import TrajectorySummary
    assert session.query(TrajectorySummary).filter_by(run_id="r1").count() == 1


async def test_execute_evaluator_fail_verdict_survives_to_the_end(session, make_user, monkeypatch):
    """Regression (A1): full chain with the evaluator failing step 0 — the final
    persisted step status must be "failed" (previously stamp erased it)."""
    run, repo, plan, u = _seed_approved(session, make_user)
    dev_thread = Thread(id="l1", run_id="r1", persona="developer", status="completed", next_seq=0)
    eval_thread = Thread(id="l2", run_id="r1", persona="evaluator", status="completed", next_seq=0)
    session.add_all([dev_thread, eval_thread]); session.commit()
    session.add(Event(run_id="r1", thread_id="l2", seq=0, type="message", title="v",
                      payload={"text": json.dumps({
                          "verdict": "fail",
                          "steps": [{"index": 0, "status": "fail", "note": "criterion unmet"}],
                      })}))
    session.commit()
    from app.services import evidence as evidence_mod
    monkeypatch.setattr(evidence_mod, "run_test_commands",
                        lambda ws, repo, commands=None: _async({"passed": True, "returncode": 0,
                                                                 "stdout": "", "stderr": ""}))
    monkeypatch.setattr(evidence_mod, "stamp_screenshots",
                        lambda run_id, ws, routes: _async([]))
    lm = _FakeLaneManager(dev_thread, evaluator_thread=eval_thread)
    bp = DevelopmentBlueprint()
    ctx = _ctx(run, services={"thread_manager": lm})
    await bp.execute(ctx)
    session.expire_all()
    assert session.query(PlanStep).filter_by(plan_id=plan.id).one().status == "failed"
