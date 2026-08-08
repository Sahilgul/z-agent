"""Sleep-Time Distiller: nightly consolidation — mine
trajectory_summaries, draft knowledge candidates, score them against the
held-out bench pool BEFORE the approval inbox (the card shows the bench
delta). Memory consolidation that must prove it doesn't regress the fleet.

Mining bookkeeping lives in the Approval card payload + the candidate's
knowledge draft (source_run_id) — no new columns. Candidates enter the SAME
knowledge draft inbox as every other draft: PHI checkpoint applies, a human
approves, nothing auto-ships.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from app.core.config import get_settings
from app.core.logging import get_logger
from app.db.base import get_session
from app.db.models.eval import EvalCase
from app.db.models.trajectory import TrajectorySummary
from app.services import bench, knowledge

log = get_logger(service="distiller")

DISTILL_PERSONA = (
    "You are the Sleep-Time Distiller. You read per-thread trajectory summaries "
    "from completed runs and extract REUSABLE knowledge — patterns that would "
    "have made the run shorter or safer. Reply with ONLY a JSON array of "
    "objects: {content, trigger_description}. Skip one-off facts, skip anything "
    "PHI-shaped (patient names, MRNs, dates of service). If nothing generalizes, "
    "reply []."
)


class DistillerError(ValueError):
    pass


def unmined_since(days: int = 1, limit: int = 100) -> list[dict]:
    """Summaries written in the window that have no distiller candidate yet —
    a summary is mined if a knowledge item carries source_run_id of its run."""
    since = datetime.now(UTC) - timedelta(days=days)
    session = get_session()
    try:
        rows = (session.query(TrajectorySummary)
                .filter(TrajectorySummary.created_at >= since)
                .order_by(TrajectorySummary.id).limit(limit).all())
        mined_run_ids = knowledge.mined_run_ids()
        return [{"id": r.id, "run_id": r.run_id, "user_id": r.user_id,
                 "summary": r.summary, "lessons": r.lessons,
                 "key_decisions": r.key_decisions}
                for r in rows if r.run_id not in mined_run_ids]
    finally:
        session.close()


async def distill(summaries: list[dict], complete=None) -> list[dict]:
    """LLM consolidation (injectable). Always returns a list; a gateway failure
    or malformed reply yields NO candidates, never a crash — sleep is optional."""
    if not summaries:
        return []
    if complete is None:
        from app.services.ideas import gateway_complete as complete  # shared caller
    corpus = json.dumps([
        {"summary": s["summary"][:1200], "lessons": s["lessons"][:8],
         "key_decisions": s["key_decisions"][:8]}
        for s in summaries[:20]
    ])
    try:
        reply = await complete(DISTILL_PERSONA, corpus,
                               model=get_settings().ideas_model)
        data = json.loads(reply)
        if not isinstance(data, list):
            raise ValueError("distiller reply was not a list")
        return [d for d in data
                if isinstance(d, dict) and d.get("content") and d.get("trigger_description")][:5]
    except Exception as exc:
        # A transient gateway/parse failure must NOT mark runs mined — the
        # lessons would be lost permanently. Propagate so run_nightly aborts
        # before any marking; the next night's sweep retries the same runs.
        log.warning("distill failed — runs left unmined for retry",
                    error=str(exc))
        raise


def _held_out_cases() -> list[int]:
    session = get_session()
    try:
        return [c.id for c in session.query(EvalCase).filter_by(held_out=True).all()]
    finally:
        session.close()


def bench_delta(candidate_label: str, scorer=None) -> dict:
    """Score the candidate against held-out cases vs the current baseline.
    The scorer is env-gated (real eval runs); injectable for tests. With no
    scorer or no held-out pool the delta is neutral — the card says so."""
    cases = _held_out_cases()
    if not cases or scorer is None:
        return {"candidate": candidate_label, "delta": 0.0, "baseline_rate": None,
                "candidate_rate": None, "note": "no held-out pool or scorer — ungated"}
    baseline = bench.report()["resolution_rate"]
    verdicts = scorer(cases)
    gate = bench.bench_gate(candidate_label, verdicts)
    return {"candidate": candidate_label, "delta": round(gate["rate"] - baseline, 4),
            "baseline_rate": baseline, "candidate_rate": gate["rate"],
            "passes": gate["rate"] >= baseline}


async def run_nightly(complete=None, scorer=None, days: int = 1) -> dict:
    """The whole night shift, fully fakeable: mine → distill → bench-gate →
    draft into the knowledge inbox (user-scoped — PHI checkpoint) + ONE summary
    approval card carrying the bench delta."""
    summaries = unmined_since(days=days)
    if not summaries:
        return {"mined": 0, "candidates": 0, "cards": 0}
    try:
        candidates = await distill(summaries, complete=complete)
    except Exception:
        # Distillation failed transiently: mark NOTHING mined so tonight's
        # lessons are retried, and surface the failure in the result.
        return {"mined": 0, "candidates": 0, "cards": 0,
                "error": "distill_failed"}
    if not candidates:
        # Legitimately no candidates (model said nothing generalizable) —
        # safe to mark mined below; nothing was lost.
        log.info("distiller returned no candidates", summaries=len(summaries))
    drafted = 0
    mined_run_ids: set[str] = set()
    for i, cand in enumerate(candidates):
        label = cand["content"][:60]
        delta = bench_delta(label, scorer=scorer)
        if delta.get("passes") is False:
            # No silent skip: a bench-regressing candidate disappears without
            # a trace unless we RECORD the rejection — draft it with status
            # "rejected" so the inbox shows it was considered and why.
            src = summaries[i % len(summaries)]
            log.info("distiller candidate regressed bench, rejected", label=label)
            knowledge.draft(content=cand["content"],
                            trigger_description=cand["trigger_description"],
                            created_by=src["user_id"], proposed_scope="user",
                            source_run_id=src["run_id"],
                            status="rejected",
                            extra_payload={"bench_delta": delta,
                                           "rejection_reason": "bench_regression"})
            mined_run_ids.add(src["run_id"])
            continue
        # H-31: attribute each candidate round-robin to a REAL summary's
        # user/run — the old code attributed every candidate to summaries[0],
        # leaking other users' distilled lessons into summaries[0]'s user
        # scope (privacy) and only marking summaries[0]'s run as mined so
        # the rest were re-mined every night (infinite re-mining).
        src = summaries[i % len(summaries)]
        knowledge.draft(content=cand["content"],
                        trigger_description=cand["trigger_description"],
                        created_by=src["user_id"], proposed_scope="user",
                        source_run_id=src["run_id"],
                        extra_payload={"bench_delta": delta})
        mined_run_ids.add(src["run_id"])
        drafted += 1
    # H-31: mark every OTHER summarized run as mined so it isn't re-mined
    # every night (the old code only marked summaries[0]'s run).
    for s in summaries:
        if s["run_id"] not in mined_run_ids:
            knowledge.mark_mined(s["run_id"], s["user_id"])
    # each draft created its own approval card with the bench delta in payload
    return {"mined": len(summaries), "candidates": drafted, "cards": drafted}
