import asyncio

import pytest

from app.gateway import litellm
from app.gateway.litellm import GatewayClient, VirtualKey, retry_with_backoff
from tests.conftest import FakeResponse, install_fake_httpx


def test_virtual_key_dataclass():
    vk = VirtualKey(key="sk-1", alias="a", max_budget=5.0)
    assert vk.key == "sk-1" and vk.alias == "a" and vk.max_budget == 5.0


async def test_mint_key_returns_virtual_key(monkeypatch):
    routes = {"/key/generate": FakeResponse({"key": "sk-new"})}
    fake = install_fake_httpx(monkeypatch, litellm, routes)
    client = GatewayClient(base_url="http://gw", master_key="mk")
    vk = await client.mint_key("thread-1", 5.0)
    assert vk.key == "sk-new"
    assert vk.alias == "thread-1"
    assert vk.max_budget == 5.0
    assert fake.calls[0][0] == "POST"


async def test_mint_key_raises_on_http_error(monkeypatch):
    routes = {"/key/generate": FakeResponse(status_code=500)}
    install_fake_httpx(monkeypatch, litellm, routes)
    client = GatewayClient(base_url="http://gw", master_key="mk")
    with pytest.raises(Exception):
        await client.mint_key("thread-1", 5.0)


async def test_delete_key(monkeypatch):
    routes = {"/key/delete": FakeResponse({})}
    fake = install_fake_httpx(monkeypatch, litellm, routes)
    client = GatewayClient(base_url="http://gw", master_key="mk")
    await client.delete_key("sk-1")
    assert fake.calls[0][0] == "POST"


async def test_key_spend(monkeypatch):
    routes = {"/key/info": FakeResponse({"info": {"spend": 12.34}})}
    install_fake_httpx(monkeypatch, litellm, routes)
    client = GatewayClient(base_url="http://gw", master_key="mk")
    spend = await client.key_spend("sk-1")
    assert spend == 12.34


async def test_key_spend_defaults_zero(monkeypatch):
    routes = {"/key/info": FakeResponse({})}
    install_fake_httpx(monkeypatch, litellm, routes)
    client = GatewayClient(base_url="http://gw", master_key="mk")
    assert await client.key_spend("sk-1") == 0.0


async def test_read_spend_reconciled_polls(monkeypatch):
    routes = {"/key/info": FakeResponse({"info": {"spend": 7.5}})}
    install_fake_httpx(monkeypatch, litellm, routes)
    client = GatewayClient(base_url="http://gw", master_key="mk")
    real_sleep = asyncio.sleep
    async def fake_sleep(t):
        await real_sleep(0)
    monkeypatch.setattr(litellm.asyncio, "sleep", fake_sleep)
    spend = await client.read_spend_reconciled("sk-1", grace_seconds=0, polls=2)
    assert spend == 7.5


async def test_health_ok(monkeypatch):
    routes = {"/health/liveliness": FakeResponse({}, status_code=200)}
    install_fake_httpx(monkeypatch, litellm, routes)
    client = GatewayClient(base_url="http://gw", master_key="mk")
    assert await client.health() is True


async def test_health_down_returns_false(monkeypatch):
    routes = {"/health/liveliness": FakeResponse({}, status_code=503)}
    install_fake_httpx(monkeypatch, litellm, routes)
    client = GatewayClient(base_url="http://gw", master_key="mk")
    assert await client.health() is False


async def test_health_httperror_returns_false(monkeypatch):
    def boom(*a, **k):
        raise litellm.httpx.HTTPError("nope")
    monkeypatch.setattr(litellm.httpx, "AsyncClient", boom)
    client = GatewayClient(base_url="http://gw", master_key="mk")
    assert await client.health() is False


async def test_retry_with_backoff_succeeds_eventually(monkeypatch):
    calls = {"n": 0}
    async def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise litellm.httpx.HTTPStatusError("5xx", request=None, response=FakeResponse(status_code=503))
        return "ok"
    real_sleep = asyncio.sleep
    async def fake_sleep(t):
        await real_sleep(0)
    monkeypatch.setattr(litellm.asyncio, "sleep", fake_sleep)
    result = await retry_with_backoff(flaky, attempts=5, base_delay=0)
    assert result == "ok"
    assert calls["n"] == 3


async def test_retry_with_backoff_exhausts(monkeypatch):
    async def always_fail():
        raise litellm.httpx.TimeoutException("timeout")
    real_sleep = asyncio.sleep
    async def fake_sleep(t):
        await real_sleep(0)
    monkeypatch.setattr(litellm.asyncio, "sleep", fake_sleep)
    with pytest.raises(litellm.httpx.TimeoutException):
        await retry_with_backoff(always_fail, attempts=3, base_delay=0)


async def test_retry_with_backoff_passes_through_nonretryable(monkeypatch):
    async def boom():
        raise ValueError("not retryable")
    with pytest.raises(ValueError):
        await retry_with_backoff(boom, attempts=3, base_delay=0)


def test_client_uses_settings_defaults(monkeypatch):
    client = GatewayClient()
    assert client.base_url.endswith("litellm") or client.base_url  # constructed from settings
    assert client.master_key  # non-empty from env


# --------------------------------------------------------------- _cli entrypoint
class _CliGateway:
    def __init__(self):
        self.minted = []
        self.spent = []
        self.deleted = []

    async def mint_key(self, alias, budget, models=None):
        self.minted.append((alias, budget))
        return VirtualKey(key="sk-cli", alias=alias, max_budget=budget)

    async def key_spend(self, key):
        self.spent.append(key)
        return 4.2

    async def delete_key(self, key):
        self.deleted.append(key)


def test_cli_mint(monkeypatch, capsys):
    import sys
    fake = _CliGateway()
    monkeypatch.setattr(litellm, "GatewayClient", lambda: fake)
    monkeypatch.setattr(sys, "argv", ["litellm", "mint", "--alias", "thread-1", "--budget", "3.5"])
    litellm._cli()
    assert fake.minted == [("thread-1", 3.5)]
    assert capsys.readouterr().out.strip() == "sk-cli"


def test_cli_mint_default_budget(monkeypatch, capsys):
    import sys
    fake = _CliGateway()
    monkeypatch.setattr(litellm, "GatewayClient", lambda: fake)
    monkeypatch.setattr(sys, "argv", ["litellm", "mint", "--alias", "thread-2"])
    litellm._cli()
    assert fake.minted == [("thread-2", 5.0)]


def test_cli_spend(monkeypatch, capsys):
    import sys
    fake = _CliGateway()
    monkeypatch.setattr(litellm, "GatewayClient", lambda: fake)
    monkeypatch.setattr(sys, "argv", ["litellm", "spend", "--key", "sk-1"])
    litellm._cli()
    out = capsys.readouterr().out
    assert "$4.2000" in out
    assert fake.spent == ["sk-1"]


def test_cli_delete(monkeypatch, capsys):
    import sys
    fake = _CliGateway()
    monkeypatch.setattr(litellm, "GatewayClient", lambda: fake)
    monkeypatch.setattr(sys, "argv", ["litellm", "delete", "--key", "sk-1"])
    litellm._cli()
    assert fake.deleted == ["sk-1"]
    assert capsys.readouterr().out.strip() == "deleted"


def test_cli_requires_subcommand(monkeypatch):
    import sys
    monkeypatch.setattr(sys, "argv", ["litellm"])
    with pytest.raises(SystemExit):
        litellm._cli()
