import json
from datetime import datetime, timedelta, timezone

import pytest

from app.db.models.approval import Approval
from app.db.models.event import Event
from app.db.models.knowledge import KnowledgeItem
from app.db.models.thread import Thread
from app.db.models.mode import Mode
from app.db.models.repo import Repo
from app.db.models.run import Plan, Run
from app.db.models.user import User


# --------------------------------------------------------------- helpers
class _FakeAdoIdentity:
    def __init__(self, descriptor="desc-1", display_name="n", mail="m@x.com"):
        self.descriptor = descriptor
        self.display_name = display_name
        self.mail = mail


class _FakeAdoClient:
    def __init__(self, identity=None, raise_exc=None):
        self._identity = identity
        self._raise = raise_exc

    async def resolve_identity(self, email):
        if self._raise:
            raise self._raise
        return self._identity


def _patch_ado(monkeypatch, identity=None, raise_exc=None):
    from app.services import team as team_mod
    fake = _FakeAdoClient(identity=identity or _FakeAdoIdentity(), raise_exc=raise_exc)
    monkeypatch.setattr(team_mod, "AdoClient", lambda *a, **k: fake)
    return fake


# --------------------------------------------------------------- health
def test_health(app_client):
    client, app, _ = app_client
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_health_no_auth_required(app_client):
    client, _, _ = app_client
    assert client.get("/health").status_code == 200


# --------------------------------------------------------------- auth
def test_login_success(auth_client):
    client, _, _, user = auth_client
    r = client.post("/auth/login", json={"username": "alice", "pin": "1234"})
    assert r.status_code == 200
    body = r.json()
    assert body["username"] == "alice"
    assert body["role"] == "member"


def test_login_unknown_user(app_client):
    client, _, _ = app_client
    r = client.post("/auth/login", json={"username": "ghost", "pin": "0000"})
    assert r.status_code == 401


def test_login_wrong_pin(auth_client):
    client, _, _, _ = auth_client
    r = client.post("/auth/login", json={"username": "alice", "pin": "9999"})
    assert r.status_code == 401


def test_login_inactive(app_client, session, make_user):
    client, _, _ = app_client
    make_user("dormant", role="member", status="pending", pin="1234")
    r = client.post("/auth/login", json={"username": "dormant", "pin": "1234"})
    assert r.status_code == 401


def test_login_locked_out(auth_client, session, make_user):
    client, _, _, user = auth_client
    user.locked_until = datetime.now(timezone.utc) + timedelta(hours=1)
    session.commit()
    r = client.post("/auth/login", json={"username": "alice", "pin": "1234"})
    assert r.status_code == 429


def test_me_requires_auth(app_client):
    client, _, _ = app_client
    assert client.get("/auth/me").status_code == 401


def test_me_returns_user(auth_client):
    client, _, _, user = auth_client
    r = client.get("/auth/me")
    assert r.status_code == 200
    assert r.json()["username"] == "alice"


def test_logout(app_client):
    client, _, _ = app_client
    r = client.post("/auth/logout")
    assert r.status_code == 200
    assert r.json()["ok"] is True


# --------------------------------------------------------------- auth first-login
def test_first_login_success(app_client, session, make_user):
    client, _, _ = app_client
    from app.services import team
    u = make_user("newbie", role="member", status="pending")
    code = team.regenerate_code(u.id)
    r = client.post("/auth/first-login", json={
        "username": "newbie", "code": code, "pin": "1234", "display_name": "New Person",
    })
    assert r.status_code == 200
    body = r.json()
    assert body["username"] == "newbie"
    assert body["first_run"] is True
    assert "zagent_token" in r.cookies
    session.expire_all()
    fresh = session.get(User, u.id)
    assert fresh.status == "active"
    assert fresh.pin_hash is not None
    assert fresh.display_name == "New Person"


def test_first_login_invalid_pin_format(app_client, make_user):
    client, _, _ = app_client
    make_user("short", role="member", status="pending")
    r = client.post("/auth/first-login", json={
        "username": "short", "code": "00000000", "pin": "12",
    })
    assert r.status_code == 422


def test_first_login_bad_code(app_client, make_user):
    client, _, _ = app_client
    make_user("newbie2", role="member", status="pending")
    r = client.post("/auth/first-login", json={
        "username": "newbie2", "code": "99999999", "pin": "1234",
    })
    assert r.status_code == 401


def test_first_login_unknown_user(app_client):
    client, _, _ = app_client
    r = client.post("/auth/first-login", json={
        "username": "ghost", "code": "00000000", "pin": "1234",
    })
    assert r.status_code == 401


def test_first_login_without_display_name(app_client, session, make_user):
    client, _, _ = app_client
    from app.services import team
    u = make_user("nodisp", role="member", status="pending")
    code = team.regenerate_code(u.id)
    r = client.post("/auth/first-login", json={
        "username": "nodisp", "code": code, "pin": "1234",
    })
    assert r.status_code == 200
    session.expire_all()
    fresh = session.get(User, u.id)
    assert fresh.status == "active"
    assert fresh.pin_hash is not None
    # No display_name in the body → existing display_name is left untouched.
    assert fresh.display_name == "Alice"


# --------------------------------------------------------------- team (admin)
def test_list_users_admin_only(auth_client):
    client, _, _, _ = auth_client
    assert client.get("/team/users").status_code == 403


def test_list_users(admin_client, session, make_user):
    client, _, _, _ = admin_client
    make_user("bob", role="member", status="active")
    r = client.get("/team/users")
    assert r.status_code == 200
    names = [u["username"] for u in r.json()]
    assert "sahil" in names and "bob" in names


def test_create_user_admin(monkeypatch, admin_client):
    client, _, _, _ = admin_client
    _patch_ado(monkeypatch)
    r = client.post("/team/users", json={"username": "newbie", "display_name": "New", "ado_email": "n@x.com"})
    assert r.status_code == 200
    body = r.json()
    assert body["username"] == "newbie"
    assert body["setup_code"]
    assert body["ado_bound"] is True


def test_create_user_duplicate(monkeypatch, admin_client, session, make_user):
    client, _, _, _ = admin_client
    make_user("dup", role="member", status="active")
    _patch_ado(monkeypatch)
    r = client.post("/team/users", json={"username": "dup", "display_name": "", "ado_email": ""})
    assert r.status_code == 422


def test_create_user_identity_error(monkeypatch, admin_client):
    client, _, _, _ = admin_client
    from app.ado.client import IdentityResolutionError
    _patch_ado(monkeypatch, raise_exc=IdentityResolutionError("no identity"))
    r = client.post("/team/users", json={"username": "x", "display_name": "", "ado_email": "x@x.com"})
    assert r.status_code == 422


def test_regenerate_code(admin_client, session, make_user):
    client, _, _, _ = admin_client
    u = make_user("regen", role="member", status="pending")
    r = client.post(f"/team/users/{u.id}/regenerate-code")
    assert r.status_code == 200
    assert r.json()["setup_code"]


def test_regenerate_code_unknown(admin_client):
    client, _, _, _ = admin_client
    r = client.post("/team/users/999999/regenerate-code")
    assert r.status_code == 404


def test_deactivate_user(admin_client, session, make_user):
    client, _, _, _ = admin_client
    u = make_user("deact", role="member", status="active")
    r = client.post(f"/team/users/{u.id}/deactivate")
    assert r.status_code == 200
    session.expire_all()
    assert session.get(User, u.id).status == "deactivated"


def test_team_stats(admin_client, session):
    client, _, _, admin = admin_client
    session.add(Run(id="r1", created_by=admin.id, mode="ask", stage="completed", cost_usd=1.5))
    session.add(Run(id="r2", created_by=admin.id, mode="ask", stage="investigating", cost_usd=2.0))
    session.commit()
    r = client.get("/team/stats")
    assert r.status_code == 200
    body = r.json()
    assert body["total_runs"] >= 2
    assert body["total_cost_usd"] >= 3.5


# --------------------------------------------------------------- modes
def test_list_modes_empty(auth_client):
    client, _, _, _ = auth_client
    r = client.get("/modes")
    assert r.status_code == 200
    assert r.json() == []


def test_list_modes(auth_client, session):
    client, _, _, _ = auth_client
    session.add(Mode(name="ask", autonomy_default="supervised", enabled=True))
    session.add(Mode(name="plan", autonomy_default="supervised", enabled=False))
    session.commit()
    r = client.get("/modes")
    assert r.status_code == 200
    names = [m["name"] for m in r.json()]
    assert "ask" in names
    assert "plan" not in names


def test_modes_require_auth(app_client):
    client, _, _ = app_client
    assert client.get("/modes").status_code == 401


# --------------------------------------------------------------- repos
def test_list_repos(auth_client, session):
    client, _, _, _ = auth_client
    session.add(Repo(name="ServerApp", integration_branch="main"))
    session.add(Repo(name="Archived", integration_branch="main", status="archived"))
    session.commit()
    r = client.get("/repos")
    assert r.status_code == 200
    names = [x["name"] for x in r.json()]
    assert "ServerApp" in names
    assert "Archived" not in names


def test_remote_branches(auth_client, monkeypatch):
    client, _, _, _ = auth_client
    import app.api.repos as route
    monkeypatch.setattr(route, "validate_remote", lambda url, pat: ["main", "dev"])
    r = client.get("/repos/remote-branches", params={"name": "ServerApp"})
    assert r.status_code == 200
    assert r.json()["branches"] == ["main", "dev"]


def test_remote_branches_onboarding_error(auth_client, monkeypatch):
    client, _, _, _ = auth_client
    import app.api.repos as route
    from app.services.repos import OnboardingError

    def boom(url, pat):
        raise OnboardingError("bad remote")
    monkeypatch.setattr(route, "validate_remote", boom)
    r = client.get("/repos/remote-branches", params={"name": "Bad"})
    assert r.status_code == 422


def test_add_repo(auth_client, monkeypatch):
    client, _, _, _ = auth_client
    import app.api.repos as route
    monkeypatch.setattr(route, "register_repo",
                        lambda name, url, branch, added_by=None: Repo(id=1, name=name, integration_branch=branch))

    async def fake_onboard(repo_id, relay):
        return None
    monkeypatch.setattr(route, "onboard", fake_onboard)
    r = client.post("/repos", json={"name": "ServerApp", "integration_branch": "main"})
    assert r.status_code == 200
    assert r.json()["name"] == "ServerApp"


def test_add_repo_rejects_duplicate(auth_client, session):
    client, _, _, _ = auth_client
    session.add(Repo(name="ServerApp", integration_branch="main", status="ready"))
    session.commit()
    r = client.post("/repos", json={"name": "ServerApp", "integration_branch": "pg-main"})
    assert r.status_code == 409
    assert "already registered" in r.json()["detail"]


def test_add_repo_revives_archived(auth_client, session, monkeypatch):
    client, _, _, _ = auth_client
    session.add(Repo(name="ServerApp", integration_branch="main", status="archived"))
    session.commit()
    import app.api.repos as route
    monkeypatch.setattr(route, "register_repo",
                        lambda name, url, branch, added_by=None: Repo(id=1, name=name, integration_branch=branch))

    async def fake_onboard(repo_id, relay):
        return None
    monkeypatch.setattr(route, "onboard", fake_onboard)
    assert client.post("/repos", json={"name": "ServerApp", "integration_branch": "main"}).status_code == 200


def test_edit_repo(auth_client, session):
    client, _, _, _ = auth_client
    repo = Repo(name="ServerApp", integration_branch="main")
    session.add(repo); session.commit()
    r = client.patch(f"/repos/{repo.id}",
                     json={"integration_branch": "pg-main", "audit_note": "cutover"})
    assert r.status_code == 200
    session.expire_all()
    assert session.get(Repo, repo.id).integration_branch == "pg-main"


def test_edit_repo_not_found(auth_client):
    client, _, _, _ = auth_client
    r = client.patch("/repos/999999", json={"integration_branch": "x"})
    assert r.status_code == 404


def test_archive_repo(auth_client, session, monkeypatch):
    client, _, _, _ = auth_client
    repo = Repo(name="ServerApp", integration_branch="main")
    session.add(repo); session.commit()
    import app.api.repos as route
    monkeypatch.setattr(route, "archive_repo", lambda rid: None)
    r = client.post(f"/repos/{repo.id}/archive")
    assert r.status_code == 200


def test_repos_require_auth(app_client):
    client, _, _ = app_client
    assert client.get("/repos").status_code == 401


# --------------------------------------------------------------- runs
def test_create_run(auth_client):
    client, _, services, _ = auth_client
    r = client.post("/runs", json={"mode": "ask", "task": "summarize scribe"})
    assert r.status_code == 200
    body = r.json()
    assert body["mode"] == "ask"
    assert body["title"] == "summarize scribe"
    assert services["run_manager"].created


def test_create_run_unknown_mode(auth_client, monkeypatch):
    client, _, services, _ = auth_client

    async def boom(*a, **k):
        raise ValueError("unknown or disabled mode 'ghost'")
    monkeypatch.setattr(services["run_manager"], "create_run", boom)
    r = client.post("/runs", json={"mode": "ghost", "task": "x"})
    assert r.status_code == 422


def test_create_run_passes_fanout_through(auth_client):
    """User-requested fan-out: the count rides POST /runs into the swarm
    blueprint's hydrate (the Lead still authors the slices)."""
    client, _, services, _ = auth_client
    r = client.post("/runs", json={"mode": "agent-rnd", "task": "map billing", "fanout": 5})
    assert r.status_code == 200
    assert services["run_manager"].created[0]["fanout"] == 5


def test_list_my_runs(auth_client, session, make_user):
    client, _, _, user = auth_client
    other = make_user("other", role="member", status="active")
    session.add(Run(id="r-mine", created_by=user.id, mode="ask", stage="completed", title="mine"))
    session.add(Run(id="r-other", created_by=other.id, mode="ask", stage="completed", title="other"))
    session.commit()
    r = client.get("/runs")
    assert r.status_code == 200
    titles = [x["title"] for x in r.json()]
    assert "mine" in titles
    assert "other" not in titles


def test_list_my_runs_with_filters(auth_client, session, make_user):
    client, _, _, user = auth_client
    session.add(Run(id="r1", created_by=user.id, mode="ask", stage="completed", title="alpha", repo="ServerApp"))
    session.add(Run(id="r2", created_by=user.id, mode="ask", stage="investigating", title="beta", repo="ClientApp"))
    session.commit()
    assert [x["title"] for x in client.get("/runs", params={"repo": "ServerApp"}).json()] == ["alpha"]
    assert [x["title"] for x in client.get("/runs", params={"stage": "investigating"}).json()] == ["beta"]
    assert [x["title"] for x in client.get("/runs", params={"q": "alph"}).json()] == ["alpha"]


def test_get_run(auth_client, session, make_user):
    client, _, _, user = auth_client
    session.add(Run(id="r1", created_by=user.id, mode="ask", stage="completed"))
    session.commit()
    r = client.get("/runs/r1")
    assert r.status_code == 200
    assert r.json()["id"] == "r1"


def test_get_run_not_found(auth_client):
    client, _, _, _ = auth_client
    r = client.get("/runs/ghost")
    assert r.status_code == 404


def test_get_run_other_users_hidden(auth_client, session, make_user):
    client, _, _, _ = auth_client
    other = make_user("other", role="member", status="active")
    session.add(Run(id="r1", created_by=other.id, mode="ask", stage="completed"))
    session.commit()
    assert client.get("/runs/r1").status_code == 404


def test_run_events(auth_client, session, make_user):
    client, _, _, user = auth_client
    run = Run(id="r1", created_by=user.id, mode="ask", stage="completed")
    thread = Thread(id="l1", run_id="r1", persona="researcher", status="completed")
    session.add_all([run, thread]); session.commit()
    session.add(Event(run_id="r1", thread_id="l1", seq=0, type="message", title="t", payload={}))
    session.commit()
    r = client.get("/runs/r1/events")
    assert r.status_code == 200
    assert len(r.json()) == 1


def test_run_threads(auth_client, session, make_user):
    client, _, _, user = auth_client
    run = Run(id="r1", created_by=user.id, mode="ask", stage="completed")
    thread = Thread(id="l1", run_id="r1", persona="researcher", status="completed")
    session.add_all([run, thread]); session.commit()
    r = client.get("/runs/r1/threads")
    assert r.status_code == 200
    assert r.json()[0]["persona"] == "researcher"


def test_post_intent_stop(auth_client, session, make_user):
    client, _, services, user = auth_client
    session.add(Run(id="r1", created_by=user.id, mode="ask", stage="investigating")); session.commit()
    r = client.post("/runs/r1/intent", json={"intent": "stop_run", "source": "button"})
    assert r.status_code == 200
    assert r.json()["intent"] == "stop_run"
    assert services["run_manager"].stopped == ["r1"]


def test_post_intent_abandon_confirm(auth_client, session, make_user):
    client, _, services, user = auth_client
    session.add(Run(id="r1", created_by=user.id, mode="ask", stage="investigating")); session.commit()
    r = client.post("/runs/r1/intent", json={"intent": "abandon_run", "source": "button", "confirmed": True})
    assert r.status_code == 200
    assert services["run_manager"].abandoned == ["r1"]


def test_post_intent_abandon_needs_confirm(auth_client, session, make_user):
    client, _, _, user = auth_client
    session.add(Run(id="r1", created_by=user.id, mode="ask", stage="investigating")); session.commit()
    r = client.post("/runs/r1/intent", json={"intent": "abandon_run", "source": "button", "confirmed": False})
    assert r.status_code == 200
    assert r.json()["status"] == "confirm"


def test_post_intent_text_message(auth_client, session, make_user):
    client, _, services, user = auth_client
    run = Run(id="r1", created_by=user.id, mode="ask", stage="investigating")
    thread = Thread(id="l1", run_id="r1", persona="researcher", status="running")
    session.add_all([run, thread]); session.commit()
    r = client.post("/runs/r1/intent", json={"text": "hurry up", "source": "text"})
    assert r.status_code == 200
    assert r.json()["intent"] == "send_message"
    assert services["run_manager"].nudged


def test_post_intent_run_not_found(auth_client):
    client, _, _, _ = auth_client
    r = client.post("/runs/ghost/intent", json={"intent": "stop_run", "source": "button"})
    assert r.status_code == 404


def test_post_intent_switch_mode(auth_client, session, make_user):
    client, _, services, user = auth_client
    run = Run(id="r1", created_by=user.id, mode="ask", stage="investigating")
    session.add(run); session.commit()
    r = client.post("/runs/r1/intent", json={
        "intent": "switch_mode", "source": "chip",
        "payload": {"mode": "plan"},
    })
    assert r.status_code == 200
    assert r.json()["intent"] == "switch_mode"
    assert r.json()["mode"] == "plan"
    assert services["run_manager"].switched_modes == [("r1", "plan")]


def test_post_intent_switch_mode_422_without_mode(auth_client, session, make_user):
    client, _, _, user = auth_client
    run = Run(id="r1", created_by=user.id, mode="ask", stage="investigating")
    session.add(run); session.commit()
    r = client.post("/runs/r1/intent", json={
        "intent": "switch_mode", "source": "chip", "payload": {},
    })
    assert r.status_code == 422


def test_runs_require_auth(app_client):
    client, _, _ = app_client
    assert client.get("/runs").status_code == 401


# --------------------------------------------------------------- threads
def test_thread_nudge(auth_client, session, make_user):
    client, _, services, user = auth_client
    run = Run(id="r1", created_by=user.id, mode="ask", stage="investigating")
    thread = Thread(id="l1", run_id="r1", persona="researcher", status="running")
    session.add_all([run, thread]); session.commit()
    r = client.post("/threads/l1/nudge", json={"run_id": "r1", "text": "go"})
    assert r.status_code == 200
    assert services["run_manager"].nudged[-1] == ("r1", "l1", "go")


def test_thread_stop(auth_client, session, make_user):
    client, _, services, user = auth_client
    run = Run(id="r1", created_by=user.id, mode="ask", stage="investigating")
    thread = Thread(id="l1", run_id="r1", persona="researcher", status="running")
    session.add_all([run, thread]); session.commit()
    r = client.post("/threads/l1/stop", json={"run_id": "r1"})
    assert r.status_code == 200
    assert any(msg.get("type") == "interrupt" for _, msg in services["control"].calls)


def test_thread_pin(auth_client, session, make_user):
    client, _, services, user = auth_client
    run = Run(id="r1", created_by=user.id, mode="ask", stage="investigating")
    thread = Thread(id="l1", run_id="r1", persona="researcher", status="running")
    session.add_all([run, thread]); session.commit()
    r = client.post("/threads/l1/pin", json={"run_id": "r1"})
    assert r.status_code == 200
    assert services["relay"].published


def test_thread_action_run_not_found(auth_client, session, make_user):
    client, _, _, user = auth_client
    other = make_user("other", role="member", status="active")
    session.add(Run(id="r1", created_by=other.id, mode="ask", stage="investigating"))
    session.commit()
    r = client.post("/threads/l1/nudge", json={"run_id": "r1", "text": "x"})
    assert r.status_code == 404


def test_thread_nudge_idor_rejects_other_run_thread(auth_client, session, make_user):
    """C-08: /threads/{id}/nudge must not act on a thread that belongs to
    another run. The caller pairs their OWN run_id with another run's
    thread_id — the thread->run ownership guard returns 404, never
    reaching run_manager.nudge_thread."""
    client, _, services, user = auth_client
    other = make_user("other", role="member", status="active")
    own_run = Run(id="r1", created_by=user.id, mode="ask", stage="investigating")
    other_run = Run(id="r2", created_by=other.id, mode="ask", stage="investigating")
    other_thread = Thread(id="l2", run_id="r2", persona="researcher", status="running")
    session.add_all([own_run, other_run, other_thread]); session.commit()
    r = client.post("/threads/l2/nudge", json={"run_id": "r1", "text": "go"})
    assert r.status_code == 404
    assert services["run_manager"].nudged == []


def test_thread_stop_idor_rejects_other_run_thread(auth_client, session, make_user):
    """C-09: /threads/{id}/stop must not interrupt a thread that belongs to
    another run — same ownership guard as nudge."""
    client, _, services, user = auth_client
    other = make_user("other", role="member", status="active")
    own_run = Run(id="r1", created_by=user.id, mode="ask", stage="investigating")
    other_run = Run(id="r2", created_by=other.id, mode="ask", stage="investigating")
    other_thread = Thread(id="l2", run_id="r2", persona="researcher", status="running")
    session.add_all([own_run, other_run, other_thread]); session.commit()
    r = client.post("/threads/l2/stop", json={"run_id": "r1"})
    assert r.status_code == 404
    assert not any(msg.get("type") == "interrupt" for _, msg in services["control"].calls)


def test_post_intent_send_message_idor_rejects_other_run_thread(auth_client, session, make_user):
    """C-11: a caller-supplied thread_id on SEND_MESSAGE must belong to the
    run, otherwise _persist_user_message would corrupt another thread's
    next_seq and nudge_thread would nudge another user's thread."""
    client, _, services, user = auth_client
    other = make_user("other", role="member", status="active")
    own_run = Run(id="r1", created_by=user.id, mode="ask", stage="investigating")
    other_run = Run(id="r2", created_by=other.id, mode="ask", stage="investigating")
    other_thread = Thread(id="l2", run_id="r2", persona="researcher", status="running")
    session.add_all([own_run, other_run, other_thread]); session.commit()
    r = client.post("/runs/r1/intent",
                    json={"intent": "send_message", "source": "button",
                          "text": "hi", "thread_id": "l2"})
    assert r.status_code == 404
    assert services["run_manager"].nudged == []


def test_threads_require_auth(app_client):
    client, _, _ = app_client
    assert client.post("/threads/l1/nudge", json={"run_id": "r1", "text": "x"}).status_code == 401


# --------------------------------------------------------------- approvals
def test_pending_approvals_empty(auth_client):
    client, _, _, _ = auth_client
    assert client.get("/approvals").status_code == 200
    assert client.get("/approvals").json() == []


def test_pending_approvals(auth_client, session, make_user):
    client, _, _, user = auth_client
    run = Run(id="r1", created_by=user.id, mode="ask", stage="investigating")
    thread = Thread(id="l1", run_id="r1", persona="researcher", status="running")
    ap = Approval(id="a1", run_id="r1", thread_id="l1", kind="bash", payload={})
    session.add_all([run, thread, ap]); session.commit()
    r = client.get("/approvals")
    assert r.status_code == 200
    assert r.json()[0]["id"] == "a1"


def test_pending_approvals_filtered_by_run(auth_client, session, make_user):
    """The console docks approvals inside the open session, so it asks for one
    run — other runs' pending cards must not come back."""
    client, _, _, user = auth_client
    for rid, aid in (("r1", "a1"), ("r2", "a2")):
        session.add_all([
            Run(id=rid, created_by=user.id, mode="ask", stage="investigating"),
            Thread(id=f"l-{rid}", run_id=rid, persona="researcher", status="running"),
            Approval(id=aid, run_id=rid, thread_id=f"l-{rid}", kind="bash", payload={}),
        ])
    session.commit()
    assert {a["id"] for a in client.get("/approvals").json()} == {"a1", "a2"}
    scoped = client.get("/approvals", params={"run_id": "r2"}).json()
    assert [a["id"] for a in scoped] == ["a2"]


def test_pending_approvals_hides_expired_cards(auth_client, session, make_user):
    """Past expires_at the worker has already denied the tool — offering the
    buttons would let the user 'approve' something nobody is waiting on."""
    from datetime import timedelta
    client, _, _, user = auth_client
    session.add(Run(id="r1", created_by=user.id, mode="ask", stage="investigating"))
    now = datetime.now(timezone.utc)
    session.add_all([
        Approval(id="a-dead", run_id="r1", thread_id="l1", kind="bash", payload={},
                 expires_at=now - timedelta(minutes=1)),
        Approval(id="a-live", run_id="r1", thread_id="l1", kind="bash", payload={},
                 expires_at=now + timedelta(minutes=10)),
    ])
    session.commit()
    body = client.get("/approvals").json()
    assert [a["id"] for a in body] == ["a-live"]
    assert body[0]["expires_at"] is not None


def test_decide_approval(auth_client, session, make_user):
    client, _, services, user = auth_client
    run = Run(id="r1", created_by=user.id, mode="ask", stage="investigating")
    thread = Thread(id="l1", run_id="r1", persona="researcher", status="running")
    ap = Approval(id="a1", run_id="r1", thread_id="l1", kind="bash", payload={})
    session.add_all([run, thread, ap]); session.commit()
    r = client.post("/approvals/a1/decide", json={"decision": "approved", "reason": "ok"})
    assert r.status_code == 200
    assert services["approval_service"].decisions[-1] == ("a1", "approved", user.id, "ok")


def test_decide_approval_not_found(auth_client):
    client, _, _, _ = auth_client
    r = client.post("/approvals/ghost/decide", json={"decision": "deny"})
    assert r.status_code == 404


def test_decide_approval_other_user_hidden(auth_client, session, make_user):
    client, _, _, _ = auth_client
    other = make_user("other", role="member", status="active")
    run = Run(id="r1", created_by=other.id, mode="ask", stage="investigating")
    thread = Thread(id="l1", run_id="r1", persona="researcher", status="running")
    ap = Approval(id="a1", run_id="r1", thread_id="l1", kind="bash", payload={})
    session.add_all([run, thread, ap]); session.commit()
    r = client.post("/approvals/a1/decide", json={"decision": "deny"})
    assert r.status_code == 404


def test_decide_approval_value_error(auth_client, session, make_user, monkeypatch):
    client, _, services, user = auth_client
    run = Run(id="r1", created_by=user.id, mode="ask", stage="investigating")
    thread = Thread(id="l1", run_id="r1", persona="researcher", status="running")
    ap = Approval(id="a1", run_id="r1", thread_id="l1", kind="bash", payload={})
    session.add_all([run, thread, ap]); session.commit()

    async def boom(*a, **k):
        raise ValueError("already decided")
    monkeypatch.setattr(services["approval_service"], "decide", boom)
    r = client.post("/approvals/a1/decide", json={"decision": "approved"})
    assert r.status_code == 409


def test_approvals_require_auth(app_client):
    client, _, _ = app_client
    assert client.get("/approvals").status_code == 401


# --------------------------------------------------------------- sessions
def test_session_replay(auth_client, session, make_user):
    client, _, _, user = auth_client
    run = Run(id="r1", created_by=user.id, mode="ask", stage="completed")
    thread = Thread(id="l1", run_id="r1", persona="researcher", status="completed")
    session.add_all([run, thread]); session.commit()
    session.add(Event(run_id="r1", thread_id="l1", seq=0, type="message", title="t", payload={}))
    session.commit()
    r = client.get("/sessions/r1/replay")
    assert r.status_code == 200
    assert r.json()["run_id"] == "r1"
    assert len(r.json()["events"]) == 1


def test_session_replay_not_found(auth_client):
    client, _, _, _ = auth_client
    assert client.get("/sessions/ghost/replay").status_code == 404


def test_session_transcript_streams_jsonl_file(auth_client, session):
    client, _, _, user = auth_client
    session.add(Run(id="r1", created_by=user.id, mode="ask", stage="completed"))
    session.commit()
    from app.services import transcript
    transcript.append("r1", {"seq": 0, "kind": "message", "title": "first"})
    transcript.append("r1", {"seq": 1, "kind": "command", "title": "second"})

    r = client.get("/sessions/r1/transcript")
    assert r.status_code == 200
    lines = [json.loads(line) for line in r.text.strip().splitlines()]
    assert [line["title"] for line in lines] == ["first", "second"]

    after = client.get("/sessions/r1/transcript", params={"after_seq": 0})
    assert [json.loads(x)["seq"] for x in after.text.strip().splitlines()] == [1]


def test_session_transcript_falls_back_to_events(auth_client, session):
    """Runs that predate the transcript writer still export — the events table
    is the source of truth and the file is only a mirror."""
    client, _, _, user = auth_client
    session.add_all([
        Run(id="r1", created_by=user.id, mode="ask", stage="completed"),
        Thread(id="l1", run_id="r1", persona="researcher", status="completed"),
    ])
    session.commit()
    session.add(Event(run_id="r1", thread_id="l1", seq=0, type="message", title="from-db", payload={}))
    session.commit()

    r = client.get("/sessions/r1/transcript")
    assert r.status_code == 200
    assert json.loads(r.text.strip())["title"] == "from-db"


def test_session_transcript_not_found(auth_client):
    client, _, _, _ = auth_client
    assert client.get("/sessions/ghost/transcript").status_code == 404


def test_session_resumable(auth_client, session, make_user, monkeypatch):
    client, _, _, user = auth_client
    run = Run(id="r1", created_by=user.id, mode="ask", stage="completed")
    thread = Thread(id="l1", run_id="r1", persona="researcher", status="completed", session_id="s1")
    session.add_all([run, thread]); session.commit()
    import app.api.sessions as route
    monkeypatch.setattr(route, "session_volume_exists", lambda run_id, thread_id: True)
    r = client.get("/sessions/r1/resumable")
    assert r.status_code == 200
    body = r.json()
    assert body["threads"][0]["resumable"] is True


def test_session_resumable_not_found(auth_client):
    client, _, _, _ = auth_client
    assert client.get("/sessions/ghost/resumable").status_code == 404


def test_session_resume(auth_client, session, make_user):
    client, _, services, user = auth_client
    run = Run(id="r1", created_by=user.id, mode="ask", stage="completed", title="orig")
    session.add(run); session.commit()
    r = client.post("/sessions/r1/resume")
    assert r.status_code == 200
    body = r.json()
    assert body["continues"] == "r1"
    # H-22: resume continues the SAME run row (no fresh run), so run_id is
    # the original id and the manager records a resume, not a create.
    assert body["run_id"] == "r1"
    assert services["run_manager"].resumed == ["r1"]
    assert services["run_manager"].created == []


def test_session_resume_not_found(auth_client):
    client, _, _, _ = auth_client
    assert client.post("/sessions/ghost/resume").status_code == 404


def test_sessions_require_auth(app_client):
    client, _, _ = app_client
    assert client.get("/sessions/ghost/replay").status_code == 401


# --------------------------------------------------------------- plan HITL intents
def test_post_intent_approve_plan(auth_client, session, make_user):
    client, _, services, user = auth_client
    run = Run(id="r1", created_by=user.id, mode="plan", stage="awaiting_user", repo="ServerApp", title="t",
              available_actions=["review_plan", "approve_plan", "reject_plan"])
    plan = Plan(run_id="r1", structured={"title": "P", "steps": [{"index": 0, "title": "s0"}]}, status="draft")
    session.add_all([run, plan]); session.commit()
    r = client.post("/runs/r1/intent", json={"intent": "approve_plan", "source": "button"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["intent"] == "approve_plan"
    assert body["plan_status"] == "approved"
    assert services["run_manager"].continued == ["r1"]


def test_post_intent_approve_plan_422_when_not_draft(auth_client, session, make_user):
    """C2: a stale approve (plan already decided) maps to 422, never a bare 500."""
    client, _, services, user = auth_client
    run = Run(id="r1", created_by=user.id, mode="plan", stage="awaiting_user", repo="ServerApp", title="t",
              available_actions=["review_plan", "approve_plan", "reject_plan"])
    plan = Plan(run_id="r1", structured={"title": "P", "steps": []}, status="rejected")
    session.add_all([run, plan]); session.commit()
    r = client.post("/runs/r1/intent", json={"intent": "approve_plan", "source": "button"})
    assert r.status_code == 422
    assert "not awaiting decision" in r.json()["detail"]
    assert services["run_manager"].continued == []


def test_post_intent_reject_plan(auth_client, session, make_user):
    client, _, services, user = auth_client
    run = Run(id="r1", created_by=user.id, mode="plan", stage="awaiting_user", repo="ServerApp", title="t",
              available_actions=["review_plan", "approve_plan", "reject_plan"])
    plan = Plan(run_id="r1", structured={"title": "P", "steps": []}, status="draft")
    session.add_all([run, plan]); session.commit()
    r = client.post("/runs/r1/intent", json={
        "intent": "reject_plan", "source": "text", "text": "redo the citations", "confirmed": True,
    })
    assert r.status_code == 200
    assert r.json()["plan_status"] == "rejected"
    assert services["run_manager"].replanned
    assert services["run_manager"].replanned[0][0] == "r1"


def test_post_intent_create_pr(auth_client, session, make_user):
    client, _, services, user = auth_client
    run = Run(id="r1", created_by=user.id, mode="development", stage="verifying", repo="ServerApp", title="t",
              available_actions=["review_evidence", "create_pr"])
    session.add(run); session.commit()
    r = client.post("/runs/r1/intent", json={"intent": "create_pr", "source": "button"})
    assert r.status_code == 200
    assert r.json()["intent"] == "create_pr"
    assert r.json()["pr_id"] == 99
    assert services["run_manager"].prs_opened == ["r1"]


def test_post_intent_merge_pr_needs_confirm(auth_client, session, make_user):
    client, _, _, user = auth_client
    run = Run(id="r1", created_by=user.id, mode="development", stage="pr_ready", repo="ServerApp", title="t",
              available_actions=["review_diff", "merge_pr"])
    session.add(run); session.commit()
    r = client.post("/runs/r1/intent", json={"intent": "merge_pr", "source": "button", "confirmed": False})
    assert r.status_code == 200
    assert r.json()["status"] == "confirm"


def test_post_intent_merge_pr_confirmed(auth_client, session, make_user):
    client, _, services, user = auth_client
    run = Run(id="r1", created_by=user.id, mode="development", stage="pr_ready", repo="ServerApp", title="t",
              available_actions=["review_diff", "merge_pr"])
    session.add(run); session.commit()
    r = client.post("/runs/r1/intent", json={"intent": "merge_pr", "source": "button", "confirmed": True})
    assert r.status_code == 200
    assert r.json()["intent"] == "merge_pr"
    assert services["run_manager"].prs_merged == [("r1", user.id)]


# --------------------------------------------------------------- thread controls
def test_post_intent_stop_thread(auth_client, session, make_user):
    client, _, services, user = auth_client
    run = Run(id="r1", created_by=user.id, mode="agent-rnd", stage="investigating")
    thread = Thread(id="l1", run_id="r1", persona="explorer", status="running")
    session.add_all([run, thread]); session.commit()
    r = client.post("/runs/r1/intent",
                    json={"intent": "stop_thread", "source": "button", "thread_id": "l1"})
    assert r.status_code == 200
    assert services["run_manager"].stopped_threads == [("r1", "l1")]


def test_post_intent_stop_thread_idor_rejects_other_run_thread(auth_client, session, make_user):
    """C-10: STOP_THREAD must not interrupt a thread that belongs to another
    run. Pair the caller's own run_id with another run's thread_id — the
    ownership guard returns 404, never reaching run_manager.stop_thread."""
    client, _, services, user = auth_client
    other = make_user("other", role="member", status="active")
    own_run = Run(id="r1", created_by=user.id, mode="agent-rnd", stage="investigating")
    other_run = Run(id="r2", created_by=other.id, mode="agent-rnd", stage="investigating")
    other_thread = Thread(id="l2", run_id="r2", persona="explorer", status="running")
    session.add_all([own_run, other_run, other_thread]); session.commit()
    r = client.post("/runs/r1/intent",
                    json={"intent": "stop_thread", "source": "button", "thread_id": "l2"})
    assert r.status_code == 404
    assert services["run_manager"].stopped_threads == []


def test_post_intent_stop_thread_rejects_unknown_thread(auth_client, session, make_user):
    """C-10: a thread_id that doesn't exist is 404, not a silent no-op."""
    client, _, services, user = auth_client
    session.add(Run(id="r1", created_by=user.id, mode="agent-rnd", stage="investigating"))
    session.commit()
    r = client.post("/runs/r1/intent",
                    json={"intent": "stop_thread", "source": "button", "thread_id": "ghost"})
    assert r.status_code == 404
    assert services["run_manager"].stopped_threads == []


def test_post_intent_stop_thread_needs_thread_id(auth_client, session, make_user):
    client, _, _, user = auth_client
    session.add(Run(id="r1", created_by=user.id, mode="agent-rnd", stage="investigating"))
    session.commit()
    r = client.post("/runs/r1/intent", json={"intent": "stop_thread", "source": "button"})
    assert r.status_code == 422


def test_post_intent_pin_finding(auth_client, session, make_user):
    client, _, services, user = auth_client
    session.add(Run(id="r1", created_by=user.id, mode="agent-rnd", stage="investigating"))
    session.commit()
    r = client.post("/runs/r1/intent",
                    json={"intent": "pin_finding", "source": "button", "thread_id": "l1",
                          "payload": {"note": "dedupe key"}})
    assert r.status_code == 200
    assert services["run_manager"].pinned == [("r1", "l1", "dedupe key")]


def test_post_intent_kill_replace_needs_confirm(auth_client, session, make_user):
    """kill_replace is irreversible (contracts.IRREVERSIBLE_INTENTS) — the button
    flow gets a confirm card first."""
    client, _, _, user = auth_client
    session.add(Run(id="r1", created_by=user.id, mode="agent-rnd", stage="investigating"))
    session.commit()
    r = client.post("/runs/r1/intent",
                    json={"intent": "kill_replace", "source": "button", "thread_id": "l1"})
    assert r.status_code == 200
    assert r.json()["status"] == "confirm"


def test_post_intent_kill_replace_returns_replacement(auth_client, session, make_user):
    client, _, services, user = auth_client
    session.add(Run(id="r1", created_by=user.id, mode="agent-rnd", stage="investigating"))
    session.commit()
    r = client.post("/runs/r1/intent",
                    json={"intent": "kill_replace", "source": "button", "thread_id": "l1",
                          "confirmed": True})
    assert r.status_code == 200
    assert r.json()["replacement_thread_id"] == "replacement-l1"


def test_threads_serializer_includes_heartbeat_and_container(auth_client, session, make_user):
    from datetime import datetime, timezone
    client, _, _, user = auth_client
    session.add(Run(id="r1", created_by=user.id, mode="agent-rnd", stage="investigating"))
    session.add(Thread(id="l1", run_id="r1", persona="explorer", status="running",
                     heartbeat_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
                     container_id="cid-1"))
    session.commit()
    r = client.get("/runs/r1/threads")
    assert r.status_code == 200
    thread = r.json()[0]
    assert thread["heartbeat_at"].startswith("2026-08-01")
    assert thread["has_container"] is True


# --------------------------------------------------------------- evidence serializer
def test_run_evidence_404_without_plan(auth_client, session, make_user):
    client, _, _, user = auth_client
    session.add(Run(id="r1", created_by=user.id, mode="development", stage="verifying"))
    session.commit()
    r = client.get("/runs/r1/evidence")
    assert r.status_code == 404


def test_run_evidence_returns_package_with_hash(auth_client, session, make_user):
    client, _, _, user = auth_client
    session.add(Run(id="r1", created_by=user.id, mode="development", stage="pr_ready", title="t"))
    session.add(Thread(id="l1", run_id="r1", persona="developer", status="completed"))
    session.add(Plan(run_id="r1", status="approved",
                     structured={"title": "P", "steps": [{"index": 0, "title": "s0", "status": "done"}]}))
    session.commit()
    r = client.get("/runs/r1/evidence")
    assert r.status_code == 200
    body = r.json()
    assert body["plan_title"] == "P"
    assert len(body["sha256"]) == 64


# --------------------------------------------------------------- knowledge api
def test_knowledge_draft_phi_checkpoint(auth_client):
    client, _, _, _ = auth_client
    r = client.post("/knowledge", json={"content": "lesson", "proposed_scope": "global"})
    assert r.status_code == 201
    body = r.json()
    assert body["scope"] == "user" and body["status"] == "draft"
    assert "phi_checkpoint" in body


def test_knowledge_pending_and_approve_flow(auth_client, session):
    client, _, _, user = auth_client
    client.post("/knowledge", json={"content": "lesson", "trigger_description": "when X"})
    pending = client.get("/knowledge/pending").json()
    assert len(pending) == 1
    item_id = pending[0]["id"]
    r = client.post(f"/knowledge/{item_id}/approve", json={"scope": "repo", "repo": "ServerApp"})
    assert r.status_code == 200
    assert r.json()["scope"] == "repo" and r.json()["status"] == "approved"
    # decisions are final
    assert client.post(f"/knowledge/{item_id}/reject").status_code == 422
    assert client.get("/knowledge/pending").json() == []


def test_knowledge_approve_repo_scope_needs_repo(auth_client):
    client, _, _, _ = auth_client
    client.post("/knowledge", json={"content": "lesson"})
    item_id = client.get("/knowledge/pending").json()[0]["id"]
    r = client.post(f"/knowledge/{item_id}/approve", json={"scope": "repo"})
    assert r.status_code == 422


def test_knowledge_corpus_hides_other_users_private_items(auth_client, session, make_user):
    client, _, _, user = auth_client
    other = make_user("bob")
    session.add(KnowledgeItem(content="shared", scope="global", status="approved"))
    session.add(KnowledgeItem(content="bob private", scope="user", status="approved",
                              created_by=other.id))
    session.commit()
    client.post("/knowledge", json={"content": "my wip"})
    contents = {c["content"] for c in client.get("/knowledge").json()}
    assert contents == {"shared", "my wip"}


# --------------------------------------------------------------- ideas api
def test_ideas_thread_lifecycle(auth_client, monkeypatch):
    client, _, _, user = auth_client
    r = client.post("/ideas", json={"title": "fleet graph?", "body": "worth it?"})
    assert r.status_code == 201
    tid = r.json()["id"]
    r = client.post(f"/ideas/{tid}/comments", json={"body": "first voice"})
    assert r.status_code == 201
    assert r.json()["author_name"] == user.display_name
    listed = client.get("/ideas").json()
    assert listed[0]["comment_count"] == 1
    detail = client.get(f"/ideas/{tid}").json()
    assert detail["comments"][0]["body"] == "first voice"


def test_ideas_ask_counsel_uses_injected_completion(auth_client, monkeypatch):
    import app.api.ideas as ideas_api
    import app.services.ideas as ideas_svc
    client, _, _, _ = auth_client
    tid = client.post("/ideas", json={"title": "t", "body": "b"}).json()["id"]

    async def fake(messages):
        return "Counsel says: do it after the flywheel."
    monkeypatch.setattr(ideas_svc, "gateway_complete", fake)
    r = client.post(f"/ideas/{tid}/ask-counsel")
    assert r.status_code == 201
    assert r.json()["author_ref"] == "counsel"
    detail = client.get(f"/ideas/{tid}").json()
    assert detail["comments"][-1]["author_type"] == "agent"


def test_ideas_summarize_and_promote(auth_client, monkeypatch):
    import app.services.ideas as ideas_svc
    client, _, _, _ = auth_client
    tid = client.post("/ideas", json={"title": "t", "body": "b"}).json()["id"]

    async def fake_lead(messages):
        return '{"consensus": "c", "disagreements": [], "recommendation": "r", "open_questions": []}'
    monkeypatch.setattr(ideas_svc, "gateway_complete", fake_lead)
    r = client.post(f"/ideas/{tid}/summarize")
    assert r.status_code == 200 and r.json()["consensus"] == "c"
    assert client.get(f"/ideas/{tid}").json()["status"] == "summarized"


def test_ideas_promote_creates_plan_run_and_pins_it(auth_client):
    client, _, services, _ = auth_client
    fake_rm = services["run_manager"]
    tid = client.post("/ideas", json={"title": "fleet graph?", "body": "worth it?"}).json()["id"]
    client.post(f"/ideas/{tid}/comments", json={"body": "voice"})
    r = client.post(f"/ideas/{tid}/promote")
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == "promoted"
    assert len(fake_rm.created) == 1
    assert "# fleet graph?" in fake_rm.created[0]["task"]
    assert "voice" in fake_rm.created[0]["task"]
    assert body["promoted_run_id"] == fake_rm.created[0]["id"]


def test_ideas_404s(auth_client):
    client, _, _, _ = auth_client
    assert client.get("/ideas/999").status_code == 404
    assert client.post("/ideas/999/comments", json={"body": "x"}).status_code == 404


# --------------------------------------------------------------- byo-pat api
def test_byo_pat_status_store_revoke(auth_client, session, monkeypatch):
    import app.services.byo_pat as svc
    client, _, _, user = auth_client
    user.ado_descriptor = "descriptor-alice"
    session.commit()
    assert client.get("/me/byo-pat").json() == {"configured": False}

    async def verify_ok(pat):
        return "descriptor-alice"
    monkeypatch.setattr(svc, "connection_data_descriptor", verify_ok)
    r = client.post("/me/byo-pat", json={"pat": "pat-xyz"})
    assert r.status_code == 200
    body = r.json()
    assert body["configured"] is True and body["days_remaining"] == 90
    assert "pat-xyz" not in str(body)  # write-only
    assert client.delete("/me/byo-pat").status_code == 204
    assert client.get("/me/byo-pat").json() == {"configured": False}


def test_byo_pat_store_identity_mismatch_422(auth_client, session, monkeypatch):
    import app.services.byo_pat as svc
    client, _, _, user = auth_client
    user.ado_descriptor = "descriptor-alice"
    session.commit()

    async def verify_other(pat):
        return "descriptor-bob"
    monkeypatch.setattr(svc, "connection_data_descriptor", verify_other)
    r = client.post("/me/byo-pat", json={"pat": "pat-xyz"})
    assert r.status_code == 422
    assert "does not match" in r.json()["detail"]


# --------------------------------------------------------------- webhook ingress
def _ado_hook(body: bytes, secret: str) -> str:
    import hashlib
    import hmac
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_webhook_rejects_bad_signature(app_client, monkeypatch):
    from app.core.config import get_settings
    client, _, _ = app_client
    monkeypatch.setattr(get_settings(), "ado_webhook_secret", "s3cret")
    r = client.post("/webhooks/ado", content=b"{}", headers={"X-Zagent-Signature": "bad"})
    assert r.status_code == 401
    # no header at all is also a rejection (fail-closed)
    assert client.post("/webhooks/ado", content=b"{}").status_code == 401


def test_webhook_full_ingress_starts_run(app_client, session, make_user, monkeypatch):
    from app.core.config import get_settings
    from app.db.models.trigger import Trigger
    client, _, services = app_client
    monkeypatch.setattr(get_settings(), "ado_webhook_secret", "s3cret")
    u = make_user()
    u.ado_descriptor = "desc-ali"
    u.status = "active"
    session.add(Trigger(name="plan-on-state", source="ado_webhook",
                        filter_json={"event_type": "work_item.updated", "state": "zagent-plan"},
                        mode="plan", autonomy="gated"))
    session.commit()
    import json as _json
    payload = _json.dumps({"resource": {"workItemId": 42, "rev": 1,
                                        "revisedBy": {"id": "desc-ali"},
                                        "fields": {"System.State": {"newValue": "zagent-plan"},
                                                   "System.Title": {"newValue": "hook task"}}}}).encode()
    r = client.post("/webhooks/ado", content=payload,
                    headers={"X-Zagent-Signature": _ado_hook(payload, "s3cret")})
    assert r.status_code == 200
    assert r.json()["status"] == "matched"
    assert "hook task" in services["run_manager"].created[0]["task"]


# --------------------------------------------------------------- plan serializer
def test_run_plan_serializer(auth_client, session, make_user):
    client, _, _, user = auth_client
    run = Run(id="r1", created_by=user.id, mode="plan", stage="awaiting_user", repo="ServerApp", title="t")
    plan = Plan(run_id="r1", structured={"title": "P", "steps": [], "blast_radius": ["ClientApp"]},
                status="draft")
    session.add_all([run, plan]); session.commit()
    from app.db.models.run import PlanStep
    session.add(PlanStep(plan_id=plan.id, index=0, title="s0", description="d",
                        repo="ServerApp", files=["a.ts"], success_criterion="tests", status="pending"))
    session.commit()
    r = client.get("/runs/r1/plan")
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == plan.id
    assert body["status"] == "draft"
    assert body["structured"]["blast_radius"] == ["ClientApp"]
    assert len(body["steps"]) == 1
    assert body["steps"][0]["title"] == "s0"


def test_run_plan_serializer_not_found(auth_client, session, make_user):
    client, _, _, user = auth_client
    run = Run(id="r1", created_by=user.id, mode="plan", stage="queued", title="t")
    session.add(run); session.commit()
    assert client.get("/runs/r1/plan").status_code == 404


def test_run_plan_serializer_other_user_denied(auth_client, session, make_user):
    client, _, _, _ = auth_client
    other = make_user("bob", role="member", status="active")
    run = Run(id="r1", created_by=other.id, mode="plan", stage="queued", title="t")
    session.add(run); session.commit()
    assert client.get("/runs/r1/plan").status_code == 404


# --------------------------------------------------------------- start_plan intent (debug -> plan promotion)
def test_post_intent_start_plan(auth_client, session, make_user):
    client, _, services, user = auth_client
    run = Run(id="r1", created_by=user.id, mode="debug", stage="awaiting_user", repo="ServerApp",
              title="Bug: dedupe", available_actions=["review_plan", "start_plan"])
    plan = Plan(run_id="r1", structured={"title": "Fix", "steps": []}, status="draft")
    session.add_all([run, plan]); session.commit()
    r = client.post("/runs/r1/intent", json={"intent": "start_plan", "source": "button"})
    assert r.status_code == 200
    assert r.json()["intent"] == "start_plan"
    assert services["run_manager"].started_plans == ["r1"]
