"""fleet-bench: the org's own benchmark, mined from closed
ADO work items — the eval that decides whether the flywheel actually compounds.

F2P/P2P scoring (SWE-bench semantics): a case is RESOLVED when every
fail_to_pass test passes (the fix works) AND every pass_to_pass test still
passes (nothing broke). Scoring is a PURE function — the eval runner executes
tests in a thread (env-gated), but the verdict is deterministic from stored
outcomes, never from agent self-report.

Cases flagged held_out form the distiller's bench-gate pool: distiller
candidates are scored against these BEFORE their approval card is written.
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.db.base import get_session
from app.db.models.eval import EvalCase, EvalRun
from app.db.models.run import Run


class BenchError(ValueError):
    pass


# --------------------------------------------------------------------- cases
def create_case(repo: str, title: str, task_text: str, base_commit: str,
                fail_to_pass: list[str], pass_to_pass: list[str] | None = None,
                work_item_id: int | None = None, held_out: bool = False) -> dict:
    """One mined case. F2P is the soul of the case — a case without a
    fail-to-pass test proves nothing."""
    if not fail_to_pass:
        raise BenchError("eval cases require at least one fail_to_pass test")
    if not base_commit:
        raise BenchError("base_commit pins the pre-fix state — required")
    session = get_session()
    try:
        case = EvalCase(work_item_id=work_item_id, repo=repo, title=title,
                        task_text=task_text, base_commit=base_commit,
                        fail_to_pass=list(fail_to_pass),
                        pass_to_pass=list(pass_to_pass or []), held_out=held_out)
        session.add(case)
        session.commit()
        session.refresh(case)
        return _case(case)
    finally:
        session.close()


def mine_from_work_item(item: dict, held_out_ratio: int = 4) -> dict:
    """Shape an ADO closed-work-item dict into a case. `item` carries what the
    miner extracted (env-gated fetch): id, repo, title, description, the fix
    PR's base commit, and the test names the PR touched (f2p) + the repo's
    standard smoke set (p2p). Every Nth case is held out for the distiller."""
    f2p = [t for t in (item.get("touched_tests") or []) if t]
    case = create_case(
        repo=item["repo"], title=item.get("title", f"work item {item['id']}"),
        task_text=item.get("description") or item.get("title", ""),
        base_commit=item["base_commit"], fail_to_pass=f2p,
        pass_to_pass=item.get("smoke_tests") or [],
        work_item_id=int(item["id"]),
        held_out=(int(item["id"]) % held_out_ratio == 0),
    )
    return case


def list_cases(held_out: bool | None = None) -> list[dict]:
    session = get_session()
    try:
        q = session.query(EvalCase)
        if held_out is not None:
            q = q.filter_by(held_out=held_out)
        return [_case(c) for c in q.order_by(EvalCase.id).all()]
    finally:
        session.close()


def _case(c: EvalCase) -> dict:
    return {"id": c.id, "work_item_id": c.work_item_id, "repo": c.repo,
            "title": c.title, "base_commit": c.base_commit,
            "fail_to_pass": c.fail_to_pass, "pass_to_pass": c.pass_to_pass,
            "held_out": c.held_out}


# -------------------------------------------------------------------- scoring
def score(case: EvalCase, outcomes: dict[str, bool]) -> dict:
    """SWE-bench verdict from test outcomes. Unknown tests count as FAILED —
    an unrun test is not a passed test."""
    f2p = [t for t in case.fail_to_pass if outcomes.get(t) is True]
    p2p = [t for t in case.pass_to_pass if outcomes.get(t) is True]
    resolved = (len(f2p) == len(case.fail_to_pass)
                and len(p2p) == len(case.pass_to_pass))
    return {"resolved": resolved, "f2p_passed": len(f2p), "f2p_total": len(case.fail_to_pass),
            "p2p_passed": len(p2p), "p2p_total": len(case.pass_to_pass)}


# --------------------------------------------------------------------- runner
async def start_eval(case_id: int, run_manager) -> dict:
    """Run a Development mode against the case at base_commit (real execution
    is env-gated; the run + eval row are recorded here either way)."""
    session = get_session()
    try:
        case = session.get(EvalCase, case_id)
        if case is None:
            raise BenchError("case not found")
        repo, title, task, base = case.repo, case.title, case.task_text, case.base_commit
    finally:
        session.close()
    from app.services import identity
    run = await run_manager.create_run(
        source="bench", initiated_by=identity.system_user_id(), mode_name="development",
        task=f"[bench] {title}\n\n{task}\n\nBase commit: {base} — implement the "
             "fix, run the repo tests, do not touch the listed test files.",
        repo=repo, autonomy="gated")
    session = get_session()
    try:
        ev = EvalRun(case_id=case_id, run_id=run.id)
        session.add(ev)
        session.commit()
        session.refresh(ev)
        return {"eval_id": ev.id, "run_id": run.id}
    finally:
        session.close()


def record_result(eval_id: int, outcomes: dict[str, bool]) -> dict:
    """The runner feeds stored test outcomes; the verdict is computed HERE."""
    session = get_session()
    try:
        ev = session.get(EvalRun, eval_id)
        if ev is None:
            raise BenchError("eval run not found")
        case = session.get(EvalCase, ev.case_id)
        verdict = score(case, outcomes)
        ev.resolved = verdict["resolved"]
        ev.f2p_passed = verdict["f2p_passed"]
        ev.p2p_passed = verdict["p2p_passed"]
        ev.report = {**verdict, "outcomes": outcomes}
        session.commit()
        return verdict
    finally:
        session.close()


# -------------------------------------------------------------------- reports
def report(before: datetime | None = None, after: datetime | None = None) -> dict:
    """Resolution-rate + cost-per-PR over a window (default: all time)."""
    session = get_session()
    try:
        q = session.query(EvalRun)
        if before:
            q = q.filter(EvalRun.created_at < before)
        if after:
            q = q.filter(EvalRun.created_at >= after)
        evals = q.all()
        total = len(evals)
        resolved = sum(1 for e in evals if e.resolved)
        run_ids = [e.run_id for e in evals if e.run_id]
        cost = 0.0
        if run_ids:
            cost = sum(r.cost_usd for r in
                       session.query(Run).filter(Run.id.in_(run_ids)).all())
        # L-16: cost is summed over evals WITH a run_id, so divide by that
        # count — the old `cost / total` spread the run-cost over ALL evals
        # (including no-run ones), understating cost-per-eval for the runs
        # that actually executed.
        evaluated = len(run_ids)
        return {
            "evals": total, "resolved": resolved,
            "resolution_rate": (resolved / total) if total else 0.0,
            "cost_total_usd": round(cost, 4),
            "cost_per_eval_usd": round(cost / evaluated, 4) if evaluated else 0.0,
        }
    finally:
        session.close()


def before_after(split: datetime) -> dict:
    """The compounding proof: bench performance before vs after a cutover
    (distiller adoption, model swap, guidebook rollout)."""
    # M-30: the API query param can arrive tz-aware while EvalRun.created_at is
    # naive UTC; comparing offset-aware vs offset-naive raises TypeError ->
    # 500. Normalize split to naive UTC before the comparison.
    if split.tzinfo is not None:
        split = split.astimezone(UTC).replace(tzinfo=None)
    return {"before": report(before=split), "after": report(after=split),
            "split": split.isoformat()}


def bench_gate(candidate_label: str, verdicts: list[dict]) -> dict:
    """The distiller's gate: a candidate ships to the approval inbox ONLY if it
    doesn't regress the held-out pool (resolved count may not drop). The card
    shows this delta."""
    resolved = sum(1 for v in verdicts if v["resolved"])
    total = len(verdicts)
    return {"candidate": candidate_label, "resolved": resolved, "total": total,
            "rate": (resolved / total) if total else 0.0}
