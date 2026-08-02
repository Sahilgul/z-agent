"""Campaign mode (plan Phase 5 — fleet swarm): ONE task applied across the
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
    missing = sorted(set(repo_names or []) - set(repos))
    if missing:
        raise CampaignError(f"repos not ready: {', '.join(missing)}")
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
    for repo in repos:
        run = await run_manager.create_run(
            source="campaign", initiated_by=user_id, mode_name="development",
            task=f"[campaign #{delivery_id}] {task}", repo=repo, autonomy="gated",
            delivery_id=delivery_id)
        run_ids.append(run.id)
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
