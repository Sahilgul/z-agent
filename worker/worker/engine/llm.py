"""LLM client — ChatOpenAI via the LiteLLM gateway.

Build-on vs build-custom:
  BUILD ON: langchain-openai ChatOpenAI (OpenAI-compatible protocol).
  BUILD CUSTOM: gateway retry/backoff for 429/529, model capability registry
  (LiteLLM does not expose vision/reasoning flags reliably), fail-closed on
  unavailable model (never substitute), per-model suffix selection.
"""

from __future__ import annotations

import asyncio
import os
import random
import time
from typing import Any

from langchain_core.messages import AIMessageChunk
from langchain_core.outputs import ChatGenerationChunk
from langchain_openai import ChatOpenAI

# --- Model capability registry (LiteLLM doesn't expose this reliably) ---

class ModelCapabilities:
    """What a gateway model alias can actually do. Fail-closed: unknown = no."""

    def __init__(self, *, vision: bool = False, reasoning: bool = False,
                 max_tokens: int = 8192, supports_tools: bool = True,
                 supports_streaming: bool = True,
                 supports_temperature: bool = True,
                 reasoning_efforts: tuple[str, ...] = (),
                 supports_thinking_off: bool = True) -> None:
        self.vision = vision
        self.reasoning = reasoning
        self.max_tokens = max_tokens
        self.supports_tools = supports_tools
        self.supports_streaming = supports_streaming
        self.supports_temperature = supports_temperature
        # reasoning_effort values the deployment accepts (empty = the model
        # takes the thinking toggle only). Mirrors the backend registry —
        # make_llm validates against it and refuses unknown efforts.
        self.reasoning_efforts = reasoning_efforts
        # False = the deployment rejects reasoning_effort="none" (thinking
        # always on): "off" would 400 the call.
        self.supports_thinking_off = supports_thinking_off


# Registry keyed by gateway model alias. Add entries as models are validated.
# Unknown aliases get the conservative default.
_CAPABILITY_REGISTRY: dict[str, ModelCapabilities] = {
    # K2.6 was once fixed-parameter (400 on non-default temperature) — probed
    # through the gateway 2026-08-08: temperature 0.0–2.0 and top_p are all
    # accepted now, so the fleet takes temperature uniformly.
    "kimi-k2.6": ModelCapabilities(
        # Vision probed through the gateway 2026-08-08: correctly read a
        # two-color test image. GLM 400s on image input; both DeepSeeks
        # return 200 but HALLUCINATE (Pro: "teal/orange" on red/blue; Flash:
        # "no image visible") — blind, so images route through kimi-k2.6.
        vision=True, reasoning=True, max_tokens=200000,
        # Probed DIRECT against Foundry's /openai/v1 surface (2026-08-08):
        # the enum is none/minimal/low/medium/high — no "max", no "thinking"
        # object (both 400/422). K2.6 rejects "minimal"; "none" = thinking
        # off (reasoning_content comes back empty).
        reasoning_efforts=("low", "medium", "high"),
    ),
    "kimi-k3": ModelCapabilities(
        vision=True, reasoning=True, max_tokens=200000,
        # Probed direct (2026-08-08, FW-Kimi-K3 on its own endpoint): the
        # ONLY fleet model that takes "max"; rejects "minimal"; "none"
        # works (thinking off) despite first-party Moonshot docs claiming
        # K3 always thinks.
        reasoning_efforts=("low", "medium", "high", "max"),
    ),
    "qwen-foundry": ModelCapabilities(vision=False, reasoning=False, max_tokens=8192),
    # Compare fleet (Azure AI Foundry, same /openai/v1 surface as Kimi).
    # reasoning=True selects the ChatOpenAIReasoning subclass, which only ADDS
    # preservation of reasoning_content — the request payload is unchanged, so
    # it's the safe default for models whose thinking emission is unverified.
    # GLM: same enum as Kimi (no minimal), thinks by default. DeepSeek V4:
    # full enum INCLUDING minimal, and does NOT think by default — pass an
    # effort to get reasoning_content out of it.
    "glm-5.2": ModelCapabilities(
        vision=False, reasoning=True, max_tokens=200000,
        reasoning_efforts=("low", "medium", "high"),
    ),
    "deepseek-v4-pro": ModelCapabilities(
        vision=False, reasoning=True, max_tokens=200000,
        reasoning_efforts=("minimal", "low", "medium", "high"),
    ),
    "deepseek-v4-flash": ModelCapabilities(
        vision=False, reasoning=True, max_tokens=200000,
        reasoning_efforts=("minimal", "low", "medium", "high"),
    ),
}

_DEFAULT_CAPS = ModelCapabilities()


def get_capabilities(model: str) -> ModelCapabilities:
    return _CAPABILITY_REGISTRY.get(model, _DEFAULT_CAPS)


# --- ChatOpenAI subclass that preserves reasoning_content ---

class ChatOpenAIReasoning(ChatOpenAI):
    """ChatOpenAI variant that preserves ``reasoning_content`` from
    OpenAI-compatible reasoning models (Kimi-K2, DeepSeek-R1, Qwen-QwQ, …).

    Stock ``ChatOpenAI`` only targets the official OpenAI spec and silently
    drops the non-standard ``reasoning_content`` field that these models return
    in their chat-completions responses (langchain-ai/langchain#37960). This
    subclass captures it into ``additional_kwargs["reasoning_content"]`` on
    both the streaming and non-streaming conversion paths — the same convention
    ``ChatDeepSeek`` uses — so the engine can surface it as ``StepKind.THINKING``.

    The request side is unchanged: we send the standard OpenAI chat-completions
    payload (no ``thinking``/``reasoning_effort`` param), and reasoning models
    emit ``reasoning_content`` by default.
    """

    def _create_chat_result(
        self,
        response: Any,
        generation_info: dict | None = None,
    ) -> Any:
        rtn = super()._create_chat_result(response, generation_info)
        try:
            import openai as _openai  # local import — not all runtimes need it
        except ImportError:  # pragma: no cover
            return rtn
        if not isinstance(response, _openai.BaseModel):
            return rtn
        choices = getattr(response, "choices", None)
        if not choices:
            return rtn
        msg = choices[0].message
        # Primary: native reasoning_content (Kimi/DeepSeek/vLLM convention).
        if hasattr(msg, "reasoning_content") and msg.reasoning_content:
            rtn.generations[0].message.additional_kwargs["reasoning_content"] = (
                msg.reasoning_content
            )
        # Fallback: OpenRouter nests it under model_extra["reasoning"].
        elif hasattr(msg, "model_extra"):
            model_extra = msg.model_extra
            if isinstance(model_extra, dict) and (reasoning := model_extra.get("reasoning")):
                rtn.generations[0].message.additional_kwargs["reasoning_content"] = reasoning
        return rtn

    def _convert_chunk_to_generation_chunk(
        self,
        chunk: dict,
        default_chunk_class: type,
        base_generation_info: dict | None,
    ) -> ChatGenerationChunk | None:
        generation_chunk = super()._convert_chunk_to_generation_chunk(
            chunk, default_chunk_class, base_generation_info,
        )
        choices = chunk.get("choices") if isinstance(chunk, dict) else None
        if not choices or generation_chunk is None:
            return generation_chunk
        if not isinstance(generation_chunk.message, AIMessageChunk):
            return generation_chunk
        top = choices[0]
        delta = top.get("delta", {}) if isinstance(top, dict) else {}
        # Primary: native reasoning_content (Kimi/DeepSeek/vLLM convention).
        if (reasoning_content := delta.get("reasoning_content")) is not None:
            generation_chunk.message.additional_kwargs["reasoning_content"] = reasoning_content
        # Fallback: OpenRouter nests it under delta["reasoning"].
        elif (reasoning := delta.get("reasoning")) is not None:
            generation_chunk.message.additional_kwargs["reasoning_content"] = reasoning
        return generation_chunk


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
            if not retryable:
                raise
            if attempt == max_retries:
                raise GatewayRetryError(
                    f"gateway unreachable after {max_retries} retries") from exc
            # Retry-After header (httpx raises HTTPStatusError with response)
            delay = base_delay * (2 ** attempt) + random.uniform(0, 0.5)
            retry_after = _retry_after(exc)
            if retry_after is not None:
                delay = max(delay, float(retry_after))
            time.sleep(delay)
    raise GatewayRetryError(f"gateway unreachable after {max_retries} retries") from last_exc


async def with_gateway_retry_aiter(stream_factory: Any, *, max_retries: int = 2,
                                  base_delay: float = 1.0) -> Any:
    """Retry the stream START (construction + first chunk) on 429/503 (H-11).

    `with_gateway_retry` only wraps stream CONSTRUCTION (`llm.astream(...)`
    returns the iterator without raising), so a 429/503 raised on the FIRST
    iteration (stream start) was never retried — the turn failed. Here we
    retry construction AND the first chunk: once the first chunk arrives,
    partial deltas may have been emitted, so mid-stream failures are NOT
    retried (the turn fails, matching the existing design).
    """
    last_exc: Exception | None = None
    for attempt in range(max_retries + 1):
        stream = None
        try:
            # Construction is inside the try per the contract above: a factory
            # that raises a retryable error is retried like a first-chunk one.
            stream = stream_factory()
            iterator = stream.__aiter__()
            first = await iterator.__anext__()
        except StopAsyncIteration:
            return  # empty stream — nothing to retry
        except Exception as exc:
            # Close the abandoned stream before retrying — an unclosed
            # ChatOpenAI.astream generator holds an httpx connection hostage
            # until GC, which exhausts the pool under a 429/503 storm.
            if stream is not None:
                try:
                    await stream.aclose()
                except Exception:
                    pass
            last_exc = exc
            if not _is_retryable(exc):
                raise
            if attempt == max_retries:
                raise GatewayRetryError(
                    f"gateway unreachable after {max_retries} retries") from exc
            delay = base_delay * (2 ** attempt) + random.uniform(0, 0.5)
            retry_after = _retry_after(exc)
            if retry_after is not None:
                delay = max(delay, float(retry_after))
            await asyncio.sleep(delay)
            continue
        # First chunk obtained safely — yield it, then the rest (no retry).
        yield first
        async for chunk in iterator:
            yield chunk
        return
    if last_exc is not None:
        raise GatewayRetryError(
            f"gateway unreachable after {max_retries} retries") from last_exc


def _is_retryable(exc: Exception) -> bool:
    # httpx.HTTPStatusError with a 429 or 529 status, or a transport error
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if status in (429, 529, 500, 502, 503, 504):
        return True
    # openai APIStatusError also carries status_code
    status = getattr(exc, "status_code", None)
    if status in (429, 529, 500, 502, 503, 504):
        return True
    # Transport errors (connection, timeout) are retryable. This must cover
    # the SDK wrappers too: openai.APIConnectionError / APITimeoutError and
    # the underlying httpx.TransportError do NOT subclass ConnectionError /
    # TimeoutError, so the most common transient gateway faults previously
    # failed the turn on the first attempt without any retry.
    transport_types: tuple[type[BaseException], ...] = (ConnectionError, TimeoutError)
    try:
        import httpx
        transport_types += (httpx.TransportError,)
    except ImportError:  # pragma: no cover
        pass
    try:
        import openai
        transport_types += (openai.APIConnectionError, openai.APITimeoutError)
    except ImportError:  # pragma: no cover
        pass
    return isinstance(exc, transport_types)


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

# USD per 1M tokens (input, output). Fallback only — the backend injects the
# registry rate as MODEL_PRICE_*_PER_MTOK per lane (F4), so these values are
# hit only when a worker runs outside the sandbox env. Kept in sync with
# backend/app/core/models.py (Azure AI Foundry listings, 2026-08).
_MODEL_PRICING: dict[str, tuple[float, float]] = {
    "kimi-k2.6": (0.95, 4.00),
    # K3 seeded at the Fireworks/Moonshot first-party rate — NOT yet confirmed
    # against the Foundry listing; fix here + registry + config.yaml together.
    "kimi-k3": (3.30, 16.50),
    "glm-5.2": (1.54, 4.84),
    "deepseek-v4-pro": (1.74, 3.48),
    "deepseek-v4-flash": (0.19, 0.51),
    "qwen-foundry": (1.0, 3.0),
}
_DEFAULT_PRICING = (2.0, 6.0)


def _pricing_for(model: str) -> tuple[float, float]:
    """F4: the backend injects MODEL_PRICE_*_PER_MTOK so the local estimate
    tracks the gateway's real rates (reminder thresholds == real spend). The
    static table remains as the fallback when the env is absent."""
    env_in = os.environ.get("MODEL_PRICE_IN_PER_MTOK")
    env_out = os.environ.get("MODEL_PRICE_OUT_PER_MTOK")
    if env_in and env_out:
        try:
            return float(env_in), float(env_out)
        except ValueError:
            pass
    return _MODEL_PRICING.get(model, _DEFAULT_PRICING)


# Cached input tokens are billed at 10% of the input rate (the common
# provider convention for prompt-cache reads). Gateway-truth remains the
# readback (F3); this only keeps the LOCAL reminder estimate honest.
CACHE_READ_PRICE_FACTOR = 0.1


def estimate_cost(model: str, usage: dict[str, Any] | None) -> float:
    """Estimate USD cost of one turn from its usage_metadata."""
    if not usage:
        return 0.0
    input_price, output_price = _pricing_for(model)
    in_tok = float(usage.get("input_tokens", 0) or 0)
    out_tok = float(usage.get("output_tokens", 0) or 0)
    # K6: OpenAI-compatible usage often INCLUDES cached tokens inside
    # input_tokens. Without this, cached turns were billed at full freight
    # and the 50%/80% budget reminders drifted far ahead of real spend.
    details = usage.get("input_token_details") or {}
    cached = float(details.get("cache_read", 0) or
                   usage.get("cache_read_input_tokens", 0) or 0)
    cached = min(cached, in_tok)
    uncached = in_tok - cached
    return (uncached * input_price
            + cached * input_price * CACHE_READ_PRICE_FACTOR
            + out_tok * output_price) / 1_000_000


# --- LLM factory ---

def make_llm(
    model: str,
    *,
    streaming: bool = True,
    # 0.1, not 0.0: near-deterministic for agent work, but a hair of
    # sampling avoids the degenerate repetition loops some deployments
    # fall into at exactly-zero temperature.
    temperature: float = 0.1,
    tools: list | None = None,
    reasoning: str | None = None,
) -> ChatOpenAI:
    """Build a ChatOpenAI pointed at the LiteLLM gateway.

    Fail-closed: if the gateway env is unset, raise (never substitute a model).

    ``reasoning`` is the composer's per-lane choice: "off" disables thinking,
    an effort string enables thinking at that effort, and None sends NO
    override — the request stays byte-identical to pre-feature traffic
    (provider default). On the wire it's ``reasoning_effort`` with the enum
    Foundry's serving layer validates (none/minimal/low/medium/high, plus
    "max" on K3 only); "off" maps to "none". There is NO "thinking" object
    on this surface (400 unrecognized_request_argument).

    Why the double-nested extra_body (verified against a local LiteLLM
    1.95.0 proxy + live Foundry, 2026-08-08): LiteLLM only forwards
    top-level ``reasoning_effort`` for model names its o-series/GPT-5
    transforms recognize — for every other openai/ route the base transform
    lacks the param and drop_params silently DELETES it (thinking stays on,
    DeepSeek never thinks). A nested ``extra_body`` object in the request is
    the proxy's sanctioned passthrough: its contents merge into the provider
    call unfiltered. But the OpenAI SDK FLATTENS ChatOpenAI's extra_body one
    level onto the wire — so we nest twice: the outer layer is flattened by
    the SDK, the inner arrives literal, and LiteLLM forwards its contents.
    """
    base_url = os.environ.get("LITELLM_BASE_URL")
    api_key = os.environ.get("LITELLM_API_KEY")
    if not base_url or not api_key:
        raise RuntimeError(
            "LITELLM_BASE_URL and LITELLM_API_KEY must be set (the LiteLLM "
            "gateway OpenAI-compatible endpoint + key). The engine never "
            "substitutes a model — fail-closed."
        )
    # F7: normalize the gateway URL — callers variously inject
    # "http://gw:4000", "http://gw:4000/", or the full "/v1" form. ChatOpenAI
    # appends "/chat/completions" to whatever it gets, so a bare host misses
    # LiteLLM's OpenAI routes and a doubled "/v1/v1" 404s just the same.
    base_url = base_url.rstrip("/")
    if not base_url.endswith("/v1"):
        base_url += "/v1"

    caps = get_capabilities(model)
    kwargs: dict[str, Any] = {
        "model": model,
        "base_url": base_url,
        "api_key": api_key,
        "streaming": streaming and caps.supports_streaming,
        "timeout": 600,
        "max_retries": 0,  # we handle retries ourselves (with_gateway_retry)
        # A custom base_url disables langchain-openai's stream_usage default
        # (it only auto-enables against api.openai.com), so the stream would
        # carry no usage chunk and usage_metadata would arrive empty — the
        # turn-metrics footer would show timings with null token counts.
        # Verified: the gateway forwards include_usage to the provider, and
        # drop_params shields any route that doesn't support it.
        "stream_usage": True,
    }
    # Fixed-param deployments (none in the current fleet — probed 2026-08-08)
    # 400 on non-default temperature: omit the param entirely for those
    # instead of relying on the gateway's drop_params.
    if caps.supports_temperature:
        kwargs["temperature"] = temperature
    # max_tokens is deliberately NOT sent: omitting it lets the deployment
    # generate to its own output limit, while explicitly asking for the
    # declared 200k ceiling would 400 on any deployment whose true cap is
    # lower. caps.max_tokens is bookkeeping for future budget math only.
    if reasoning == "off":
        if not caps.supports_thinking_off:
            raise RuntimeError(
                f"model '{model}' always thinks — 'off' would 400 "
                "(fail-closed, no silent clamp)")
        kwargs["extra_body"] = {"extra_body": {"reasoning_effort": "none"}}
    elif reasoning:
        if reasoning not in caps.reasoning_efforts:
            raise RuntimeError(
                f"model '{model}' takes reasoning {sorted(caps.reasoning_efforts)} "
                f"or 'off' — not '{reasoning}' (fail-closed, no silent clamp)")
        kwargs["extra_body"] = {"extra_body": {"reasoning_effort": reasoning}}
    # Reasoning models (Kimi-K2, DeepSeek-R1, …) return a non-standard
    # ``reasoning_content`` field that stock ChatOpenAI silently drops
    # (langchain-ai/langchain#37960). Use the subclass that preserves it so
    # the engine can surface thinking tokens as StepKind.THINKING.
    cls = ChatOpenAIReasoning if caps.reasoning else ChatOpenAI
    llm = cls(**kwargs)
    if tools and caps.supports_tools:
        llm = llm.bind_tools(tools)
    return llm


__all__ = [
    "ChatOpenAIReasoning",
    "GatewayRetryError",
    "ModelCapabilities",
    "estimate_cost",
    "get_capabilities",
    "make_llm",
    "with_gateway_retry",
    "with_gateway_retry_aiter",
]
