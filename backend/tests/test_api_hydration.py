from app.db.models.repo import Repo


def test_my_tickets_returns_bound_tickets(auth_client, session, make_user, monkeypatch):
    client, app, services, user = auth_client
    # bind the user's ADO identity
    user.ado_descriptor = "desc-1"
    session.commit()
    monkeypatch.setattr(app.state, "ado_client", _TicketedAdo([
        {"id": 7, "fields": {"System.Title": "Bug dedupe", "System.State": "Active",
                             "System.WorkItemType": "Bug"}},
    ]))
    r = client.get("/hydration/my-tickets")
    assert r.status_code == 200
    assert r.json()["tickets"] == [{"id": 7, "title": "Bug dedupe", "state": "Active", "type": "Bug"}]


def test_my_tickets_422_when_unbound(auth_client, session, make_user):
    """Unbound users get 422 — the UI must distinguish 'bind your account' from
    'no active tickets' (B2)."""
    client, _, _, user = auth_client
    assert user.ado_descriptor is None
    r = client.get("/hydration/my-tickets")
    assert r.status_code == 422
    assert "not bound" in r.json()["detail"]


def test_blast_radius_endpoint(auth_client):
    client, _, _, _ = auth_client
    r = client.get("/hydration/blast-radius", params={"repo": "ServerApp"})
    assert r.status_code == 200
    assert r.json()["repo"] == "ServerApp"
    assert isinstance(r.json()["blast_radius"], list)


def test_hydrate_title_endpoint_uses_work_item(auth_client, monkeypatch):
    client, app, _, _ = auth_client
    monkeypatch.setattr(app.state, "ado_client", _TicketedAdo([], work_items={
        42: {"id": 42, "fields": {"System.Title": "Real title"}},
    }))
    r = client.get("/hydration/title", params={"work_item_id": 42, "task": "fallback"})
    assert r.status_code == 200
    assert r.json()["title"] == "Real title"
    assert r.json()["work_item_id"] == 42


def test_hydrate_title_endpoint_falls_back_to_task(auth_client):
    client, _, _, _ = auth_client
    r = client.get("/hydration/title", params={"task": "just a task"})
    assert r.status_code == 200
    assert r.json()["title"] == "just a task"
    assert r.json()["work_item_id"] is None


def test_prewarm_endpoint(auth_client, session):
    client, app, _, _ = auth_client
    repo = Repo(name="ServerApp", integration_branch="main")
    session.add(repo); session.commit()
    r = client.post("/hydration/prewarm", json={"repos": [{"name": "ServerApp"}, {"name": "Ghost"}]})
    assert r.status_code == 200
    prewarmed = {p["repo"]: p for p in r.json()["prewarmed"]}
    # The stub records intent only — it must never claim warmth (B3).
    assert prewarmed["ServerApp"]["status"] == "recorded"
    assert prewarmed["Ghost"]["status"] == "repo_not_registered"


def test_prewarm_status_endpoint_reports_disabled(auth_client):
    client, _, _, _ = auth_client
    r = client.get("/hydration/prewarm-status")
    assert r.status_code == 200
    body = r.json()
    assert body["enabled"] is False
    assert body["pool_size"] == 0
    assert "not implemented" in body["note"]


def test_hydration_requires_auth(app_client):
    client, _, _ = app_client
    assert client.get("/hydration/my-tickets").status_code == 401
    assert client.get("/hydration/blast-radius", params={"repo": "x"}).status_code == 401
    assert client.post("/hydration/prewarm", json={"repos": []}).status_code == 401


class _TicketedAdo:
    def __init__(self, tickets=None, work_items=None):
        self._tickets = tickets or []
        self._work_items = work_items or {}

    async def my_active_tickets(self, descriptor):
        return self._tickets

    async def get_work_item(self, work_item_id):
        return self._work_items.get(work_item_id, {"id": work_item_id,
                                                   "fields": {"System.Title": f"item {work_item_id}"}})
