import pytest

from app.ado import client as ado_mod
from app.ado.client import AdoClient, AdoIdentity, IdentityResolutionError
from tests.conftest import FakeResponse, install_fake_httpx


def test_identity_resolution_error_is_runtime_error():
    assert issubclass(IdentityResolutionError, RuntimeError)


def test_ado_identity_dataclass():
    ident = AdoIdentity(descriptor="d", display_name="n", mail="m")
    assert ident.descriptor == "d"


async def test_list_repos(monkeypatch):
    routes = {"/_apis/git/repositories": FakeResponse({"value": [{"name": "A"}, {"name": "B"}]})}
    fake = install_fake_httpx(monkeypatch, ado_mod, routes)
    c = AdoClient(pat="p", org="o")
    repos = await c.list_repos()
    assert [r["name"] for r in repos] == ["A", "B"]
    assert fake.calls[0][0] == "GET"


async def test_list_branches(monkeypatch):
    routes = {"/refs": FakeResponse({"value": [
        {"name": "refs/heads/main"}, {"name": "refs/heads/dev"},
    ]})}
    install_fake_httpx(monkeypatch, ado_mod, routes)
    c = AdoClient(pat="p", org="o")
    branches = await c.list_branches("repo-1")
    assert branches == ["main", "dev"]


async def test_resolve_identity_single_match(monkeypatch):
    routes = {"/graph/users": FakeResponse({"value": [
        {"descriptor": "d1", "mailAddress": "Ali@x.com", "displayName": "Ali"},
        {"descriptor": "d2", "mailAddress": "Bob@x.com", "displayName": "Bob"},
    ]})}
    install_fake_httpx(monkeypatch, ado_mod, routes)
    c = AdoClient(pat="p", org="o")
    ident = await c.resolve_identity("ali@x.com")
    assert ident.descriptor == "d1"
    assert ident.display_name == "Ali"
    assert ident.mail == "Ali@x.com"


async def test_resolve_identity_no_match_raises(monkeypatch):
    routes = {"/graph/users": FakeResponse({"value": [
        {"descriptor": "d2", "mailAddress": "Bob@x.com"},
    ]})}
    install_fake_httpx(monkeypatch, ado_mod, routes)
    c = AdoClient(pat="p", org="o")
    with pytest.raises(IdentityResolutionError, match="no ADO identity"):
        await c.resolve_identity("ghost@x.com")


async def test_resolve_identity_multiple_matches_raises(monkeypatch):
    routes = {"/graph/users": FakeResponse({"value": [
        {"descriptor": "d1", "mailAddress": "Ali@x.com"},
        {"descriptor": "d2", "mailAddress": "Ali@x.com"},
    ]})}
    install_fake_httpx(monkeypatch, ado_mod, routes)
    c = AdoClient(pat="p", org="o")
    with pytest.raises(IdentityResolutionError, match="2 ADO identities"):
        await c.resolve_identity("ali@x.com")


async def test_connection_data(monkeypatch):
    routes = {"/connectionData": FakeResponse({"authenticatedUser": {
        "descriptor": "desc", "customDisplayName": "Me",
        "properties": {"Account": {"$value": "me@x.com"}},
    }})}
    install_fake_httpx(monkeypatch, ado_mod, routes)
    c = AdoClient(pat="p", org="o")
    ident = await c.connection_data("byopat")
    assert ident.descriptor == "desc"
    assert ident.display_name == "Me"
    assert ident.mail == "me@x.com"


async def test_connection_data_fallback_display_name(monkeypatch):
    routes = {"/connectionData": FakeResponse({"authenticatedUser": {
        "descriptor": "desc", "providerDisplayName": "Provider",
        "properties": {},
    }})}
    install_fake_httpx(monkeypatch, ado_mod, routes)
    c = AdoClient(pat="p", org="o")
    ident = await c.connection_data("byopat")
    assert ident.display_name == "Provider"
    assert ident.mail == ""


async def test_get_work_item(monkeypatch):
    routes = {"/wit/workitems/42": FakeResponse({"id": 42, "fields": {"System.Title": "T"}})}
    install_fake_httpx(monkeypatch, ado_mod, routes)
    c = AdoClient(pat="p", org="o")
    wi = await c.get_work_item(42)
    assert wi["id"] == 42


async def test_my_active_tickets_empty(monkeypatch):
    routes = {"/wit/wiql": FakeResponse({"workItems": []})}
    install_fake_httpx(monkeypatch, ado_mod, routes)
    c = AdoClient(pat="p", org="o")
    assert await c.my_active_tickets("desc") == []


async def test_my_active_tickets_fetches_details(monkeypatch):
    routes = {
        "/wit/wiql": FakeResponse({"workItems": [{"id": 1}, {"id": 2}]}),
        "/wit/workitems": FakeResponse({"value": [{"id": 1, "fields": {"System.Title": "A"}}]}),
    }
    install_fake_httpx(monkeypatch, ado_mod, routes)
    c = AdoClient(pat="p", org="o")
    items = await c.my_active_tickets("desc")
    assert len(items) == 1
    assert items[0]["id"] == 1


async def test_create_pull_request(monkeypatch):
    routes = {"/pullrequests": FakeResponse({"pullRequestId": 7, "title": "T"})}
    install_fake_httpx(monkeypatch, ado_mod, routes)
    c = AdoClient(pat="p", org="o")
    pr = await c.create_pull_request("repo-1", "feat", "main", "T", "D", work_item_id=42)
    assert pr["pullRequestId"] == 7


async def test_complete_pull_request(monkeypatch):
    routes = {"/pullrequests/7": FakeResponse({"status": "completed"})}
    install_fake_httpx(monkeypatch, ado_mod, routes)
    c = AdoClient(pat="p", org="o")
    result = await c.complete_pull_request("repo-1", 7, "merged")
    assert result["status"] == "completed"


async def test_complete_pull_request_plumbs_commit_message(monkeypatch):
    """G-23: complete_pull_request must send the human's merge_commit_message
    through to ADO as completionOptions.mergeCommitMessage (the audit
    trail ties the ADO merge commit to the Zagent approval). The existing
    test only checked the response status, not that the message was
    plumbed into the PATCH body. Capture the PATCH body and assert it."""
    captured: dict[str, object] = {}

    class _CaptureClient:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def patch(self, url, **kw):
            captured["url"] = url
            captured["json"] = kw.get("json")
            return FakeResponse({"status": "completed"})

    monkeypatch.setattr(ado_mod.httpx, "AsyncClient", lambda *a, **k: _CaptureClient())
    c = AdoClient(pat="p", org="o")
    msg = "Zagent run r1 — evidence package attached"
    await c.complete_pull_request("repo-1", 7, msg)
    body = captured["json"]
    assert body["status"] == "completed"
    assert body["completionOptions"]["mergeCommitMessage"] == msg


async def test_list_repos_raises_on_http_error(monkeypatch):
    routes = {"/_apis/git/repositories": FakeResponse(status_code=401)}
    install_fake_httpx(monkeypatch, ado_mod, routes)
    c = AdoClient(pat="p", org="o")
    with pytest.raises(Exception):
        await c.list_repos()


def test_client_headers_set():
    c = AdoClient(pat="secret", org="o")
    assert c._headers["Authorization"].startswith("Basic ")
    assert c._base == "https://dev.azure.com/o"
