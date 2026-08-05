import json

import pytest
from zagent_contracts import RunStage

from app.db.models.event import Event
from app.db.models.thread import Thread
from app.db.models.mode import Mode
from app.db.models.repo import Repo
from app.db.models.run import Plan, PlanStep, Run
from app.orchestrator.blueprints.base import BlueprintContext
from app.orchestrator.blueprints.plan import PlanBlueprint


class _FakeLaneManager:
    def __init__(self, thread):
        self._thread = thread
        self.spawned = []

    async def spawn(self, run, persona, prompt, persona_prompt, writable_repo, context_repos,
                    resume_session=False, resume_from_thread_id=None):
        self.spawned.append({"persona": persona, "prompt": prompt, "persona_prompt": persona_prompt})
        return self._thread


class _FakeAdo:
    def __init__(self, payload=None, raise_exc=None):
        self._payload = payload or {"id": 42, "fields": {"System.Title": "Bug: dedupe drift"}}
        self._raise = raise_exc
        self.calls: list[int] = []  # M-63: record calls so the swallow path is observable

    async def get_work_item(self, work_item_id):
        self.calls.append(work_item_id)
        if self._raise:
            raise self._raise
        return self._payload


def _ctx(run, services=None, artifacts=None):
    return BlueprintContext(run=run, services=services or {}, artifacts=artifacts or {})


VALID_PLAN = {
    "schema_version": 1,
    "title": "Fix dedupe drift",
    "summary": "Normalize trailing whitespace in dedupe.service.ts",
    "steps": [{
        "index": 0, "title": "Fix normalize",
        "description": "src/services/scribe/normalize.ts:5 trim trailing ws",
        "repo": "ServerApp", "files": ["src/services/scribe/normalize.ts"],
        "success_criterion": "dedupe tests pass", "status": "pending",
    }],
    "blast_radius": ["ClientApp"], "risks": ["normalize.ts:5 may shift"],
    "evidence_contract": ["tests_pass"],
}


# --------------------------------------------------------------- _hydrate
async def test_hydrate_resolves_repo(session, make_user):
    u = make_user("alice")
    run = Run(id="r1", created_by=u.id, mode="plan", stage="queued", repo="ServerApp", title="t")
    repo = Repo(name="ServerApp", integration_branch="main")
    session.add_all([run, repo]); session.commit()
    bp = PlanBlueprint()
    ctx = _ctx(run)
    await bp._hydrate(ctx)
    assert ctx.artifacts["repo_row"].name == "ServerApp"
    assert isinstance(ctx.artifacts["blast_radius"], list)


async def test_hydrate_missing_repo_raises(session, make_user):
    u = make_user("alice")
    run = Run(id="r1", created_by=u.id, mode="plan", stage="queued", repo="Ghost", title="t")
    session.add(run); session.commit()
    bp = PlanBlueprint()
    with pytest.raises(RuntimeError, match="repo 'Ghost' not registered"):
        await bp._hydrate(_ctx(run))


async def test_hydrate_fetches_work_item(session, make_user):
    u = make_user("alice")
    run = Run(id="r1", created_by=u.id, mode="plan", stage="queued", repo="ServerApp",
              work_item_id=42, title="t")
    repo = Repo(name="ServerApp", integration_branch="main")
    session.add_all([run, repo]); session.commit()
    bp = PlanBlueprint()
    ado = _FakeAdo(payload={"id": 42, "fields": {"System.Title": "Bug X"}})
    ctx = _ctx(run, services={"ado_client": ado})
    await bp._hydrate(ctx)
    assert ctx.artifacts["work_item"]["id"] == 42


async def test_hydrate_work_item_failure_is_swallowed(session, make_user):
    u = make_user("alice")
    run = Run(id="r1", created_by=u.id, mode="plan", stage="queued", repo="ServerApp",
              work_item_id=99, title="t")
    repo = Repo(name="ServerApp", integration_branch="main")
    session.add_all([run, repo]); session.commit()
    bp = PlanBlueprint()
    ado = _FakeAdo(raise_exc=RuntimeError("ado down"))
    ctx = _ctx(run, services={"ado_client": ado})
    await bp._hydrate(ctx)
    assert ctx.artifacts["work_item"] is None
    # M-63: distinguish the swallow path from a no-op None return. The old
    # assertion (`work_item is None`) was indistinguishable from _hydrate
    # never calling the client at all. Assert the client WAS called (and
    # raised) so the swallow path is actually exercised.
    assert ado.calls == [99]


# --------------------------------------------------------------- _draft
async def test_draft_reads_plan_json_from_thread_message(session, make_user):
    u = make_user("alice")
    run = Run(id="r1", created_by=u.id, mode="plan", stage="planning", repo="ServerApp", title="fix dedupe")
    repo = Repo(name="ServerApp", integration_branch="main")
    mode = Mode(name="plan", persona_prompt="You are a planner.", permission_mode="default")
    thread = Thread(id="l1", run_id="r1", persona="planner", status="completed")
    session.add_all([run, repo, mode, thread]); session.commit()
    session.add(Event(run_id="r1", thread_id="l1", seq=0, type="message", title="plan",
                      payload={"text": "```json\n" + json.dumps(VALID_PLAN) + "\n```"}))
    session.commit()
    lm = _FakeLaneManager(thread)
    bp = PlanBlueprint()
    ctx = _ctx(run, services={"thread_manager": lm},
               artifacts={"repo_row": repo, "task": "fix dedupe", "work_item": None, "blast_radius": []})
    await bp._draft(ctx)
    assert ctx.artifacts["draft_thread_id"] == "l1"
    assert "Fix dedupe drift" in ctx.artifacts["draft_text"]
    assert lm.spawned[0]["persona"] == "planner"
    assert "Structured output contract" in lm.spawned[0]["persona_prompt"]


async def test_draft_persona_includes_mode_playbooks(session, make_user):
    """WU6: the planner thread's persona prompt carries the mode's playbooks."""
    from app.db.models.knowledge import Playbook
    u = make_user("alice")
    run = Run(id="r1", created_by=u.id, mode="plan", stage="planning", repo="ServerApp", title="t")
    repo = Repo(name="ServerApp", integration_branch="main")
    mode = Mode(name="plan", persona_prompt="You are a planner.", playbook_ids=["plan/fleet-scoping"])
    pb = Playbook(name="plan/fleet-scoping", version=1, skill_md=(
        "---\nname: plan/fleet-scoping\nmode: plan\ndescription: scope it\n---\n\n"
        "# Fleet-scoping\nAlways scope from the blast radius.\n"))
    thread = Thread(id="l1", run_id="r1", persona="planner", status="completed")
    session.add_all([run, repo, mode, pb, thread]); session.commit()
    session.add(Event(run_id="r1", thread_id="l1", seq=0, type="message", title="plan",
                      payload={"text": json.dumps(VALID_PLAN)}))
    session.commit()
    lm = _FakeLaneManager(thread)
    bp = PlanBlueprint()
    ctx = _ctx(run, services={"thread_manager": lm},
               artifacts={"repo_row": repo, "task": "t", "work_item": None, "blast_radius": []})
    await bp._draft(ctx)
    assert "Always scope from the blast radius." in lm.spawned[0]["persona_prompt"]


async def test_draft_with_no_message_yields_none(session, make_user):
    u = make_user("alice")
    run = Run(id="r1", created_by=u.id, mode="plan", stage="planning", repo="ServerApp", title="t")
    repo = Repo(name="ServerApp", integration_branch="main")
    mode = Mode(name="plan", persona_prompt="p")
    thread = Thread(id="l1", run_id="r1", persona="planner", status="completed")
    session.add_all([run, repo, mode, thread]); session.commit()
    lm = _FakeLaneManager(thread)
    bp = PlanBlueprint()
    ctx = _ctx(run, services={"thread_manager": lm},
               artifacts={"repo_row": repo, "task": "t", "work_item": None, "blast_radius": []})
    await bp._draft(ctx)
    assert ctx.artifacts["draft_text"] is None


# --------------------------------------------------------------- _critique
async def test_critique_spawns_fresh_critic_thread(session, make_user):
    u = make_user("alice")
    run = Run(id="r1", created_by=u.id, mode="plan", stage="planning", repo="ServerApp", title="t")
    repo = Repo(name="ServerApp", integration_branch="main")
    mode = Mode(name="plan", persona_prompt="p")
    thread = Thread(id="l2", run_id="r1", persona="critic", status="completed")
    session.add_all([run, repo, mode, thread]); session.commit()
    session.add(Event(run_id="r1", thread_id="l2", seq=0, type="message", title="critique",
                      payload={"text": "drift on normalize.ts:5"}))
    session.commit()
    lm = _FakeLaneManager(thread)
    bp = PlanBlueprint()
    ctx = _ctx(run, services={"thread_manager": lm},
               artifacts={"repo_row": repo, "draft_plan": VALID_PLAN, "draft_text": json.dumps(VALID_PLAN)})
    await bp._critique(ctx)
    assert ctx.artifacts["critique_thread_id"] == "l2"
    assert "drift" in ctx.artifacts["critique_notes"]
    assert lm.spawned[0]["persona"] == "critic"
    assert "CRITIC" in lm.spawned[0]["persona_prompt"]


async def test_critique_no_parseable_draft_records_notes(session, make_user):
    u = make_user("alice")
    run = Run(id="r1", created_by=u.id, mode="plan", stage="planning", repo="ServerApp", title="t")
    repo = Repo(name="ServerApp", integration_branch="main")
    session.add_all([run, repo]); session.commit()
    lm = _FakeLaneManager(Thread(id="lx", run_id="r1", persona="critic", status="completed"))
    bp = PlanBlueprint()
    ctx = _ctx(run, services={"thread_manager": lm},
               artifacts={"repo_row": repo, "draft_text": "not json"})
    await bp._critique(ctx)
    assert "no parseable" in ctx.artifacts["critique_notes"]


# --------------------------------------------------------------- _present
async def test_present_persists_plan_steps_and_transitions(session, make_user):
    u = make_user("alice")
    run = Run(id="r1", created_by=u.id, mode="plan", stage="planning", repo="ServerApp", title="t")
    repo = Repo(name="ServerApp", integration_branch="main")
    session.add_all([run, repo]); session.commit()
    bp = PlanBlueprint()
    ctx = _ctx(run, artifacts={
        "repo_row": repo, "draft_plan": VALID_PLAN, "blast_radius": ["ClientApp"],
        "critique_notes": "check normalize.ts:5",
    })
    await bp._present(ctx)
    session.expire_all()
    plan = session.query(Plan).filter_by(run_id="r1").one()
    assert plan.status == "draft"
    assert plan.structured["title"] == "Fix dedupe drift"
    assert plan.structured["blast_radius"] == ["ClientApp"]
    assert plan.structured["critic_notes"] == ["check normalize.ts:5"]  # always a list (C1)
    steps = session.query(PlanStep).filter_by(plan_id=plan.id).all()
    assert len(steps) == 1
    assert steps[0].status == "pending"
    assert session.get(Run, "r1").stage == RunStage.AWAITING_USER.value


async def test_present_flags_citation_lint_without_crashing(session, make_user):
    u = make_user("alice")
    run = Run(id="r1", created_by=u.id, mode="plan", stage="planning", repo="ServerApp", title="t")
    repo = Repo(name="ServerApp", integration_branch="main")
    session.add_all([run, repo]); session.commit()
    bp = PlanBlueprint()
    plan_with_cites = dict(VALID_PLAN)
    plan_with_cites["steps"] = [{
        "index": 0, "title": "x", "description": "see src/services/scribe/normalize.ts:5 and ghost.py:99",
        "repo": "ServerApp", "files": [], "success_criterion": "tests pass", "status": "pending",
    }]
    ctx = _ctx(run, artifacts={"repo_row": repo, "draft_plan": plan_with_cites, "blast_radius": []})
    await bp._present(ctx)
    session.expire_all()
    plan = session.query(Plan).filter_by(run_id="r1").one()
    assert "citation_lint" in plan.structured


async def test_present_raises_when_no_parseable_plan(session, make_user):
    u = make_user("alice")
    run = Run(id="r1", created_by=u.id, mode="plan", stage="planning", repo="ServerApp", title="t")
    repo = Repo(name="ServerApp", integration_branch="main")
    session.add_all([run, repo]); session.commit()
    bp = PlanBlueprint()
    ctx = _ctx(run, artifacts={"repo_row": repo, "draft_text": "not json"})
    with pytest.raises(RuntimeError, match="parseable Plan JSON"):
        await bp._present(ctx)


# --------------------------------------------------------------- helpers / nodes
def test_parse_json_fenced():
    text = "here\n```json\n" + json.dumps(VALID_PLAN) + "\n```\ndone"
    assert PlanBlueprint._parse_json(text)["title"] == "Fix dedupe drift"


def test_parse_json_raw():
    text = "prefix " + json.dumps(VALID_PLAN) + " suffix"
    assert PlanBlueprint._parse_json(text)["title"] == "Fix dedupe drift"


def test_parse_json_none():
    assert PlanBlueprint._parse_json("") is None
    assert PlanBlueprint._parse_json("no braces here") is None


def test_collect_citations():
    cits = PlanBlueprint._collect_citations(VALID_PLAN)
    assert "normalize.ts:5" in cits


def test_lint_citations_none_when_empty():
    assert PlanBlueprint._lint_citations("ServerApp", []) is None


def test_lint_citations_skips_when_golden_missing(monkeypatch, tmp_path):
    fake_settings = type("S", (), {"golden_dir": tmp_path / "nope"})()
    monkeypatch.setattr("app.core.config.get_settings", lambda: fake_settings)
    report = PlanBlueprint._lint_citations("ServerApp", ["normalize.ts:5"])
    assert report is not None
    assert report.get("skipped")


def test_nodes_in_order():
    bp = PlanBlueprint()
    nodes = bp.nodes()
    assert [n.name for n in nodes] == ["hydrate", "draft", "critique", "present"]
    assert [n.deterministic for n in nodes] == [True, False, False, True]
    assert nodes[-1].stage == RunStage.AWAITING_USER

