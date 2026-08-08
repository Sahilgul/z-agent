#!/usr/bin/env python3
"""Real-inference matrix: every fleet model x every reasoning mode.

Two targets:

  DIRECT (default) — hits the Foundry /openai/v1 surface with
  AZURE_AI_FOUNDRY_API_KEY. Works from anywhere; tests the provider.

      python3 scripts/test_all_llm.py

  GATEWAY (--gateway) — hits LiteLLM with the master key using the fleet
  aliases. This is the exact production path (routing, drop behavior,
  virtual-key surface). Run it INSIDE the gateway container, where the env
  is already set and localhost:4000 is reachable:

      cd ~/z-agent/infra/vm
      docker compose exec -T gateway python - --gateway < ../../scripts/test_all_llm.py

Env is read via dotenv.load_dotenv from infra/vm/.env (override with
--env PATH or ENV_FILE). If python-dotenv isn't installed, a minimal
fallback parser handles simple KEY=value files; if the vars are already in
the process environment (inside the container), no file is needed at all.

Keep the MODELS matrix in sync with backend/app/core/models.py — the
registry is the source of truth for efforts and thinking-off support.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

# --------------------------------------------------------------- env loading

def load_env(path: str) -> None:
    try:
        from dotenv import load_dotenv
        load_dotenv(path)
        return
    except ImportError:
        pass
    # Fallback: minimal KEY=value parser (no interpolation, no quotes magic)
    # so the script still runs where python-dotenv isn't installed.
    try:
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    except FileNotFoundError:
        pass  # inside the gateway container the vars are already set


# --------------------------------------------------------------- the matrix

# alias -> (deployment env var, reasoning efforts, thinking-off allowed)
MODELS: dict[str, tuple[str, list[str], bool]] = {
    "kimi-foundry":           ("FOUNDRY_MODEL",         ["low", "high", "max"], True),
    "kimi3-foundry":          ("FOUNDRY_MODEL_K3",      ["low", "high", "max"], False),
    "glm-foundry":            ("FOUNDRY_MODEL_GLM",     ["high", "max"],        True),
    "deepseek-pro-foundry":   ("FOUNDRY_MODEL_DS_PRO",  ["low", "high", "max"], True),
    "deepseek-flash-foundry": ("FOUNDRY_MODEL_DS_FLASH", ["low", "high", "max"], True),
}

PROMPT = ("Is 9.11 greater than 9.8? Think step by step, "
          "then give the final answer in one sentence.")
MAX_TOKENS = 4096  # bounded cost per cell; the matrix is ~75 calls


# ------------------------------------------------------------------ calling

def call(base: str, key: str, model: str, extra: dict | None,
         timeout: int) -> dict:
    body: dict = {"model": model, "max_tokens": MAX_TOKENS,
                  "messages": [{"role": "user", "content": PROMPT}]}
    if extra:
        body.update(extra)  # exactly what the worker's extra_body puts on the wire
    req = urllib.request.Request(
        base.rstrip("/") + "/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {key}",
                 "Content-Type": "application/json"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return {"ok": False, "status": exc.code,
                "error": exc.read().decode()[:160], "secs": time.time() - t0}
    except Exception as exc:  # timeout, connection reset, ...
        return {"ok": False, "status": "ERR",
                "error": f"{type(exc).__name__}: {exc}", "secs": time.time() - t0}

    choice = data["choices"][0]
    msg = choice["message"]
    usage = data.get("usage") or {}
    return {
        "ok": True, "status": 200, "secs": time.time() - t0,
        "reasoning_chars": len(msg.get("reasoning_content") or ""),
        "answer_chars": len(msg.get("content") or ""),
        "finish": choice.get("finish_reason"),
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
        "reasoning_tokens": (usage.get("completion_tokens_details") or {})
                            .get("reasoning_tokens"),
        "cached_tokens": (usage.get("prompt_tokens_details") or {})
                         .get("cached_tokens"),
    }


def fmt(r: dict) -> str:
    if not r["ok"]:
        return f"{r['status']} in {r['secs']:5.1f}s  {r['error']}"
    return (f"200 in {r['secs']:5.1f}s  finish={r['finish']:<6} "
            f"reasoning={r['reasoning_chars']:5d}ch answer={r['answer_chars']:4d}ch | "
            f"tokens p={r['prompt_tokens']} c={r['completion_tokens']} "
            f"r={r['reasoning_tokens']} cached={r['cached_tokens']}")


# --------------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--gateway", metavar="URL",
                    help="test through LiteLLM (aliases) instead of direct Foundry")
    ap.add_argument("--env", default=os.environ.get("ENV_FILE", "infra/vm/.env"),
                    help="path to the .env file (default: infra/vm/.env)")
    ap.add_argument("--models", nargs="*", choices=[*MODELS, "all"], default=["all"],
                    help="subset of aliases to test")
    ap.add_argument("--timeout", type=int, default=300)
    args = ap.parse_args()

    load_env(args.env)

    if args.gateway:
        base, key = args.gateway.rstrip("/") + "/v1", os.environ.get("LITELLM_MASTER_KEY", "")
        name_of = {alias: alias for alias in MODELS}  # gateway routes by alias
        if not key:
            print("LITELLM_MASTER_KEY is not set (env file missing?)", file=sys.stderr)
            return 2
    else:
        base = os.environ.get("FOUNDRY_API_BASE", "")
        key = os.environ.get("AZURE_AI_FOUNDRY_API_KEY", "")
        # "openai/Kimi-K2.6" -> "Kimi-K2.6" (LiteLLM provider prefix)
        name_of = {a: os.environ.get(env, "").split("/", 1)[-1]
                   for a, (env, _, _) in MODELS.items()}
        if not base or not key:
            print("FOUNDRY_API_BASE / AZURE_AI_FOUNDRY_API_KEY not set "
                  f"(looked in {args.env})", file=sys.stderr)
            return 2

    selected = list(MODELS) if args.models == ["all"] else args.models
    failures: list[str] = []

    for alias in selected:
        _, efforts, off_allowed = MODELS[alias]
        model = name_of[alias]
        if not model:
            print(f"\n=== {alias} === SKIPPED (deployment env var unset)")
            failures.append(f"{alias}: no deployment name")
            continue
        rows: list[tuple[str, dict | None, bool]] = [("default", None, False)]
        # "off" is always probed: for always-thinking models a 400 CONFIRMS
        # the registry guard (expected=True), a 200 means the flag can relax.
        rows.append(("off", {"thinking": {"type": "disabled"}}, not off_allowed))
        rows += [(e, {"thinking": {"type": "enabled"}, "reasoning_effort": e}, False)
                 for e in efforts]

        print(f"\n=== {alias}  ({model}) ===")
        for label, extra, expect_400 in rows:
            r = call(base, key, model, extra, args.timeout)
            line = fmt(r)
            if expect_400 and not r["ok"] and r["status"] == 400:
                line += "   <- expected: always-thinking model, guard confirmed"
            elif expect_400 and r["ok"]:
                line += "   <- UNEXPECTED 200: Foundry tolerates off here — flag can relax"
            elif not r["ok"]:
                failures.append(f"{alias}/{label}: {r['status']}")
            print(f"  {label:<6}: {line}")

    print("\n" + ("=" * 60))
    if failures:
        print("FAILURES:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("all cells passed (expected-400 cells included)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
