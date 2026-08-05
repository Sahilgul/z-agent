"""Knowledge flywheel service.

Corpus lifecycle: items are born DRAFTS, and drafts are ALWAYS scoped ``user``
— the deterministic PHI checkpoint (drafts distilled from PHI-bearing runs
never enter the shared corpus before a human sees the diff). Approval is where
a private episode becomes a shared fact: the approver picks the promoted scope
(global | repo | user). Retrieval reads APPROVED items only.

Retrieval (run start, per run not per thread): search space = approved global +
approved repo-scoped (run repo) + your own approved user-scoped items + your
OWN trajectory_summaries (episodic recall, un-gated — your history is yours).
A cheap model reranks candidates by trigger_description against the task text;
top-k are pinned into every thread prompt of the run. Any gateway failure falls
back to deterministic lexical ranking — retrieval must never fail a run.
~200 rows: no embeddings (the RAG ban is for code, not curated rows).
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone

import httpx

from app.core.config import get_settings
from app.core.logging import get_logger
from app.db.base import get_session
from app.db.models.approval import Approval
from app.db.models.knowledge import KnowledgeItem
from app.db.models.trajectory import TrajectorySummary

log = get_logger(service="knowledge")

SCOPES = {"global", "repo", "user"}

# Per-run prompt-block cache: the first thread of a run pays the rerank, the
# rest reuse the block. Single-host local era; cross-host store lands later.
_block_cache: dict[str, str] = {}


class KnowledgeError(ValueError):
    pass


def _serialize(item: KnowledgeItem) -> dict:
    return {
        "id": item.id, "content": item.content,
        "trigger_description": item.trigger_description,
        "scope": item.scope, "repo": item.repo, "status": item.status,
        "created_by": item.created_by, "source_run_id": item.source_run_id,
        "created_at": item.created_at.isoformat() if item.created_at else None,
    }


def draft(content: str, trigger_description: str, created_by: int,
          repo: str | None = None, source_run_id: str | None = None,
          proposed_scope: str = "global",
          extra_payload: dict | None = None) -> KnowledgeItem:
    """Create a draft item. The PHI checkpoint is enforced HERE, not at the
    API: whatever scope the caller asks for, a draft is stored scope=user and
    only approval can promote it into the shared corpus."""
    if proposed_scope not in SCOPES:
        raise KnowledgeError(f"scope must be one of {sorted(SCOPES)}")
    session = get_session()
    try:
        item = KnowledgeItem(
            content=content, trigger_description=trigger_description,
            scope="user", repo=repo, created_by=created_by,
            source_run_id=source_run_id, status="draft",
        )
        session.add(item)
        session.flush()  # item.id for the approval payload
        if source_run_id is not None:
            import uuid as _uuid
            session.add(Approval(
                id=str(_uuid.uuid4()), run_id=source_run_id, kind="knowledge",
                payload={"item_id": item.id, "content": content,
                         "trigger_description": trigger_description,
                         "proposed_scope": proposed_scope, "repo": repo,
                         **(extra_payload or {})},
            ))
        session.commit()
        session.refresh(item)
        return item
    finally:
        session.close()


def mined_run_ids() -> set[str]:
    """Runs already mined by the Sleep-Time Distiller — any knowledge item
    carrying the run as source counts, whatever its status."""
    session = get_session()
    try:
        rows = (session.query(KnowledgeItem.source_run_id)
                .filter(KnowledgeItem.source_run_id.isnot(None)).all())
        return {r[0] for r in rows}
    finally:
        session.close()


def mark_mined(run_id: str, user_id: int) -> None:
    """Mark a run as mined by the distiller WITHOUT drafting a candidate
    (H-31). A KnowledgeItem with source_run_id puts the run in
    mined_run_ids so it isn't re-mined every night. No Approval card is
    created (this is a mining marker, not a draft for review) and the
    status is "rejected" so it never promotes into the shared corpus."""
    session = get_session()
    try:
        item = KnowledgeItem(
            content="(distiller mined — no candidate)", trigger_description="",
            scope="user", created_by=user_id, source_run_id=run_id,
            status="rejected",
        )
        session.add(item)
        session.commit()
    finally:
        session.close()


def pending() -> list[dict]:
    """Draft items, team-wide (shared-by-design exception: the corpus and
    its approval cards are visible to every teammate)."""
    session = get_session()
    try:
        rows = (session.query(KnowledgeItem).filter_by(status="draft")
                .order_by(KnowledgeItem.id.desc()).all())
        return [_serialize(r) for r in rows]
    finally:
        session.close()


def corpus_for(user_id: int) -> list[dict]:
    """What a teammate sees browsing the corpus: approved shared items plus ALL
    of their own items (drafts included — your own history is yours)."""
    session = get_session()
    try:
        shared = (session.query(KnowledgeItem)
                  .filter(KnowledgeItem.status == "approved",
                          KnowledgeItem.scope.in_(["global", "repo"])).all())
        own = (session.query(KnowledgeItem)
               .filter(KnowledgeItem.created_by == user_id).all())
        seen = {r.id for r in shared}
        rows = list(shared) + [r for r in own if r.id not in seen]
        rows.sort(key=lambda r: r.id, reverse=True)
        return [_serialize(r) for r in rows]
    finally:
        session.close()


def _resolve_linked_approval(session, item: KnowledgeItem, decision: str,
                             decided_by: int) -> None:
    if not item.source_run_id:
        return
    # H-29: a run can have MULTIPLE pending knowledge approvals (one per
    # draft). The old .one_or_none() raised MultipleResultsFound on the
    # second draft's approval, leaving the PHI checkpoint deadlocked.
    # Query all pending knowledge cards for the run and match the
    # specific item_id in Python (payload is JSON; the item_id filter is
    # dialect-neutral here).
    cards = (session.query(Approval)
            .filter_by(run_id=item.source_run_id, kind="knowledge", decision=None)
            .all())
    for card in cards:
        if card.payload.get("item_id") == item.id:
            card.decision = decision
            card.decided_by = decided_by
            card.decided_at = datetime.now(timezone.utc)
            break


def approve(item_id: int, scope: str, decided_by: int,
            repo: str | None = None) -> dict:
    """Promote a draft into the corpus at the chosen scope. Approving to
    scope=user is legal (a private fact that stays private)."""
    if scope not in SCOPES:
        raise KnowledgeError(f"scope must be one of {sorted(SCOPES)}")
    session = get_session()
    try:
        item = session.get(KnowledgeItem, item_id)
        if item is None or item.status != "draft":
            raise KnowledgeError("knowledge item not found or already decided")
        if scope == "repo" and not (repo or item.repo):
            raise KnowledgeError("repo scope requires a repo")
        item.status = "approved"
        item.scope = scope
        if repo:
            item.repo = repo
        _resolve_linked_approval(session, item, "approved", decided_by)
        session.commit()
        return _serialize(item)
    finally:
        session.close()


def reject(item_id: int, decided_by: int) -> dict:
    session = get_session()
    try:
        item = session.get(KnowledgeItem, item_id)
        if item is None or item.status != "draft":
            raise KnowledgeError("knowledge item not found or already decided")
        item.status = "rejected"
        _resolve_linked_approval(session, item, "denied", decided_by)
        session.commit()
        return _serialize(item)
    finally:
        session.close()


# ---------------------------------------------------------------- retrieval

def _search_space(user_id: int, repo: str | None) -> list[dict]:
    """Approved global + approved repo-scoped (run repo) + your own approved
    user items + your own trajectory_summaries (un-gated episodic recall)."""
    session = get_session()
    try:
        items = (session.query(KnowledgeItem)
                 .filter(KnowledgeItem.status == "approved")
                 .filter(
                     (KnowledgeItem.scope == "global")
                     | ((KnowledgeItem.scope == "repo") & (KnowledgeItem.repo == (repo or "")))
                     | ((KnowledgeItem.scope == "user") & (KnowledgeItem.created_by == user_id)))
                 .all())
        candidates = [{
            "kind": "knowledge", "id": f"k{i.id}", "scope": i.scope, "repo": i.repo,
            "trigger": i.trigger_description, "content": i.content,
        } for i in items]
        trajs = (session.query(TrajectorySummary).filter_by(user_id=user_id)
                 .order_by(TrajectorySummary.id.desc()).limit(50).all())
        candidates += [{
            "kind": "trajectory", "id": f"t{t.id}", "scope": "user", "repo": None,
            "trigger": t.summary[:400], "content": t.summary,
        } for t in trajs]
        return candidates
    finally:
        session.close()


_WORD_RE = re.compile(r"[a-z0-9_]+")


def lexical_rank(task_text: str, candidates: list[dict]) -> list[str]:
    """Deterministic fallback: token-overlap between the task and each
    candidate's trigger_description. Ties break by id for stable ordering."""
    task_tokens = set(_WORD_RE.findall(task_text.lower()))
    scored: list[tuple[int, str]] = []
    for c in candidates:
        tokens = set(_WORD_RE.findall(str(c["trigger"]).lower()))
        scored.append((len(task_tokens & tokens), c["id"]))
    scored.sort(key=lambda t: (-t[0], t[1]))
    return [cid for score, cid in scored if score > 0]


async def llm_rerank(task_text: str, candidates: list[dict]) -> list[str]:
    """Cheap-model rerank through the LiteLLM gateway: one call, JSON list of
    candidate ids ordered by relevance. Raises on any gateway/schema problem —
    the caller falls back to lexical_rank."""
    settings = get_settings()
    catalog = [{"id": c["id"], "trigger": c["trigger"][:300]} for c in candidates]
    body = {
        "model": settings.knowledge_rerank_model,
        "messages": [
            {"role": "system", "content": (
                "You rank knowledge items by relevance to a task. Reply with ONLY "
                "a JSON array of candidate ids, most relevant first. Omit irrelevant ids.")},
            {"role": "user", "content": json.dumps({"task": task_text[:2000], "candidates": catalog})},
        ],
        "temperature": 0,
    }
    async with httpx.AsyncClient(timeout=settings.knowledge_rerank_timeout_seconds) as client:
        resp = await client.post(
            f"{settings.gateway_url}/chat/completions",
            headers={"Authorization": f"Bearer {settings.litellm_master_key}"},
            json=body,
        )
        resp.raise_for_status()
    text = resp.json()["choices"][0]["message"]["content"]
    match = re.search(r"\[.*\]", text, re.DOTALL)
    if not match:
        raise KnowledgeError("rerank reply contained no JSON array")
    ids = json.loads(match.group(0))
    valid = {c["id"] for c in candidates}
    return [str(i) for i in ids if str(i) in valid]


async def rerank(task_text: str, candidates: list[dict], ranker=None) -> list[dict]:
    """Rerank candidates against the task; returns the top-k candidate dicts.
    ``ranker`` is injectable for tests; the default is llm_rerank with a
    lexical fallback so retrieval never fails a run."""
    if not candidates:
        return []
    top_k = get_settings().knowledge_top_k
    if ranker is None:
        async def _default(text: str, cands: list[dict]) -> list[str]:
            try:
                return await llm_rerank(text, cands)
            except Exception as exc:
                log.info("rerank falling back to lexical", error=str(exc)[:160])
                return lexical_rank(text, cands)
        ranker = _default
    ordered_ids = await ranker(task_text, candidates)
    by_id = {c["id"]: c for c in candidates}
    return [by_id[i] for i in ordered_ids[:top_k] if i in by_id]


def render_block(pinned: list[dict]) -> str:
    """Render pinned candidates as the thread-prompt knowledge block."""
    if not pinned:
        return ""
    knowledge = [p for p in pinned if p["kind"] == "knowledge"]
    episodic = [p for p in pinned if p["kind"] == "trajectory"]
    chunks: list[str] = []
    if knowledge:
        lines = [f"- [{k['scope']}{':' + k['repo'] if k['repo'] else ''}] {k['content']}"
                 for k in knowledge]
        chunks.append("--- Pinned knowledge (flywheel, human-approved) ---\n" + "\n".join(lines))
    if episodic:
        lines = [f"- (run {e['id'][1:]}) {e['content'][:600]}" for e in episodic]
        chunks.append("--- Your past runs (episodic recall) ---\n" + "\n".join(lines))
    return "\n\n" + "\n\n".join(chunks) + "\n"


async def prompt_block_for_run(run_id: str, task_text: str, user_id: int,
                               repo: str | None, ranker=None) -> str:
    """The per-run pinned-knowledge block. Cached per run_id so only the first
    thread of a run pays the rerank; swarm threads share the block."""
    if run_id in _block_cache:
        return _block_cache[run_id]
    pinned = await rerank(task_text, _search_space(user_id, repo), ranker=ranker)
    block = render_block(pinned)
    _block_cache[run_id] = block
    return block


def clear_run_cache(run_id: str) -> None:
    _block_cache.pop(run_id, None)
