"""LLM client — ChatOpenAI via the LiteLLM gateway.

Build-on vs build-custom:
  BUILD ON: langchain-openai ChatOpenAI (OpenAI-compatible protocol).
  BUILD CUSTOM: gateway retry/backoff for 429/529, model capability registry
  (LiteLLM does not expose vision/reasoning flags reliably), fail-closed on
  unavailable model (never substitute), per-model suffix selection.
"""

from __future__ import annotations

import os
import random
import time
from typing import Any

from langchain_openai import ChatOpenAI

# --- Model capability registry (LiteLLM doesn't expose this reliably) ---

class ModelCapabilities:
    """What a gateway model alias can actually do. Fail-closed: unknown = no."""

    def __init__(self, *, vision: bool = False, reasoning: bool = False,
                 max_tokens: int = 8192, supports_tools: bool = True,
                 supports_streaming: bool = True) -> None:
        self.vision = vision
        self.reasoning = reasoning
        self.max_tokens = max_tokens
        self.supports_tools = supports_tools
        self.supports_streaming = supports_streaming


# Registry keyed by gateway model alias. Add entries as models are validated.
# Unknown aliases get the conservative default.
_CAPABILITY_REGISTRY: dict[str, ModelCapabilities] = {
    "kimi-foundry": ModelCapabilities(vision=False, reasoning=True, max_tokens=8192),
    "qwen-foundry": ModelCapabilities(vision=False, reasoning=False, max_tokens=8192),
}

_DEFAULT_CAPS = ModelCapabilities()


def get_capabilities(model: str) -> ModelCapabilities:
    return _CAPABILITY_REGISTRY.get(model, _DEFAULT_CAPS)


# --- Per-model prompt suffix selection ---

def suffix_for(model: str) -> str:
    """Return the model-specific suffix path (or empty if none)."""
    suffix_dir = os.path.join(os.path.dirname(__file__), "prompts", "suffixes")
    # kimi models get the kimi suffix; everything else gets default-open
    if "kimi" in model.lower():
        path = os.path.join(suffix_dir, "kimi.md")
        if os.path.exists(path):
            return path
    path = os.path.join(suffix_dir, "default-open.md")
    return path if os.path.exists(path) else ""


# --- Gateway retry/backoff ---

class GatewayRetryError(Exception):
    """Raised when the gateway is unreachable after all retries."""


def with_gateway_retry(fn: Any, *, max_retries: int = 3, base_delay: float = 1.0) -> Any:
    """Retry a callable on 429/529 with jittered exponential backoff.

    Respects Retry-After when present (passed via the exception's `headers`).
    Budget check happens BEFORE the call (fail-closed at the gateway) — the
    caller is responsible for that; this only handles transport retries.
    """

    last_exc: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            return fn()
        except Exception as exc:
            last_exc = exc
            retryable = _is_retryable(exc)
            if not retryable or attempt == max_retries:
                raise
            # Retry-After header (httpx raises HTTPStatusError with response)
            delay = base_delay * (2 ** attempt) + random.uniform(0, 0.5)
            retry_after = _retry_after(exc)
            if retry_after is not None:
                delay = max(delay, float(retry_after))
            time.sleep(delay)
    raise GatewayRetryError(f"gateway unreachable after {max_retries} retries") from last_exc


def _is_retryable(exc: Exception) -> bool:
    # httpx.HTTPStatusError with a 429 or 529 status, or a transport error
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if status in (429, 529, 500, 502, 503, 504):
        return True
    # openai APIStatusError also carries status_code
    status = getattr(exc, "status_code", None)
    if status in (429, 529, 500, 502, 503, 504):
        return True
    # Transport errors (connection, timeout) are retryable
    return isinstance(exc, (ConnectionError, TimeoutError))


def _retry_after(exc: Exception) -> float | None:
    headers = getattr(getattr(exc, "response", None), "headers", None)
    if headers is None:
        headers = getattr(exc, "headers", None)
    if headers is None:
        return None
    try:
        val = headers.get("retry-after") or headers.get("Retry-After")
        return float(val) if val else None
    except (TypeError, ValueError):
        return None


# --- Cost estimation (budget accounting) ---

# USD per 1M tokens (input, output). Conservative defaults; refine from gateway
# invoices. Unknown aliases get the conservative default (fail-closed posture).
_MODEL_PRICING: dict[str, tuple[float, float]] = {
    "kimi-foundry": (2.0, 6.0),
    "qwen-foundry": (1.0, 3.0),
}
_DEFAULT_PRICING = (2.0, 6.0)


def estimate_cost(model: str, usage: dict[str, Any] | None) -> float:
    """Estimate USD cost of one turn from its usage_metadata."""
    if not usage:
        return 0.0
    input_price, output_price = _MODEL_PRICING.get(model, _DEFAULT_PRICING)
    in_tok = float(usage.get("input_tokens", 0) or 0)
    out_tok = float(usage.get("output_tokens", 0) or 0)
    return (in_tok * input_price + out_tok * output_price) / 1_000_000


# --- LLM factory ---

def make_llm(
    model: str,
    *,
    streaming: bool = True,
    temperature: float = 0.0,
    tools: list | None = None,
) -> ChatOpenAI:
    """Build a ChatOpenAI pointed at the LiteLLM gateway.

    Fail-closed: if the gateway env is unset, raise (never substitute a model).
    """
    base_url = os.environ.get("LITELLM_BASE_URL")
    api_key = os.environ.get("LITELLM_API_KEY")
    if not base_url or not api_key:
        raise RuntimeError(
            "LITELLM_BASE_URL and LITELLM_API_KEY must be set (the LiteLLM "
            "gateway OpenAI-compatible endpoint + key). The engine never "
            "substitutes a model — fail-closed."
        )

    caps = get_capabilities(model)
    kwargs: dict[str, Any] = {
        "model": model,
        "base_url": base_url,
        "api_key": api_key,
        "streaming": streaming and caps.supports_streaming,
        "temperature": temperature,
        "timeout": 600,
        "max_retries": 0,  # we handle retries ourselves (with_gateway_retry)
    }
    llm = ChatOpenAI(**kwargs)
    if tools and caps.supports_tools:
        llm = llm.bind_tools(tools)
    return llm


__all__ = [
    "GatewayRetryError",
    "ModelCapabilities",
    "estimate_cost",
    "get_capabilities",
    "make_llm",
    "suffix_for",
    "with_gateway_retry",
]
