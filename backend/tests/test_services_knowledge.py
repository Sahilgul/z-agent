"""Knowledge flywheel service tests: PHI-checkpoint
drafts, team-wide inbox, approval promotion, retrieval search space, and the
rerank path (injected rankers only — no gateway sockets in unit tests).
"""

import pytest

from app.db.models.approval import Approval
from app.db.models.knowledge import KnowledgeItem
from app.db.models.trajectory import TrajectorySummary
from app.services import knowledge

# Bound BEFORE the autouse conftest stub replaces the module attribute, so the
# caching test can restore the real implementation.
_REAL_PROMPT_BLOCK = knowledge.prompt_block_for_run


def _item(session, **kw):
    row = KnowledgeItem(
        content=kw.get("content", "lesson"),
        trigger_description=kw.get("trigger", "when X"),
        scope=kw.get("scope", "global"),
        repo=kw.get("repo"),
        created_by=kw.get("created_by", 1),
        status=kw.get("status", "approved"),
    )
    session.add(row)
    session.commit()
    return row


# ------------------------------------------------------------- PHI checkpoint
def test_draft_forces_user_scope_whatever_was_proposed(session, make_user):
    u = make_user()
    item = knowledge.draft("PHI-ish lesson", "when intake fails", u.id,
                           proposed_scope="global")
    assert item.scope == "user"
    assert item.status == "draft"


def test_draft_rejects_invalid_proposed_scope(session, make_user):
    """G-18: an invalid proposed_scope is rejected at the service layer
    (knowledge.draft) before any row is written — the PHI checkpoint
    enforces scope=user storage regardless, but the proposed value must be
    one of the known scopes or the approval card would carry a bogus scope
    the approver can't act on. Covers the `proposed_scope not in SCOPES`
    guard in draft()."""
    u = make_user()
    with pytest.raises(knowledge.KnowledgeError):
        knowledge.draft("lesson", "trig", u.id, proposed_scope="bogus-scope")
    # Boundary: the valid scopes are accepted (no raise).
    for scope in ("global", "repo", "user"):
        item = knowledge.draft("lesson", "trig", u.id, proposed_scope=scope)
        assert item.scope == "user"  # always stored as user (PHI checkpoint)


def test_draft_from_run_creates_knowledge_approval_card(session, make_user):
    from app.db.models.run import Run
    u = make_user()
    session.add(Run(id="r1", created_by=u.id, mode="development"))
    session.commit()
    item = knowledge.draft("lesson", "trig", u.id, source_run_id="r1")
    card = session.query(Approval).filter_by(run_id="r1", kind="knowledge").one()
    assert card.payload["item_id"] == item.id
    assert card.decision is None


def test_draft_without_run_gets_decidable_card(session, make_user):
    # M-36: a user-authored draft (no source run) used to get NO approval card
    # — orphaned from the card flow (stuck in "draft", never surfaced for
    # review). It now gets a decidable card with run_id=NULL; the decide
    # endpoint acts by approval_id, so the card is decidable.
    u = make_user()
    knowledge.draft("lesson", "trig", u.id)
    card = session.query(Approval).filter_by(kind="knowledge").one()
    assert card.run_id is None
    assert card.decision is None


# ------------------------------------------------------------------- inbox
def test_pending_is_team_wide(session, make_user):
    a = make_user("alice")
    b = make_user("bob")
    knowledge.draft("alice draft", "t", a.id)
    knowledge.draft("bob draft", "t", b.id)
    _item(session, content="already shared")
    pending = knowledge.pending()
    assert len(pending) == 2  # both drafts, regardless of who asks
    assert all(p["status"] == "draft" for p in pending)


def test_corpus_for_shows_shared_plus_own_including_own_drafts(session, make_user):
    a = make_user("alice")
    b = make_user("bob")
    _item(session, content="shared", scope="global")
    _item(session, content="bob private", scope="user", created_by=b.id)
    knowledge.draft("alice wip", "t", a.id)
    corpus = knowledge.corpus_for(a.id)
    contents = {c["content"] for c in corpus}
    assert contents == {"shared", "alice wip"}  # bob's user-scoped item hidden


# ------------------------------------------------------------------ decisions
def test_approve_promotes_scope_and_resolves_card(session, make_user):
    from app.db.models.run import Run
    u = make_user()
    session.add(Run(id="r1", created_by=u.id, mode="development"))
    session.commit()
    item = knowledge.draft("lesson", "trig", u.id, source_run_id="r1")
    out = knowledge.approve(item.id, "global", decided_by=u.id)
    assert out["status"] == "approved" and out["scope"] == "global"
    card = session.query(Approval).filter_by(run_id="r1", kind="knowledge").one()
    assert card.decision == "approved" and card.decided_by == u.id


def test_approve_repo_scope_requires_repo(session, make_user):
    u = make_user()
    item = knowledge.draft("lesson", "trig", u.id)
    with pytest.raises(knowledge.KnowledgeError):
        knowledge.approve(item.id, "repo", decided_by=u.id)
    # W9-M6: repo-scope approvals validate against the registry — register it.
    from app.db.models.repo import Repo
    session.add(Repo(name="ServerApp", integration_branch="main", status="ready"))
    session.commit()
    out = knowledge.approve(item.id, "repo", decided_by=u.id, repo="ServerApp")
    assert out["repo"] == "ServerApp"


def test_approve_to_user_scope_keeps_fact_private(session, make_user):
    u = make_user()
    item = knowledge.draft("my standing instruction", "trig", u.id)
    out = knowledge.approve(item.id, "user", decided_by=u.id)
    assert out["scope"] == "user" and out["status"] == "approved"


def test_decisions_are_final(session, make_user):
    u = make_user()
    item = knowledge.draft("lesson", "trig", u.id)
    knowledge.reject(item.id, decided_by=u.id)
    assert session.get(KnowledgeItem, item.id).status == "rejected"
    with pytest.raises(knowledge.KnowledgeError):
        knowledge.approve(item.id, "global", decided_by=u.id)
    with pytest.raises(knowledge.KnowledgeError):
        knowledge.reject(item.id, decided_by=u.id)


# -------------------------------------------------------------- search space
def test_search_space_scopes(session, make_user):
    a = make_user("alice")
    b = make_user("bob")
    _item(session, content="g", scope="global", trigger="billing flow")
    _item(session, content="r-hit", scope="repo", repo="ServerApp", trigger="drizzle txn")
    _item(session, content="r-miss", scope="repo", repo="ClientApp", trigger="ionic")
    _item(session, content="mine", scope="user", created_by=a.id, trigger="terse plans")
    _item(session, content="bobs", scope="user", created_by=b.id, trigger="his prefs")
    _item(session, content="unapproved", scope="global", status="draft", trigger="draft")
    space = knowledge._search_space(a.id, "ServerApp")
    contents = {c["content"] for c in space}
    assert contents == {"g", "r-hit", "mine"}


def test_search_space_includes_own_trajectories_only(session, make_user):
    a = make_user("alice")
    b = make_user("bob")
    session.add(TrajectorySummary(run_id="r1", user_id=a.id, summary="alice episode"))
    session.add(TrajectorySummary(run_id="r2", user_id=b.id, summary="bob episode"))
    session.commit()
    space = knowledge._search_space(a.id, None)
    kinds = {(c["kind"], c["content"]) for c in space}
    assert ("trajectory", "alice episode") in kinds
    assert ("trajectory", "bob episode") not in kinds


# -------------------------------------------------------------------- rerank
def test_lexical_rank_deterministic_and_excludes_zero_overlap():
    cands = [
        {"id": "k1", "trigger": "drizzle transaction audit log"},
        {"id": "k2", "trigger": "ionic capacitor build"},
        {"id": "k3", "trigger": "transaction retry policy"},
    ]
    ids = knowledge.lexical_rank("add a transaction to the audit flow", cands)
    # M-66: the old assertion (`ids == ["k1","k3"] or ids == ["k3","k1"] or
    # set(ids) == {"k1","k3"}`) accepted ANY order, so a regression that
    # reversed the ranking would pass. lexical_rank sorts by (-score, id):
    # k1 (overlap 2: transaction+audit) ranks above k3 (overlap 1:
    # transaction). Assert the EXACT deterministic order.
    assert ids == ["k1", "k3"]
    assert "k2" not in ids
    # stable across calls
    assert knowledge.lexical_rank("add a transaction to the audit flow", cands) == ids


async def test_rerank_injected_ranker_and_top_k(monkeypatch):
    monkeypatch.setattr(knowledge.get_settings(), "knowledge_top_k", 2)
    cands = [{"id": f"k{i}", "trigger": f"t{i}", "kind": "knowledge"} for i in range(5)]

    async def ranker(task, cs):
        return ["k3", "k1", "k0", "k2", "k4"]

    out = await knowledge.rerank("task", cands, ranker=ranker)
    assert [c["id"] for c in out] == ["k3", "k1"]


async def test_rerank_default_falls_back_to_lexical_on_gateway_failure(monkeypatch):
    async def boom(task, cands):
        raise RuntimeError("gateway down")
    monkeypatch.setattr(knowledge, "llm_rerank", boom)
    cands = [
        {"id": "k1", "trigger": "billing ledger posting", "kind": "knowledge"},
        {"id": "k2", "trigger": "unrelated thing", "kind": "knowledge"},
    ]
    out = await knowledge.rerank("fix the billing ledger", cands)
    assert [c["id"] for c in out] == ["k1"]


async def test_rerank_empty_space_is_empty():
    assert await knowledge.rerank("anything", []) == []


# --------------------------------------------------------------- prompt block
def test_render_block_sections():
    pinned = [
        {"kind": "knowledge", "id": "k1", "scope": "global", "repo": None, "content": "rule one"},
        {"kind": "knowledge", "id": "k2", "scope": "repo", "repo": "ServerApp", "content": "rule two"},
        {"kind": "trajectory", "id": "t9", "scope": "user", "repo": None, "content": "episode"},
    ]
    block = knowledge.render_block(pinned)
    assert "Pinned knowledge" in block and "[global] rule one" in block
    assert "[repo:ServerApp] rule two" in block
    assert "episodic recall" in block and "episode" in block
    assert knowledge.render_block([]) == ""


async def test_prompt_block_cached_per_run(session, make_user, monkeypatch):
    monkeypatch.setattr(knowledge, "prompt_block_for_run", _REAL_PROMPT_BLOCK)
    u = make_user()
    _item(session, content="g", scope="global", trigger="billing")
    calls = {"n": 0}

    async def ranker(task, cs):
        calls["n"] += 1
        return [c["id"] for c in cs]

    knowledge.clear_run_cache("run-x")
    first = await knowledge.prompt_block_for_run("run-x", "billing task", u.id, None, ranker=ranker)
    second = await knowledge.prompt_block_for_run("run-x", "billing task", u.id, None, ranker=ranker)
    assert first == second and calls["n"] == 1
    assert "[global] g" in first
    knowledge.clear_run_cache("run-x")


# ------------------------------------------------------- W9-M5/M6 (web W5a)
def test_pending_serializes_proposed_scope(session, make_user):
    """W9-M5: the inbox selector defaults from proposed_scope, so the row
    must carry it out of pending() (it used to live only in the Approval
    payload, invisible to the knowledge screen)."""
    u = make_user()
    knowledge.draft("lesson", "trig", u.id, proposed_scope="repo", repo="LivekitScribe")
    rows = knowledge.pending()
    assert rows[0]["proposed_scope"] == "repo"


def test_approve_repo_scope_validates_against_registry(session, make_user):
    """W9-M6: free-typed repo names black-holed the lesson — repo scope now
    422s unless the name is a live registry entry."""
    from app.db.models.repo import Repo
    u = make_user()
    item = knowledge.draft("lesson", "trig", u.id)
    with pytest.raises(knowledge.KnowledgeError, match="not in the registry"):
        knowledge.approve(item.id, "repo", u.id, repo="typo-repo")
    session.add(Repo(name="LivekitScribe", integration_branch="main", status="ready"))
    session.commit()
    out = knowledge.approve(item.id, "repo", u.id, repo="LivekitScribe")
    assert out["scope"] == "repo" and out["repo"] == "LivekitScribe"
