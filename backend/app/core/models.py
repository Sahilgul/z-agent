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
    # True = natively reads image inputs (probed live through the gateway
    # 2026-08-08: both Kimi deployments pass; GLM 400s; both DeepSeeks 200
    # but hallucinate — blind). The composer uses this to badge vision
    # models; the orchestrator routes images through a Kimi pre-pass when
    # the selected lane is blind.
    vision: bool = False
    # Selectable reasoning efforts (empty = the model takes no
    # reasoning_effort value — on/off only). The worker puts the choice on
    # the wire as reasoning_effort=<value>; "off" maps to "none".
    reasoning_efforts: list[str] = []
    # False = the deployment rejects reasoning_effort="none" (always
    # thinks): "off" would 400 the lane, so the picker hides it and
    # validation rejects it.
    supports_thinking_off: bool = True

DEFAULT_MODELS: list[ModelOption] = [
    ModelOption(
        alias="kimi-k2.6", label="Kimi K2.6",
        price_in_per_mtok=0.95, price_out_per_mtok=4.00,
        cache_read_per_mtok=0.16,
        vision=True,
        reasoning_efforts=["low", "medium", "high"],
    ),
    ModelOption(
        alias="kimi-k3", label="Kimi K3",
        price_in_per_mtok=3.30, price_out_per_mtok=16.50,
        cache_read_per_mtok=0.33,
        vision=True,
        reasoning_efforts=["low", "medium", "high", "max"],
    ),
    ModelOption(
        alias="glm-5.2", label="GLM 5.2",
        price_in_per_mtok=1.54, price_out_per_mtok=4.84,
        cache_read_per_mtok=0.15,
        reasoning_efforts=["low", "medium", "high"],
    ),
    ModelOption(
        alias="deepseek-v4-pro", label="DeepSeek V4 Pro",
        price_in_per_mtok=1.74, price_out_per_mtok=3.48,
        cache_read_per_mtok=0.145,
        reasoning_efforts=["minimal", "low", "medium", "high"],
    ),
    ModelOption(
        alias="deepseek-v4-flash", label="DeepSeek V4 Flash",
        price_in_per_mtok=0.19, price_out_per_mtok=0.51,
        cache_read_per_mtok=0.028,
        reasoning_efforts=["minimal", "low", "medium", "high"],
    ),
]
