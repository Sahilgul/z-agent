"""Wave 4 Stream B worker-side: pricing parity (F4) and gateway URL
normalization (F7)."""


import pytest

from worker.engine import llm

# ------------------------------------------------------------------- F4

def test_estimate_uses_backend_injected_pricing(monkeypatch):
    """The backend injects MODEL_PRICE_*_PER_MTOK so the worker's reminder
    thresholds track REAL gateway spend, not a hardcoded guess."""
    monkeypatch.setenv("MODEL_PRICE_IN_PER_MTOK", "1.0")
    monkeypatch.setenv("MODEL_PRICE_OUT_PER_MTOK", "3.0")
    cost = llm.estimate_cost("kimi-foundry",
                             {"input_tokens": 1_000_000, "output_tokens": 1_000_000})
    assert cost == pytest.approx(4.0)  # 1.0 + 3.0, not the static 2.0 + 6.0


def test_estimate_falls_back_without_env(monkeypatch):
    monkeypatch.delenv("MODEL_PRICE_IN_PER_MTOK", raising=False)
    monkeypatch.delenv("MODEL_PRICE_OUT_PER_MTOK", raising=False)
    cost = llm.estimate_cost("kimi-foundry",
                             {"input_tokens": 1_000_000, "output_tokens": 0})
    assert cost == pytest.approx(2.0)


# ------------------------------------------------------------------- F7

def test_base_url_normalized_to_v1(monkeypatch):
    """Whatever the backend injects (bare host, trailing slash, or a full
    /v1 path), ChatOpenAI must land on LiteLLM's OpenAI routes exactly once."""
    monkeypatch.setenv("LITELLM_API_KEY", "sk-test")
    captured = {}

    class _Spy:
        def __init__(self, **kw):
            captured.update(kw)

    monkeypatch.setattr(llm, "ChatOpenAI", _Spy)
    monkeypatch.setattr(llm, "ChatOpenAIReasoning", _Spy)
    for raw, want in [
        ("http://gw:4000", "http://gw:4000/v1"),
        ("http://gw:4000/", "http://gw:4000/v1"),
        ("http://gw:4000/v1", "http://gw:4000/v1"),
        ("http://gw:4000/v1/", "http://gw:4000/v1"),
    ]:
        monkeypatch.setenv("LITELLM_BASE_URL", raw)
        llm.make_llm("kimi-foundry")
        assert captured["base_url"] == want, raw


def test_missing_gateway_env_fails_closed(monkeypatch):
    monkeypatch.delenv("LITELLM_BASE_URL", raising=False)
    monkeypatch.delenv("LITELLM_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="fail-closed"):
        llm.make_llm("kimi-foundry")
