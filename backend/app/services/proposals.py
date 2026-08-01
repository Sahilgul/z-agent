"""Improvement Inbox (plan §6 Phase 4): ranked proposals from the Janitor
(hygiene patrol) and Perfector (product research).

Both patrol lanes are READ-ONLY and emit proposals — NEVER unsolicited PRs.
Ranking is impact × confidence (deterministic, no model in the sort). Accept
turns a proposal into a normal gated Development run (weekly spend ceiling
enforced HERE, in code); Dismiss feeds a preference signal into the flywheel
as a user-scoped knowledge draft — the personalization loop on top of the
knowledge loop. Team-wide readable: proposals cite code, not sessions.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.core.config import get_settings
from app.core.logging import get_logger
from app.db.base import get_session
from app.db.models.proposal import Proposal
from app.db.models.run import Run
from app.services import knowledge

log = get_logger(service="proposals")

SOURCES = {"janitor", "perfector"}
LEVELS = {"low": 1, "medium": 2, "high": 3}


class ProposalError(ValueError):
    pass


def _serialize(p: Proposal) -> dict:
    score = LEVELS.get(p.impact, 0) * LEVELS.get(p.confidence, 0)
    return {
        "id": p.id, "source": p.source, "repo": p.repo, "title": p.title,
        "body": p.body, "evidence": p.evidence, "impact": p.impact,
        "confidence": p.confidence, "rank_score": score, "status": p.status,
        "promoted_run_id": p.promoted_run_id, "created_by": p.created_by,
        "created_at": p.created_at.isoformat() if p.created_at else None,
    }


def emit(source: str, title: str, body: str, evidence: list[str] | None = None,
         impact: str = "medium", confidence: str = "medium",
         repo: str | None = None, created_by: str = "system") -> dict:
    """What the patrol lanes call. Evidence is REQUIRED — a proposal without
    file:line citations is an opinion, and opinions don't rank."""
    if source not in SOURCES:
        raise ProposalError(f"source must be one of {sorted(SOURCES)}")
    if impact not in LEVELS or confidence not in LEVELS:
        raise ProposalError("impact/confidence must be low|medium|high")
    evidence = list(evidence or [])
    if not evidence:
        raise ProposalError("proposals require file:line evidence")
    session = get_session()
    try:
        p = Proposal(source=source, title=title, body=body, evidence=evidence,
                     impact=impact, confidence=confidence, repo=repo,
                     created_by=created_by)
        session.add(p)
        session.commit()
        session.refresh(p)
        return _serialize(p)
    finally:
        session.close()


def inbox(status: str | None = "proposed") -> list[dict]:
    """Ranked: impact × confidence desc, newest first on ties."""
    session = get_session()
    try:
        q = session.query(Proposal)
        if status:
            q = q.filter_by(status=status)
        rows = q.order_by(Proposal.id.desc()).all()
        items = [_serialize(r) for r in rows]
        items.sort(key=lambda i: (-i["rank_score"], -i["id"]))
        return items
    finally:
        session.close()


def _weekly_spend() -> float:
    since = datetime.now(timezone.utc) - timedelta(days=7)
    session = get_session()
    try:
        runs = (session.query(Run).filter_by(source="proposal")
                .filter(Run.created_at >= since).all())
        return sum(r.cost_usd for r in runs)
    finally:
        session.close()


def _get(proposal_id: int) -> Proposal:
    session = get_session()
    try:
        p = session.get(Proposal, proposal_id)
        if p is None:
            raise ProposalError("proposal not found")
        session.expunge(p)
        return p
    finally:
        session.close()


def task_for(p: Proposal) -> str:
    lines = [f"# {p.title}", "", p.body[:2000], "", "## Evidence"]
    lines += [f"- {e}" for e in p.evidence[:20]]
    if p.repo:
        lines.insert(0, f"Repo scope: {p.repo}")
    return "\n".join(lines)


async def accept(proposal_id: int, user_id: int, run_manager) -> dict:
    """Proposal → normal gated Development run. The weekly spend ceiling is
    enforced in code — a patrol must never surprise the budget."""
    p = _get(proposal_id)
    if p.status != "proposed":
        raise ProposalError("proposal already decided")
    ceiling = get_settings().proposals_weekly_ceiling_usd
    spend = _weekly_spend()
    if spend >= ceiling:
        raise ProposalError(
            f"weekly proposal spend ceiling reached (${spend:.2f} >= ${ceiling:.2f})")
    run = await run_manager.create_run(
        source="proposal", initiated_by=user_id, mode_name="development",
        task=task_for(p), repo=p.repo, autonomy="gated")
    session = get_session()
    try:
        row = session.get(Proposal, proposal_id)
        row.status = "accepted"
        row.promoted_run_id = run.id
        session.commit()
    finally:
        session.close()
    return {"run_id": run.id, "status": "accepted"}


def dismiss(proposal_id: int, user_id: int, reason: str = "") -> dict:
    """Dismiss → the preference feeds the flywheel as a user-scoped knowledge
    draft (private until approved — same PHI checkpoint as every draft)."""
    p = _get(proposal_id)
    if p.status != "proposed":
        raise ProposalError("proposal already decided")
    session = get_session()
    try:
        row = session.get(Proposal, proposal_id)
        row.status = "dismissed"
        session.commit()
    finally:
        session.close()
    knowledge.draft(
        content=(f"Dismissed {p.source} proposal '{p.title}'"
                 + (f" — reason: {reason}" if reason else "")),
        trigger_description=f"when ranking {p.source} proposals for this teammate",
        created_by=user_id, proposed_scope="user")
    return {"status": "dismissed"}
