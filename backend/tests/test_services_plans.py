import pytest
from collegium_contracts import RunStage

from app.db.models.run import Plan, Run
from app.services import plans


def _seed_plan(session, make_user, status="draft", structured=None):
    u = make_user("alice", role="member", status="active")
    run = Run(id="r1", created_by=u.id, mode="plan", stage=RunStage.AWAITING_USER.value, repo="ServerApp", title="t")
    plan = Plan(run_id="r1", structured=structured or {
        "title": "P", "steps": [{"index": 0, "title": "s0", "status": "draft"}],
    }, status=status)
    session.add_all([run, plan])
    session.commit()
    from app.db.models.run import PlanStep
    session.add(PlanStep(plan_id=plan.id, index=0, title="s0", description="d",
                        repo="ServerApp", files=["a.ts"], success_criterion="tests", status="draft"))
    session.commit()
    return run, plan, u


def test_approve_plan_marks_approved_and_steps_pending(session, make_user):
    run, plan, u = _seed_plan(session, make_user)
    result = plans.approve_plan("r1", u.id)
    assert result.status == "approved"
    assert result.decided_by == u.id
    session.expire_all()
    assert run.stage == RunStage.DEVELOPING.value
    assert plan.steps[0].status == "pending"


def test_approve_plan_rejects_non_draft(session, make_user):
    """C2: only a plan awaiting decision may be approved."""
    run, plan, u = _seed_plan(session, make_user, status="approved")
    with pytest.raises(ValueError, match="not awaiting decision"):
        plans.approve_plan("r1", u.id)


def test_reject_plan_rejects_non_draft(session, make_user):
    run, plan, u = _seed_plan(session, make_user, status="approved")
    with pytest.raises(ValueError, match="not awaiting decision"):
        plans.reject_plan("r1", u.id)


def test_approve_plan_no_plan_raises(session, make_user):
    u = make_user("alice")
    run = Run(id="r1", created_by=u.id, mode="plan", stage="awaiting_user", title="t")
    session.add(run); session.commit()
    with pytest.raises(ValueError, match="no plan to approve"):
        plans.approve_plan("r1", u.id)


def test_reject_plan_marks_rejected_and_rolls_to_planning(session, make_user):
    run, plan, u = _seed_plan(session, make_user)
    result = plans.reject_plan("r1", u.id, notes="fix the normalize citation")
    assert result.status == "rejected"
    session.expire_all()
    assert run.stage == RunStage.PLANNING.value
    assert "fix the normalize citation" in plan.structured["critic_notes"]


def test_reject_plan_no_plan_raises(session, make_user):
    u = make_user("alice")
    run = Run(id="r1", created_by=u.id, mode="plan", stage="awaiting_user", title="t")
    session.add(run); session.commit()
    with pytest.raises(ValueError, match="no plan to reject"):
        plans.reject_plan("r1", u.id)


def test_critic_notes_extracts_last(session, make_user):
    run, plan, u = _seed_plan(session, make_user, structured={
        "title": "P", "steps": [], "critic_notes": ["first note", "second note"],
    })
    assert plans.critic_notes(plan) == "second note"


def test_reject_plan_normalizes_legacy_string_notes(session, make_user):
    """C1: a plan whose critic_notes is a legacy bare string gets normalized to
    a list before the new note appends — never str.append AttributeError."""
    run, plan, u = _seed_plan(session, make_user, structured={
        "title": "P", "steps": [], "critic_notes": "legacy note",
    })
    result = plans.reject_plan("r1", u.id, notes="second round")
    assert result.structured["critic_notes"] == ["legacy note", "second round"]


def test_critic_notes_empty_returns_blank(session, make_user):
    run, plan, u = _seed_plan(session, make_user)
    assert plans.critic_notes(plan) == ""


def test_latest_plan_returns_most_recent(session, make_user):
    run, plan, u = _seed_plan(session, make_user)
    plan2 = Plan(run_id="r1", structured={"title": "P2", "steps": []}, status="draft")
    session.add(plan2); session.commit()
    assert plans.latest_plan("r1").id == plan2.id


def test_latest_plan_none_when_no_plan(session, make_user):
    u = make_user("alice")
    run = Run(id="r1", created_by=u.id, mode="plan", stage="queued", title="t")
    session.add(run); session.commit()
    assert plans.latest_plan("r1") is None
