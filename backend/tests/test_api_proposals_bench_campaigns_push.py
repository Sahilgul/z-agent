"""G-07: four mutating route modules (proposals, bench, campaigns, push)
had zero tests. Auth-boundary + happy-path coverage for each.

Note: admin_client and auth_client share the same underlying TestClient
(app_client) and each sets its own auth cookie, so a single test must NOT
request both fixtures (the second overwrites the first's cookie). Split
admin-vs-member boundary checks into separate tests."""

from __future__ import annotations

from app.db.models.proposal import Proposal
from app.db.models.repo import Repo


# --------------------------------------------------------------- proposals
def test_proposals_inbox_ranks_by_impact_times_confidence(auth_client, session):
    client, _, _, _ = auth_client
    session.add_all([
        Proposal(source="janitor", title="low/med", body="b", evidence=["a.py:1"],
                  impact="low", confidence="medium", status="proposed"),
        Proposal(source="perfector", title="high/high", body="b", evidence=["a.py:2"],
                  impact="high", confidence="high", status="proposed"),
    ])
    session.commit()
    r = client.get("/proposals")
    assert r.status_code == 200
    items = r.json()["items"]
    assert items[0]["title"] == "high/high"  # rank_score 9 > 4
    assert items[0]["rank_score"] >= items[1]["rank_score"]


def test_proposals_dismiss_happy_then_404(auth_client, session):
    client, _, _, _ = auth_client
    session.add(Proposal(id=10, source="janitor", title="t", body="b", evidence=["a.py:1"],
                        impact="medium", confidence="medium", status="proposed"))
    session.commit()
    r = client.post("/proposals/10/dismiss", json={"reason": "not now"})
    assert r.status_code == 200
    assert r.json()["status"] == "dismissed"
    # Already decided -> 422; missing -> 404.
    assert client.post("/proposals/10/dismiss", json={"reason": ""}).status_code == 422
    assert client.post("/proposals/9999/dismiss", json={"reason": ""}).status_code == 404


def test_proposals_accept_happy_then_404(auth_client, session):
    client, _, _, _ = auth_client
    session.add(Proposal(id=20, source="perfector", title="accept me", body="b",
                        evidence=["a.py:1"], impact="high", confidence="high",
                        status="proposed", repo="ServerApp"))
    session.commit()
    r = client.post("/proposals/20/accept")
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == "accepted"
    assert "run_id" in body
    assert client.post("/proposals/9999/accept").status_code == 404


def test_proposals_requires_auth(app_client):
    client, _, _ = app_client
    # No auth cookie -> 401.
    assert client.get("/proposals").status_code == 401


# --------------------------------------------------------------- bench
def test_bench_admin_creates_case_and_lists(admin_client, session):
    admin, _, _, _ = admin_client
    r = admin.post("/bench/cases", json={
        "repo": "ServerApp", "title": "case-1", "task_text": "do it",
        "base_commit": "abc", "fail_to_pass": ["test_a"],
    })
    assert r.status_code == 201
    assert r.json()["fail_to_pass"] == ["test_a"]
    r2 = admin.get("/bench/cases")
    assert r2.status_code == 200
    assert any(c["title"] == "case-1" for c in r2.json()["items"])


def test_bench_member_cannot_create_case(auth_client):
    member, _, _, _ = auth_client
    r = member.post("/bench/cases", json={
        "repo": "ServerApp", "title": "t", "task_text": "do it",
        "base_commit": "abc", "fail_to_pass": ["test_a"],
    })
    assert r.status_code == 403


def test_bench_report_is_team_visible(auth_client):
    client, _, _, _ = auth_client
    r = client.get("/bench/report")
    assert r.status_code == 200
    assert "resolution_rate" in r.json()


def test_bench_record_result_404_for_missing_eval(admin_client):
    admin, _, _, _ = admin_client
    r = admin.post("/bench/evals/9999/result", json={"outcomes": {"test_a": True}})
    assert r.status_code == 404


def test_bench_member_cannot_record_result(auth_client):
    member, _, _, _ = auth_client
    r = member.post("/bench/evals/1/result", json={"outcomes": {"test_a": True}})
    assert r.status_code == 403


def test_bench_run_case_404_for_missing_case(admin_client):
    admin, _, _, _ = admin_client
    r = admin.post("/bench/cases/9999/run")
    assert r.status_code == 404
    assert "not found" in r.json()["detail"]


# --------------------------------------------------------------- campaigns
def test_campaigns_launch_and_deliveries(admin_client, session):
    admin, _, _, _ = admin_client
    session.add(Repo(name="ServerApp", integration_branch="main", status="ready"))
    session.commit()
    r = admin.post("/campaigns", json={"task": "migrate logging across the fleet",
                                       "repos": ["ServerApp"], "title": "log-migrate"})
    assert r.status_code == 201, r.text
    body = r.json()
    assert "delivery_id" in body
    # Fleet rollup is team-visible (route is /deliveries, no /campaigns prefix).
    r2 = admin.get("/deliveries")
    assert r2.status_code == 200, r2.text
    assert any(d["id"] == body["delivery_id"] for d in r2.json()["items"])


def test_campaigns_launch_422_for_non_ready_repo(admin_client):
    admin, _, _, _ = admin_client
    r = admin.post("/campaigns", json={"task": "do a thing", "repos": ["NotReady"]})
    assert r.status_code == 422


def test_campaigns_launch_distinguishes_not_found_vs_not_ready(admin_client, session):
    """M-43: a typo (repo that doesn't exist) and a repo that exists but
    isn't USABLE must produce DISTINCT 422 messages — the old code lumped
    both under 'repos not ready', so the human couldn't tell whether to
    register the repo, mark it ready, or fix their casing."""
    admin, _, _, _ = admin_client
    session.add(Repo(name="ServerApp", integration_branch="main", status="provisioning"))
    session.commit()
    # not-found: a repo that doesn't exist at all.
    r = admin.post("/campaigns", json={"task": "do a thing", "repos": ["GhostApp"]})
    assert r.status_code == 422
    assert "not found" in r.json()["detail"]
    assert "GhostApp" in r.json()["detail"]
    # not-ready: a repo that exists but isn't USABLE.
    r = admin.post("/campaigns", json={"task": "do a thing", "repos": ["ServerApp"]})
    assert r.status_code == 422
    assert "not ready" in r.json()["detail"]
    assert "ServerApp" in r.json()["detail"]
    # wrong-case: a repo that exists under a different case.
    r = admin.post("/campaigns", json={"task": "do a thing", "repos": ["serverapp"]})
    assert r.status_code == 422
    assert "wrong case" in r.json()["detail"]


# --------------------------------------------------------------- push
def test_push_vapid_key(auth_client):
    client, _, _, _ = auth_client
    r = client.get("/push/vapid-public-key")
    assert r.status_code == 200
    assert "public_key" in r.json()
    assert "enabled" in r.json()


def test_push_subscribe_and_unsubscribe(auth_client):
    client, _, _, _ = auth_client
    endpoint = "https://updates.push.services.example/s/abc"
    r = client.post("/push/subscriptions",
                     json={"endpoint": endpoint, "keys": {"p256dh": "k", "auth": "a"}})
    assert r.status_code == 201
    assert r.json()["endpoint"] == endpoint
    # Idempotent re-subscribe updates keys, same endpoint.
    assert client.post("/push/subscriptions",
                       json={"endpoint": endpoint, "keys": {"p256dh": "k2", "auth": "a2"}}).status_code == 201
    # Unsubscribe -> 204 (DELETE with body via request()).
    r3 = client.request("DELETE", "/push/subscriptions",
                        json={"endpoint": endpoint, "keys": {}})
    assert r3.status_code == 204


def test_push_subscribe_422_for_non_https(auth_client):
    client, _, _, _ = auth_client
    r = client.post("/push/subscriptions",
                    json={"endpoint": "http://insecure.example/s", "keys": {}})
    assert r.status_code == 422


def test_autonomy_cap(auth_client):
    client, _, _, _ = auth_client
    r = client.get("/me/autonomy-cap")
    assert r.status_code == 200
    assert r.json()["cap"] in ("supervised", "gated", "autonomous")
