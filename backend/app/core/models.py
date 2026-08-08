"""Fleet model registry — the user-selectable LLM routes behind the gateway.

One source for: the composer dropdown (GET /models), create_run validation,
per-thread virtual-key scoping (thread_manager.spawn), and per-model worker
pricing env (sandbox thread_env). Aliases MUST match infra/litellm/config.yaml
``model_name`` entries — the gateway 404s anything else, and the engine never
substitutes a model (fail-closed).
"""

from __future__ import annotations

from pydantic import BaseModel


class ModelOption(BaseModel):
    alias: str                    # LiteLLM route name (config.yaml model_name)
    label: str                    # composer / lane display name
    price_in_per_mtok: float      # USD per 1M standard input tokens
    price_out_per_mtok: float     # USD per 1M output tokens
    cache_read_per_mtok: float | None = None  # USD per 1M cached input
    # Selectable reasoning efforts (the thinking toggle is always available:
    # "off" disables thinking server-side). Empty = the model takes no
    # reasoning_effort value — on/off only. The worker maps the choice into
    # extra_body {"thinking": ..., "reasoning_effort": ...}.
    reasoning_efforts: list[str] = []


# Pricing confirmed against the Azure AI Foundry listings (2026-08). Keep in
# sync with the model_info blocks in infra/litellm/config.yaml — the gateway
# meters spend from its copy, the worker's budget reminders estimate from
# ours (injected as MODEL_PRICE_*_PER_MTOK container env).
# Reasoning efforts, verified against the LIVE Foundry deployments through the
# gateway (2026-08-08 probe): Kimi accepts low/high/max (provider default is
# max — dialing down is the cost/latency lever); GLM maps low/medium→high (so
# we offer the real two); V4 Pro rejects or maps low (unverified — high/max
# only until probed); V4 Flash has all three. All four default to thinking ON.
DEFAULT_MODELS: list[ModelOption] = [
    ModelOption(
        alias="kimi-foundry", label="kimi k2.6",
        price_in_per_mtok=0.95, price_out_per_mtok=4.00,
        cache_read_per_mtok=0.16,
        reasoning_efforts=["low", "high", "max"],
    ),
    ModelOption(
        alias="glm-foundry", label="glm 5.2",
        price_in_per_mtok=1.54, price_out_per_mtok=4.84,
        cache_read_per_mtok=0.15,
        reasoning_efforts=["high", "max"],
    ),
    ModelOption(
        alias="deepseek-pro-foundry", label="deepseek v4 pro",
        price_in_per_mtok=1.74, price_out_per_mtok=3.48,
        cache_read_per_mtok=0.145,
        reasoning_efforts=["low", "high", "max"],
    ),
    ModelOption(
        alias="deepseek-flash-foundry", label="deepseek v4 flash",
        price_in_per_mtok=0.19, price_out_per_mtok=0.51,
        cache_read_per_mtok=0.028,
        reasoning_efforts=["low", "high", "max"],
    ),
]
