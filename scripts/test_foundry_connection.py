"""RG preflight — verify the Foundry credentials in infra/.env actually work.

Reads the env file ITSELF (the agent never sees the values) and exercises the
exact API shape the LiteLLM gateway proxies (OpenAI-compatible /chat/completions
on the Foundry Models endpoint, `api-key` header), for BOTH model slots the
gateway config knows about:

  kimi-foundry  <- FOUNDRY_MODEL    + FOUNDRY_API_BASE    + AZURE_AI_FOUNDRY_API_KEY
  qwen-foundry  <- FOUNDRY_MODEL_2  + FOUNDRY_API_BASE_2  + AZURE_AI_FOUNDRY_API_KEY_2

Per model: non-streaming completion (+ usage), streaming (time-to-first-delta),
tool calling, and json_schema structured output — the four capabilities the
DECISION_MATRIX gate (a/b/c/d) depends on. Secrets are never printed.

Usage:
  python3 scripts/test_foundry_connection.py [path-to-.env]   # default infra/.env
Exit: 0 = every discovered model passed all four probes.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ENV_PATH = Path(sys.argv[1] if len(sys.argv) > 1 else "infra/.env")
PROMPT = "Reply with exactly: connection ok"

TOOL_SCHEMA = [{
    "type": "function",
    "function": {
        "name": "file_read",
        "description": "Read a file from the workspace.",
        "parameters": {
            "type": "object",
            "properties": {"file_path": {"type": "string"}},
            "required": ["file_path"],
        },
    },
}]

PLAN_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "probe_plan",
        "schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "steps": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["title", "steps"],
            "additionalProperties": False,
        },
        "strict": True,
    },
}


def load_env(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        env[key.strip()] = value.strip().strip('"').strip("'")
    return env


def deployment_name(foundry_model: str) -> str:
    """FOUNDRY_MODEL carries the LiteLLM provider prefix (openai/Kimi-K2.6);
    the raw endpoint wants only the deployment name."""
    return foundry_model.split("/", 1)[1] if "/" in foundry_model else foundry_model


def chat(api_base: str, api_key: str, model: str, body: dict,
         timeout: int = 120) -> tuple[int, dict | str, float]:
    """POST {api_base}/chat/completions. Returns (http_status, payload, elapsed_s)."""
    url = api_base.rstrip("/") + "/chat/completions"
    payload = json.dumps({"model": model, **body}).encode()
    started = time.monotonic()
    for header in ("api-key", "Authorization"):
        req = urllib.request.Request(url, data=payload, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header(header, api_key if header == "api-key" else f"Bearer {api_key}")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode()
                return resp.status, json.loads(raw), time.monotonic() - started
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode()[:300]
            if exc.code in (401, 403) and header == "api-key":
                continue  # try the other auth header
            return exc.code, detail, time.monotonic() - started
        except Exception as exc:  # noqa: BLE001
            return 0, str(exc), time.monotonic() - started
    return 401, "authentication failed with both api-key and Bearer", time.monotonic() - started


def probe_nonstreaming(base: str, key: str, model: str) -> tuple[bool, str]:
    status, payload, elapsed = chat(base, key, model, {
        "messages": [{"role": "user", "content": PROMPT}],
        "max_tokens": 32, "stream": False,
        "stream_options": {"include_usage": True},
    })
    if status != 200 or not isinstance(payload, dict):
        return False, f"HTTP {status}: {payload}"
    usage = payload.get("usage") or {}
    has_usage = usage.get("prompt_tokens", 0) > 0 and usage.get("completion_tokens", 0) > 0
    note = f"{elapsed:.1f}s, usage={'present' if has_usage else 'MISSING (check c would fail)'}"
    return has_usage, note


def probe_streaming(base: str, key: str, model: str) -> tuple[bool, str]:
    url = base.rstrip("/") + "/chat/completions"
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": PROMPT}],
        "max_tokens": 32, "stream": True,
    }).encode()
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("api-key", key)
    started = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            for raw in resp:
                line = raw.decode().strip()
                if not line.startswith("data:") or line == "data: [DONE]":
                    continue
                delta = json.loads(line[5:]).get("choices", [{}])[0].get("delta", {})
                if delta.get("content"):
                    ttfd = time.monotonic() - started
                    ok = ttfd < 5.0
                    return ok, f"first delta {ttfd:.2f}s ({'<' if ok else '>='} 5s gate)"
            return False, "stream ended with no content delta"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def probe_tools(base: str, key: str, model: str) -> tuple[bool, str]:
    status, payload, _ = chat(base, key, model, {
        "messages": [{"role": "user", "content": "Read the file README.md using the tool."}],
        "tools": TOOL_SCHEMA, "tool_choice": "auto", "max_tokens": 128,
    })
    if status != 200 or not isinstance(payload, dict):
        return False, f"HTTP {status}: {payload}"
    calls = payload.get("choices", [{}])[0].get("message", {}).get("tool_calls") or []
    if not calls:
        return False, "no tool_calls in response"
    fn = calls[0].get("function", {})
    try:
        args = json.loads(fn.get("arguments", "{}"))
    except json.JSONDecodeError:
        return False, f"malformed tool arguments: {fn.get('arguments', '')[:80]}"
    return True, f"{fn.get('name')}({args})"


def probe_structured(base: str, key: str, model: str) -> tuple[bool, str]:
    status, payload, _ = chat(base, key, model, {
        "messages": [{"role": "user", "content":
                      "Plan a two-step probe. Respond with the JSON schema only."}],
        "response_format": PLAN_SCHEMA, "max_tokens": 256,
    })
    if status != 200 or not isinstance(payload, dict):
        return False, f"HTTP {status}: {str(payload)[:200]}"
    content = payload.get("choices", [{}])[0].get("message", {}).get("content", "")
    try:
        parsed = json.loads(content)
        assert isinstance(parsed["title"], str) and isinstance(parsed["steps"], list)
        return True, "schema-valid JSON returned"
    except Exception:  # noqa: BLE001
        return False, f"invalid JSON for schema: {content[:120]}"


def probe_model(alias: str, model: str, base: str, key: str) -> bool:
    print(f"\n=== {alias}: {model} @ {base} ===")
    if not model or not base or not key:
        print("  SKIP — slot not fully configured in the env file")
        return False
    all_ok = True
    for label, probe in (
        ("non-streaming + usage", probe_nonstreaming),
        ("streaming TTFD", probe_streaming),
        ("tool calling", probe_tools),
        ("json_schema output", probe_structured),
    ):
        ok, note = probe(base, key, deployment_name(model))
        all_ok = all_ok and ok
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}: {note}")
    return all_ok


def main() -> int:
    if not ENV_PATH.is_file():
        print(f"env file not found: {ENV_PATH}")
        return 2
    env = load_env(ENV_PATH)

    print(f"preflight against {ENV_PATH} (values never printed)")
    ok_kimi = probe_model("kimi-foundry",
                          env.get("FOUNDRY_MODEL", ""),
                          env.get("FOUNDRY_API_BASE", ""),
                          env.get("AZURE_AI_FOUNDRY_API_KEY", ""))
    ok_qwen = probe_model("qwen-foundry",
                          env.get("FOUNDRY_MODEL_2", ""),
                          env.get("FOUNDRY_API_BASE_2", ""),
                          env.get("AZURE_AI_FOUNDRY_API_KEY_2", ""))

    print("\n--- other RG prerequisites ---")
    for var in ("FETCH_PAT", "LITELLM_MASTER_KEY"):
        print(f"  {'set' if env.get(var) else 'MISSING'}  {var}")

    print("\n=== verdict ===")
    if ok_kimi and ok_qwen:
        print("both gate models reachable — RG recorded run can proceed")
        return 0
    if ok_kimi or ok_qwen:
        print("only ONE model slot works — the gate needs >=2; configure the other slot")
        return 1
    print("no working model slot — fix credentials before RG")
    return 1


if __name__ == "__main__":
    sys.exit(main())
