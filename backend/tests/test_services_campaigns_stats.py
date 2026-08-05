"""Campaign mode + cost dashboard tests: launch validation,
delivery rollup, run↔delivery linkage, dashboard aggregation math.
"""

import pytest

from app.db.models.delivery import Delivery, PrLink
from app.db.models.repo import Repo
from app.db.models.run import Run
from app.services import campaigns, stats


class FakeRM:
    def __init__(self):
        self.created = []

    async def create_run(self, source, initiated_by, mode_name, task,
                         repo=None, autonomy=None, delivery_id=None, **kw):
        import uuid
        run = type("R", (), {"id": str(uuid.uuid4())})()
        self.created.append({"id": run.id, "source": source, "repo": repo,
                             "task": task, "delivery_id": delivery_id,
                             "autonomy": autonomy})
        return run


def _repos(session, *names):
    for n in names:
        session.add(Repo(name=n, integration_branch="main", status="ready"))
    session.commit()


# --------------------------------------------------------------------- launch
async def test_launch_fans_out_one_gated_run_per_repo(session, make_user):
    _repos(session, "ServerApp", "ClientApp", "Billing-Engine")
    u = make_user()
    rm = FakeRM()
    out = await campaigns.launch("migrate logging to structured JSON", None, u.id, rm)
    assert out["repos"] == ["Billing-Engine", "ClientApp", "ServerApp"]
    assert len(out["run_ids"]) == 3
    assert all(c["source"] == "campaign" and c["autonomy"] == "gated" for c in rm.created)
    assert all(c["delivery_id"] == out["delivery_id"] for c in rm.created)
    assert all("[campaign #" in c["task"] for c in rm.created)


async def test_launch_scoped_subset_and_missing_repos(session, make_user):
    _repos(session, "ServerApp", "ClientApp")
    u = make_user()
    out = await campaigns.launch("bump timeouts", ["ClientApp"], u.id, FakeRM())
    assert out["repos"] == ["ClientApp"]
    with pytest.raises(campaigns.CampaignError, match="not ready"):
        await campaigns.launch("x task", ["Ghost-Repo"], u.id, FakeRM())


async def test_launch_includes_ready_no_map_repos(session, make_user):
    """Onboarding lands every repo at ready-no-map until the map
    generator exists, so filtering on 'ready' alone matched the whole fleet out
    of every campaign."""
    session.add(Repo(name="ServerApp", integration_branch="main", status="ready-no-map"))
    session.add(Repo(name="Archived-One", integration_branch="main", status="archived"))
    session.commit()
    out = await campaigns.launch("bump timeouts", None, make_user().id, FakeRM())
    assert out["repos"] == ["ServerApp"]


async def test_launch_rejects_empty_fleet(session, make_user):
    u = make_user()
    with pytest.raises(campaigns.CampaignError, match="no ready repos"):
        await campaigns.launch("x task", None, u.id, FakeRM())


# -------------------------------------------------------------------- rollup
def test_list_deliveries_rolls_up_stages_costs_and_prs(session, make_user):
    u = make_user()
    session.add(Delivery(id=1, title="logging migration", created_by=u.id))
    session.add(Run(id="r1", created_by=u.id, mode="development", stage="developing",
                    delivery_id=1, cost_usd=1.2))
    session.add(Run(id="r2", created_by=u.id, mode="development", stage="completed",
                    delivery_id=1, cost_usd=0.8))
    session.add(Run(id="unrelated", created_by=u.id, mode="ask", stage="completed"))
    session.add(PrLink(run_id="r2", repo="ServerApp", branch="b", ado_pr_id=42,
                       delivery_id=1, status="open"))
    session.commit()
    items = campaigns.list_deliveries()
    assert len(items) == 1
    d = items[0]
    assert d["runs"] == 2 and d["stages"] == {"developing": 1, "completed": 1}
    assert d["cost_usd"] == 2.0
    assert d["prs"] == [{"repo": "ServerApp", "ado_pr_id": 42, "status": "open"}]


# ------------------------------------------------------------------ dashboard
def test_cost_dashboard_aggregates(session, make_user):
    u = make_user(display_name="Ali Raza")
    session.add(Run(id="r1", created_by=u.id, mode="development", repo="ServerApp",
                    cost_usd=2.0, tokens=1000))
    session.add(Run(id="r2", created_by=u.id, mode="ask", repo="ServerApp",
                    cost_usd=0.5, tokens=250))
    session.add(Run(id="r3", created_by=u.id, mode="development", repo=None,
                    cost_usd=1.0, tokens=500))
    session.commit()
    dash = stats.cost_dashboard(days=30)
    assert dash["total"] == {"cost_usd": 3.5, "tokens": 1750, "runs": 3}
    assert dash["by_mode"]["development"]["cost_usd"] == 3.0
    assert dash["by_mode"]["ask"]["runs"] == 1
    assert dash["by_repo"]["ServerApp"]["cost_usd"] == 2.5
    assert dash["by_repo"]["(none)"]["runs"] == 1
    assert dash["by_user"]["Ali Raza"]["tokens"] == 1750
    assert len(dash["by_day"]) == 1  # all today


def test_cost_dashboard_excludes_content(session, make_user):
    u = make_user()
    session.add(Run(id="r1", created_by=u.id, mode="ask", title="secret task",
                    auto_summary="secret summary", cost_usd=1.0))
    session.commit()
    import json as _json
    blob = _json.dumps(stats.cost_dashboard())
    assert "secret task" not in blob and "secret summary" not in blob
