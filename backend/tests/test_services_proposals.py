"""Improvement Inbox tests (plan Phase 4): emit validation (evidence required),
impact×confidence ranking, accept → gated Development run with weekly spend
ceiling, dismiss → user-scoped flywheel preference draft. run_manager is a fake.
"""

import pytest

from app.db.models.knowledge import KnowledgeItem
from app.db.models.proposal import Proposal
from app.db.models.run import Run
from app.services import proposals


class FakeRM:
    def __init__(self):
        self.created = []

    async def create_run(self, source, initiated_by, mode_name, task,
                         repo=None, autonomy=None, **kw):
        import uuid
        run = type("R", (), {"id": str(uuid.uuid4())})()
        self.created.append({"id": run.id, "source": source, "mode": mode_name,
                             "task": task, "repo": repo, "autonomy": autonomy,
                             "by": initiated_by})
        return run


def _emit(source="janitor", impact="high", confidence="medium", **kw):
    return proposals.emit(source=source, title="Dead code in billing module",
                          body="utils.ts re-export chain is unreferenced",
                          evidence=["ClientApp/src/lib/utils.ts:14"],
                          impact=impact, confidence=confidence, **kw)


# ------------------------------------------------------------------------ emit
def test_emit_requires_evidence():
    with pytest.raises(proposals.ProposalError, match="evidence"):
        proposals.emit(source="janitor", title="x", body="y", evidence=[])


def test_emit_validates_source_and_levels():
    with pytest.raises(proposals.ProposalError):
        proposals.emit(source="rogue", title="x", body="y",
                       evidence=["a.ts:1"])
    with pytest.raises(proposals.ProposalError):
        _emit(impact="huge")


# ----------------------------------------------------------------------- inbox
def test_inbox_ranks_by_impact_times_confidence(session):
    proposals.emit(source="janitor", title="low-low", body="", impact="low",
                   confidence="low", evidence=["a.ts:1"])
    proposals.emit(source="perfector", title="high-high", body="", impact="high",
                   confidence="high", evidence=["b.ts:2"])
    proposals.emit(source="janitor", title="high-med", body="", impact="high",
                   confidence="medium", evidence=["c.ts:3"])
    inbox = proposals.inbox()
    assert [i["title"] for i in inbox] == ["high-high", "high-med", "low-low"]
    assert inbox[0]["rank_score"] == 9 and inbox[1]["rank_score"] == 6


def test_inbox_excludes_decided_by_default(session):
    _emit()
    session.query(Proposal).update({"status": "dismissed"})
    session.commit()
    assert proposals.inbox() == []
    assert len(proposals.inbox(status=None)) == 1


# ---------------------------------------------------------------------- accept
async def test_accept_promotes_to_gated_development_run(session, make_user):
    p = _emit(repo="ClientApp")
    u = make_user()
    rm = FakeRM()
    out = await proposals.accept(p["id"], u.id, rm)
    assert out["status"] == "accepted"
    run = rm.created[0]
    assert run["source"] == "proposal" and run["mode"] == "development"
    assert run["autonomy"] == "gated"
    assert run["repo"] == "ClientApp"
    assert "utils.ts:14" in run["task"]  # evidence carries into the task brief
    stored = session.get(Proposal, p["id"])
    assert stored.promoted_run_id == out["run_id"]


async def test_accept_enforces_weekly_spend_ceiling(session, make_user, monkeypatch):
    monkeypatch.setattr(proposals.get_settings(), "proposals_weekly_ceiling_usd", 10.0)
    p = _emit()
    u = make_user()
    session.add(Run(id="old", created_by=u.id, mode="development",
                    source="proposal", cost_usd=12.0))
    session.commit()
    with pytest.raises(proposals.ProposalError, match="ceiling"):
        await proposals.accept(p["id"], u.id, FakeRM())


async def test_double_decision_rejected(session, make_user):
    p = _emit()
    u = make_user()
    await proposals.accept(p["id"], u.id, FakeRM())
    with pytest.raises(proposals.ProposalError, match="already decided"):
        proposals.dismiss(p["id"], u.id)


# --------------------------------------------------------------------- dismiss
def test_dismiss_feeds_flywheel_preference(session, make_user):
    p = _emit(source="perfector")
    u = make_user()
    proposals.dismiss(p["id"], u.id, reason="we like this duplication")
    item = session.query(KnowledgeItem).one()
    assert item.created_by == u.id
    assert item.scope == "user"          # private until approved — PHI checkpoint
    assert "we like this duplication" in item.content
    assert "perfector" in item.trigger_description
    assert session.get(Proposal, p["id"]).status == "dismissed"
