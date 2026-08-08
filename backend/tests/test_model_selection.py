"""Model selection: composer picks gateway aliases → validated at create_run,
fanned out per lane in ask mode, scoped per virtual key, priced per lane.

Covers: run_manager._validate_models (registry guard + ask-only compare),
POST /runs pass-through, GET /models registry endpoint.
"""

import pytest

from app.orchestrator.run_manager import RunManager


def _rm() -> RunManager:
    # _validate_models touches no collaborator — a bare manager is enough.
    return RunManager(None, None, None, None)


# ------------------------------------------------------------ _validate_models

def test_validate_models_none_means_default():
    assert _rm()._validate_models(None, "ask") is None
    assert _rm()._validate_models([], "ask") is None


def test_validate_models_single_alias_any_mode():
    assert _rm()._validate_models(["glm-5.2"], "development") == ["glm-5.2"]


def test_validate_models_dedupes_preserving_order():
    picked = _rm()._validate_models(
        ["glm-5.2", "kimi-k2.6", "glm-5.2"], "ask")
    assert picked == ["glm-5.2", "kimi-k2.6"]


def test_validate_models_unknown_alias_rejected():
    with pytest.raises(ValueError, match="unknown model 'gpt-9'"):
        _rm()._validate_models(["kimi-k2.6", "gpt-9"], "ask")


def test_validate_models_multi_is_ask_only():
    with pytest.raises(ValueError, match="ask-mode only"):
        _rm()._validate_models(["kimi-k2.6", "glm-5.2"], "development")


# ---------------------------------------------------------- _validate_reasoning

def test_validate_reasoning_none_means_provider_default():
    assert _rm()._validate_reasoning(None, None) is None
    assert _rm()._validate_reasoning({}, ["kimi-k2.6"]) is None


def test_validate_reasoning_off_and_effort_accepted():
    rm = _rm()
    assert rm._validate_reasoning(
        {"glm-5.2": "high"}, ["glm-5.2"]) == {"glm-5.2": "high"}
    assert rm._validate_reasoning(
        {"kimi-k2.6": "off"}, ["kimi-k2.6"]) == {"kimi-k2.6": "off"}


def test_validate_reasoning_targets_default_when_nothing_selected():
    # The picker shows the reasoning control on the default model's row even
    # with an empty selection — that entry is valid.
    assert _rm()._validate_reasoning(
        {"kimi-k2.6": "off"}, None) == {"kimi-k2.6": "off"}


def test_validate_reasoning_rejects_alias_outside_the_run():
    with pytest.raises(ValueError, match="doesn't use"):
        _rm()._validate_reasoning(
            {"glm-5.2": "high"}, ["kimi-k2.6"])


def test_validate_reasoning_rejects_unknown_alias():
    with pytest.raises(ValueError, match="unknown model 'gpt-9'"):
        _rm()._validate_reasoning({"gpt-9": "max"}, ["gpt-9"])


def test_validate_reasoning_rejects_effort_the_model_lacks():
    # Foundry's enum is none/minimal/low/medium/high (probed direct
    # 2026-08-08) — "max" does not exist on this surface at all.
    with pytest.raises(ValueError, match="not 'max'"):
        _rm()._validate_reasoning({"kimi-k2.6": "max"}, ["kimi-k2.6"])
    # Kimi/GLM reject "minimal"; only the DeepSeek deployments take it.
    with pytest.raises(ValueError, match="not 'minimal'"):
        _rm()._validate_reasoning({"glm-5.2": "minimal"}, ["glm-5.2"])


def test_validate_reasoning_rejects_off_for_always_thinking_models(monkeypatch):
    # No CURRENT fleet model is always-thinking (K3 accepted "none" in the
    # 2026-08-08 probe), but the guard stays for future ones — exercise it
    # with a synthetic registry entry.
    from app.core.config import get_settings
    from app.core.models import ModelOption
    settings = get_settings()
    fake = ModelOption(alias="always-thinks", label="t", price_in_per_mtok=1,
                       price_out_per_mtok=2, reasoning_efforts=["low"],
                       supports_thinking_off=False)
    monkeypatch.setattr(settings, "available_models",
                        [*settings.available_models, fake])
    with pytest.raises(ValueError, match="always thinks"):
        _rm()._validate_reasoning({"always-thinks": "off"}, ["always-thinks"])
    # ...but its efforts are fine.
    assert _rm()._validate_reasoning(
        {"always-thinks": "low"}, ["always-thinks"]) == {"always-thinks": "low"}


# ------------------------------------------------------------- lane_override

def test_lane_override_reads_artifacts():
    """Non-ask blueprints resolve (model, reasoning) uniformly: the single
    selection, or the default model's reasoning entry when nothing is picked."""
    from app.orchestrator.blueprints.base import BlueprintContext, lane_override

    ctx = BlueprintContext(run=None, artifacts={})
    assert lane_override(ctx) == (None, None)

    ctx = BlueprintContext(run=None, artifacts={
        "models": ["glm-5.2"], "reasoning": {"glm-5.2": "high"}})
    assert lane_override(ctx) == ("glm-5.2", "high")

    # Selection without a reasoning entry → provider default for that lane.
    ctx = BlueprintContext(run=None, artifacts={"models": ["glm-5.2"]})
    assert lane_override(ctx) == ("glm-5.2", None)

    # No selection, reasoning on the default model's row.
    ctx = BlueprintContext(run=None, artifacts={"reasoning": {"kimi-k2.6": "off"}})
    assert lane_override(ctx) == (None, "off")


# ------------------------------------------------------------------- API

def test_list_models_returns_registry(auth_client):
    client, _, _, _ = auth_client
    r = client.get("/models")
    assert r.status_code == 200
    body = r.json()
    aliases = [m["alias"] for m in body["models"]]
    assert body["default"] == "kimi-k2.6"
    assert aliases == ["kimi-k2.6", "kimi-k3", "glm-5.2",
                       "deepseek-v4-pro", "deepseek-v4-flash"]
    kimi = body["models"][0]
    assert kimi["price_in_per_mtok"] == 0.95
    assert kimi["cache_read_per_mtok"] == 0.16
    # Reasoning options ride the registry so the picker renders per-model rows.
    assert kimi["reasoning_efforts"] == ["low", "medium", "high"]
    assert kimi["supports_thinking_off"] is True
    k3 = body["models"][1]
    # K3 is the only "max" taker, and "none" works on it (probed 2026-08-08).
    assert k3["reasoning_efforts"] == ["low", "medium", "high", "max"]
    assert k3["supports_thinking_off"] is True
    assert body["models"][2]["reasoning_efforts"] == ["low", "medium", "high"]
    assert body["models"][4]["reasoning_efforts"] == [
        "minimal", "low", "medium", "high"]


def test_create_run_passes_models_through(auth_client):
    client, _, services, _ = auth_client
    r = client.post("/runs", json={
        "mode": "ask", "task": "compare the answers",
        "models": ["kimi-k2.6", "deepseek-v4-flash"],
    })
    assert r.status_code == 200
    assert services["run_manager"].created[0]["models"] == [
        "kimi-k2.6", "deepseek-v4-flash"]


def test_create_run_without_models_sends_none(auth_client):
    client, _, services, _ = auth_client
    r = client.post("/runs", json={"mode": "ask", "task": "plain question"})
    assert r.status_code == 200
    assert services["run_manager"].created[0]["models"] is None
    assert services["run_manager"].created[0]["reasoning"] is None


def test_create_run_passes_reasoning_through(auth_client):
    client, _, services, _ = auth_client
    r = client.post("/runs", json={
        "mode": "ask", "task": "compare with reasoning",
        "models": ["kimi-k2.6", "deepseek-v4-flash"],
        "reasoning": {"kimi-k2.6": "off", "deepseek-v4-flash": "high"},
    })
    assert r.status_code == 200
    assert services["run_manager"].created[0]["reasoning"] == {
        "kimi-k2.6": "off", "deepseek-v4-flash": "high"}


def test_create_run_rejects_bad_reasoning(auth_client):
    client, _, _, _ = auth_client
    r = client.post("/runs", json={
        "mode": "ask", "task": "bad reasoning",
        "models": ["glm-5.2"],
        "reasoning": {"glm-5.2": "minimal"},  # only the DeepSeeks take minimal
    })
    assert r.status_code == 422
