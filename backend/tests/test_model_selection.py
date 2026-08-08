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
    assert _rm()._validate_models(["glm-foundry"], "development") == ["glm-foundry"]


def test_validate_models_dedupes_preserving_order():
    picked = _rm()._validate_models(
        ["glm-foundry", "kimi-foundry", "glm-foundry"], "ask")
    assert picked == ["glm-foundry", "kimi-foundry"]


def test_validate_models_unknown_alias_rejected():
    with pytest.raises(ValueError, match="unknown model 'gpt-9'"):
        _rm()._validate_models(["kimi-foundry", "gpt-9"], "ask")


def test_validate_models_multi_is_ask_only():
    with pytest.raises(ValueError, match="ask-mode only"):
        _rm()._validate_models(["kimi-foundry", "glm-foundry"], "development")


# ---------------------------------------------------------- _validate_reasoning

def test_validate_reasoning_none_means_provider_default():
    assert _rm()._validate_reasoning(None, None) is None
    assert _rm()._validate_reasoning({}, ["kimi-foundry"]) is None


def test_validate_reasoning_off_and_effort_accepted():
    rm = _rm()
    assert rm._validate_reasoning(
        {"glm-foundry": "max"}, ["glm-foundry"]) == {"glm-foundry": "max"}
    assert rm._validate_reasoning(
        {"kimi-foundry": "off"}, ["kimi-foundry"]) == {"kimi-foundry": "off"}


def test_validate_reasoning_targets_default_when_nothing_selected():
    # The picker shows the reasoning control on the default model's row even
    # with an empty selection — that entry is valid.
    assert _rm()._validate_reasoning(
        {"kimi-foundry": "off"}, None) == {"kimi-foundry": "off"}


def test_validate_reasoning_rejects_alias_outside_the_run():
    with pytest.raises(ValueError, match="doesn't use"):
        _rm()._validate_reasoning(
            {"glm-foundry": "max"}, ["kimi-foundry"])


def test_validate_reasoning_rejects_unknown_alias():
    with pytest.raises(ValueError, match="unknown model 'gpt-9'"):
        _rm()._validate_reasoning({"gpt-9": "max"}, ["gpt-9"])


def test_validate_reasoning_rejects_effort_the_model_lacks():
    # Kimi takes low/high/max (live-probed 2026-08-08) — anything else is a
    # client bug.
    with pytest.raises(ValueError, match="not 'xhigh'"):
        _rm()._validate_reasoning({"kimi-foundry": "xhigh"}, ["kimi-foundry"])
    # GLM maps low→high server-side; we offer the real two only.
    with pytest.raises(ValueError, match="not 'low'"):
        _rm()._validate_reasoning({"glm-foundry": "low"}, ["glm-foundry"])


# ------------------------------------------------------------- lane_override

def test_lane_override_reads_artifacts():
    """Non-ask blueprints resolve (model, reasoning) uniformly: the single
    selection, or the default model's reasoning entry when nothing is picked."""
    from app.orchestrator.blueprints.base import BlueprintContext, lane_override

    ctx = BlueprintContext(run=None, artifacts={})
    assert lane_override(ctx) == (None, None)

    ctx = BlueprintContext(run=None, artifacts={
        "models": ["glm-foundry"], "reasoning": {"glm-foundry": "max"}})
    assert lane_override(ctx) == ("glm-foundry", "max")

    # Selection without a reasoning entry → provider default for that lane.
    ctx = BlueprintContext(run=None, artifacts={"models": ["glm-foundry"]})
    assert lane_override(ctx) == ("glm-foundry", None)

    # No selection, reasoning on the default model's row.
    ctx = BlueprintContext(run=None, artifacts={"reasoning": {"kimi-foundry": "off"}})
    assert lane_override(ctx) == (None, "off")


# ------------------------------------------------------------------- API

def test_list_models_returns_registry(auth_client):
    client, _, _, _ = auth_client
    r = client.get("/models")
    assert r.status_code == 200
    body = r.json()
    aliases = [m["alias"] for m in body["models"]]
    assert body["default"] == "kimi-foundry"
    assert aliases == ["kimi-foundry", "glm-foundry",
                       "deepseek-pro-foundry", "deepseek-flash-foundry"]
    kimi = body["models"][0]
    assert kimi["price_in_per_mtok"] == 0.95
    assert kimi["cache_read_per_mtok"] == 0.16
    # Reasoning options ride the registry so the picker renders per-model rows.
    assert kimi["reasoning_efforts"] == ["low", "high", "max"]
    assert body["models"][1]["reasoning_efforts"] == ["high", "max"]
    assert body["models"][3]["reasoning_efforts"] == ["low", "high", "max"]


def test_create_run_passes_models_through(auth_client):
    client, _, services, _ = auth_client
    r = client.post("/runs", json={
        "mode": "ask", "task": "compare the answers",
        "models": ["kimi-foundry", "deepseek-flash-foundry"],
    })
    assert r.status_code == 200
    assert services["run_manager"].created[0]["models"] == [
        "kimi-foundry", "deepseek-flash-foundry"]


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
        "models": ["kimi-foundry", "deepseek-flash-foundry"],
        "reasoning": {"kimi-foundry": "off", "deepseek-flash-foundry": "max"},
    })
    assert r.status_code == 200
    assert services["run_manager"].created[0]["reasoning"] == {
        "kimi-foundry": "off", "deepseek-flash-foundry": "max"}


def test_create_run_rejects_bad_reasoning(auth_client):
    client, _, _, _ = auth_client
    r = client.post("/runs", json={
        "mode": "ask", "task": "bad reasoning",
        "models": ["glm-foundry"],
        "reasoning": {"glm-foundry": "low"},  # glm offers high/max only
    })
    assert r.status_code == 422
