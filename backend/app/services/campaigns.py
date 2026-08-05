"""Campaign mode — fleet swarm: ONE task applied across the
fleet — "migrate every repo to the new logging" becomes one Delivery grouping
one gated Development run per repo, each opening its own PR under the same
delivery. The runs are ordinary runs (inbox, trace, HITL) — the Delivery row
is the fleet-wide rollup."""

from __future__ import annotations

from app.db.base import get_session
from app.db.models.delivery import Delivery, PrLink
from app.db.models.repo import Repo, RepoStatus
from app.db.models.run import Run


class CampaignError(ValueError):
    pass


async def launch(task: str, repo_names: list[str] | None, user_id: int,
                 run_manager, title: str = "") -> dict:
    """Validate targets, create the Delivery, fan out one run per repo."""
    session = get_session()
    try:
        q = session.query(Repo).filter(Repo.status.in_(RepoStatus.USABLE))
        if repo_names:
            q = q.filter(Repo.name.in_(repo_names))
        repos = [r.name for r in q.order_by(Repo.name).all()]
    finally:
        session.close()
    # M-43: the old "missing = set(repo_names) - set(repos)" lumped every
    # name that didn't end up in `repos` under one "repos not ready" message,
    # so a typo (a repo that doesn't EXIST) was indistinguishable from a
    # repo that exists but isn't USABLE (not-ready), and a wrong-case name
    # looked identical to both. The caller (and the human reading the 422)
    # couldn't tell whether to register the repo, mark it ready, or fix
    # their casing. Split the missing set into three distinct causes.
    if repo_names:
        from sqlalchemy import func
        lowered = [n.lower() for n in repo_names]
        session = get_session()
        try:
            # Case-insensitive lookup so a wrong-case name still resolves to
            # the existing row (SQLite `IN` is case-sensitive, so a plain
            # Repo.name.in_(repo_names) would miss "ServerApp" when the
            # caller typed "serverapp" and we'd mis-report it as not-found).
            existing = {r.name for r in session.query(Repo.name)
                        .filter(func.lower(Repo.name).in_(lowered)).all()}
        finally:
            session.close()
        existing_lower = {n.lower() for n in existing}
        not_found = sorted(n for n in repo_names if n not in existing)
        wrong_case = sorted(n for n in not_found if n.lower() in existing_lower)
        not_found = sorted(n for n in not_found if n.lower() not in existing_lower)
        not_ready = sorted(set(repo_names) - set(not_found) - set(wrong_case) - set(repos))
        if wrong_case:
            raise CampaignError(
                f"repos not found (wrong case): {', '.join(wrong_case)} "
                f"(known: {', '.join(sorted(existing))})")
        if not_found:
            raise CampaignError(f"repos not found: {', '.join(not_found)}")
        if not_ready:
            raise CampaignError(f"repos not ready: {', '.join(not_ready)}")
    if not repos:
        raise CampaignError("no ready repos match the campaign scope")

    session = get_session()
    try:
        delivery = Delivery(title=title or task[:120], created_by=user_id)
        session.add(delivery)
        session.commit()
        session.refresh(delivery)
        delivery_id = delivery.id
    finally:
        session.close()

    run_ids = []
    try:
        for repo in repos:
            run = await run_manager.create_run(
                source="campaign", initiated_by=user_id, mode_name="development",
                task=f"[campaign #{delivery_id}] {task}", repo=repo, autonomy="gated",
                delivery_id=delivery_id)
            run_ids.append(run.id)
    except Exception:
        # H-34: fanout failed partway — the old code had already committed
        # the Delivery row, leaving an orphaned campaign (a Delivery with
        # only some of its runs). Roll the Delivery back so the campaign
        # never existed; the already-created runs self-reconcile via
        # reconcile_on_boot (orphan runs with a missing delivery_id FK are
        # handled there).
        session = get_session()
        try:
            session.query(Delivery).filter_by(id=delivery_id).delete()
            session.commit()
        finally:
            session.close()
        raise
    return {"delivery_id": delivery_id, "repos": repos, "run_ids": run_ids}


def list_deliveries() -> list[dict]:
    """Fleet rollup: per-delivery run-stage counts and PR links."""
    session = get_session()
    try:
        out = []
        for d in session.query(Delivery).order_by(Delivery.id.desc()).all():
            runs = session.query(Run).filter_by(delivery_id=d.id).all()
            prs = session.query(PrLink).filter_by(delivery_id=d.id).all()
            stages: dict[str, int] = {}
            for r in runs:
                stages[r.stage] = stages.get(r.stage, 0) + 1
            out.append({
                "id": d.id, "title": d.title,
                "created_at": d.created_at.isoformat() if d.created_at else None,
                "runs": len(runs), "stages": stages,
                "cost_usd": round(sum(r.cost_usd for r in runs), 4),
                "prs": [{"repo": p.repo, "ado_pr_id": p.ado_pr_id, "status": p.status}
                        for p in prs],
            })
        return out
    finally:
        session.close()
