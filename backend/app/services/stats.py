"""Cost/token dashboards (plan Phase 5): METADATA-ONLY team-wide rollups of
runs — costs, tokens, counts. Never content (titles, tasks, events). Gateway
metering lands on Run.cost_usd/tokens; this is the read side."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.db.base import get_session
from app.db.models.run import Run
from app.db.models.user import User


def cost_dashboard(days: int = 30) -> dict:
    since = datetime.now(timezone.utc) - timedelta(days=days)
    session = get_session()
    try:
        runs = (session.query(Run).filter(Run.created_at >= since)
                .order_by(Run.created_at).all())
        names = {u.id: u.display_name for u in session.query(User).all()}
    finally:
        session.close()

    by_day: dict[str, dict] = {}
    by_mode: dict[str, dict] = {}
    by_repo: dict[str, dict] = {}
    by_user: dict[str, dict] = {}

    def bump(bucket: dict, key: str, cost: float, tokens: int) -> None:
        entry = bucket.setdefault(key, {"cost_usd": 0.0, "tokens": 0, "runs": 0})
        entry["cost_usd"] += cost
        entry["tokens"] += tokens
        entry["runs"] += 1

    for r in runs:
        day = r.created_at.date().isoformat()
        bump(by_day, day, r.cost_usd, r.tokens)
        bump(by_mode, r.mode, r.cost_usd, r.tokens)
        bump(by_repo, r.repo or "(none)", r.cost_usd, r.tokens)
        bump(by_user, names.get(r.created_by, f"user {r.created_by}"),
             r.cost_usd, r.tokens)

    def rounded(bucket: dict) -> dict:
        return {k: {**v, "cost_usd": round(v["cost_usd"], 4)} for k, v in bucket.items()}

    return {
        "days": days,
        "total": {"cost_usd": round(sum(r.cost_usd for r in runs), 4),
                  "tokens": sum(r.tokens for r in runs), "runs": len(runs)},
        "by_day": rounded(by_day),
        "by_mode": rounded(by_mode),
        "by_repo": rounded(by_repo),
        "by_user": rounded(by_user),
    }
