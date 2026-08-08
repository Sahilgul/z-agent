"""Phase 0 spike — DECISION MATRIX runner (CLI entrypoint).

Runs the full a–g matrix against 2–3 open models via the LiteLLM gateway and
renders results into DECISION_MATRIX.md. The gate passes ONLY if ≥2 models
pass ALL checks; thresholds are fixed pre-run in DECISION_MATRIX.md.

Usage:
  python -m spike.matrix all       --golden /golden/repos --models kimi-foundry,qwen-foundry
  python -m spike.matrix ask        --golden /golden/repos --repo ServerApp --branch main
  python -m spike.matrix structured
  python -m spike.matrix soak      --golden /golden/repos
  python -m spike.matrix interrupt --golden /golden/repos
  python -m spike.matrix cache

Env:
  LITELLM_BASE_URL   gateway OpenAI-compatible endpoint (e.g. http://gateway:4000/v1)
  LITELLM_API_KEY    gateway master or virtual key
  SPIKE_RESULTS_DIR  default ./spike-results
  SPIKE_MODELS       comma-separated gateway model aliases (default kimi-foundry)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from spike.checks import (
    check_ask,
    check_cache,
    check_soak,
    check_structured,
)
from spike.interrupt_graph import run_interrupt_check

RESULTS_DIR = Path(os.environ.get("SPIKE_RESULTS_DIR", "./spike-results"))
DEFAULT_MODELS = [m.strip() for m in os.environ.get("SPIKE_MODELS", "kimi-foundry").split(",") if m.strip()]


async def run_matrix(command: str, golden: Path, repo: str, branch: str, models: list[str]) -> dict[str, Any]:
    """Run one check (or 'all') across all models. Returns {model: {check: result}}."""
    results: dict[str, Any] = {}
    for model in models:
        results[model] = {}
        print(f"[spike] === model: {model} ===")
        try:
            if command in ("ask", "all"):
                results[model]["ask"] = await check_ask(golden, repo, branch, model, RESULTS_DIR)
            if command in ("structured", "all"):
                results[model]["structured"] = await check_structured(model)
            if command in ("soak", "all"):
                results[model]["soak"] = await check_soak(golden, model, RESULTS_DIR)
            if command in ("interrupt", "all"):
                results[model]["interrupt"] = await run_interrupt_check(golden, model, RESULTS_DIR)
            if command in ("cache", "all"):
                results[model]["cache"] = await check_cache(model)
        except Exception as exc:
            results[model]["error"] = str(exc)
            print(f"[spike] model {model} failed: {exc}")
    return results


# ----------------------------------------------------------- threshold eval

THRESHOLDS = {
    "a": ("tool_call_success_rate", lambda v: v is not None and v >= 0.95),
    "b": ("first_delta_latency_s", lambda v: v is not None and v < 5.0),
    "c": ("usage", lambda v: v is not None and v.get("input_tokens", 0) > 0 and v.get("output_tokens", 0) > 0),
    "d": ("schema_validity_rate", lambda v: v is not None and v >= 1.0),
    "e": ("soak_turns_met", lambda v: v is True),
    "f": ("caching_survives", lambda v: v is True),
    "g": ("interrupt_resume_works", lambda v: v is True),
}


def evaluate_model(model_results: dict[str, Any]) -> dict[str, bool]:
    """Map one model's results to a–g pass/fail."""
    verdicts: dict[str, bool] = {}
    ask = model_results.get("ask", {})
    structured = model_results.get("structured", {})
    soak = model_results.get("soak", {})
    cache = model_results.get("cache", {})
    interrupt = model_results.get("interrupt", {})

    verdicts["a"] = THRESHOLDS["a"][1](ask.get("tool_call_success_rate"))
    verdicts["b"] = THRESHOLDS["b"][1](ask.get("first_delta_latency_s"))
    verdicts["c"] = THRESHOLDS["c"][1](ask.get("usage"))
    verdicts["d"] = THRESHOLDS["d"][1](structured.get("schema_validity_rate"))
    verdicts["e"] = THRESHOLDS["e"][1](soak.get("soak_turns_met"))
    verdicts["f"] = THRESHOLDS["f"][1](cache.get("caching_survives"))
    verdicts["g"] = THRESHOLDS["g"][1](interrupt.get("interrupt_resume_works"))
    return verdicts


def evaluate_gate(all_results: dict[str, Any]) -> dict[str, Any]:
    """Apply the decision rule: gate passes iff ≥2 models pass ALL of a–g."""
    model_verdicts = {m: evaluate_model(r) for m, r in all_results.items() if "error" not in r}
    passing_models = [m for m, v in model_verdicts.items() if all(v.values())]
    return {
        "model_verdicts": model_verdicts,
        "passing_models": passing_models,
        "gate_passed": len(passing_models) >= 2,
    }


# ----------------------------------------------------------- DECISION_MATRIX render

TEMPLATE_PATH = Path(__file__).parent / "DECISION_MATRIX.md"


def render_matrix(all_results: dict[str, Any], gate: dict[str, Any]) -> str:
    """Render results + verdicts into the DECISION_MATRIX.md template."""
    rendered = TEMPLATE_PATH.read_text()
    rendered = rendered.replace("{{GENERATED_AT}}", datetime.now(UTC).isoformat())
    payload = {
        "results": all_results,
        "gate": gate,
        "models_tested": list(all_results.keys()),
    }
    rendered = rendered.replace("{{RESULTS_JSON}}", json.dumps(payload, indent=2, default=str))

    # Append a human-readable verdict table.
    lines = ["\n## Verdicts\n"]
    lines.append("| Model | a | b | c | d | e | f | g | ALL PASS |")
    lines.append("|-------|---|---|---|---|---|---|---|----------|")
    for model, verdicts in gate["model_verdicts"].items():
        cells = ["PASS" if verdicts[k] else "FAIL" for k in ("a", "b", "c", "d", "e", "f", "g")]
        all_pass = "PASS" if all(verdicts.values()) else "FAIL"
        lines.append(f"| {model} | " + " | ".join(cells) + f" | {all_pass} |")
    lines.append("")
    # M-23: render None (not evaluated, partial run) distinctly from False.
    _gp = gate['gate_passed']
    gate_label = "N/A (not evaluated)" if _gp is None else str(_gp)
    lines.append(f"**Gate passed:** {gate_label}")
    lines.append(f"**Passing models:** {', '.join(gate['passing_models']) or '(none)'}")
    rendered += "\n".join(lines) + "\n"
    return rendered


def write_matrix(all_results: dict[str, Any], gate: dict[str, Any]) -> Path:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    (RESULTS_DIR / "results.json").write_text(json.dumps({"results": all_results, "gate": gate}, indent=2, default=str))
    out = RESULTS_DIR / "DECISION_MATRIX.md"
    out.write_text(render_matrix(all_results, gate))
    print(f"[spike] decision matrix -> {out}")
    # M-23: print None distinctly from a failed gate.
    _gp = gate['gate_passed']
    _gp_label = "n/a (partial run)" if _gp is None else str(_gp)
    print(f"[spike] gate passed: {_gp_label} (passing models: {', '.join(gate['passing_models']) or 'none'})")
    return out


# ----------------------------------------------------------- CLI

async def main() -> int:
    parser = argparse.ArgumentParser(prog="matrix")
    parser.add_argument("command", choices=["ask", "structured", "soak", "interrupt", "cache", "all"])
    parser.add_argument("--golden", default=os.environ.get("GOLDEN_DIR", "/golden/repos"))
    parser.add_argument("--repo", default="ServerApp")
    parser.add_argument("--branch", default="main")
    parser.add_argument("--models", default=",".join(DEFAULT_MODELS), help="comma-separated gateway model aliases")
    args = parser.parse_args()

    for var in ("LITELLM_BASE_URL", "LITELLM_API_KEY"):
        if not os.environ.get(var):
            print(f"[spike] missing env {var} (gateway OpenAI-compatible endpoint + key)", file=sys.stderr)
            return 2

    models = [m.strip() for m in args.models.split(",") if m.strip()]
    if not models:
        print("[spike] no models selected (set SPIKE_MODELS or pass --models)", file=sys.stderr)
        return 2

    golden = Path(args.golden)
    all_results = await run_matrix(args.command, golden, args.repo, args.branch, models)
    gate = evaluate_gate(all_results) if args.command == "all" else {
        "model_verdicts": {},
        "passing_models": [],
        # M-23: a partial run never evaluates the gate, but wrote
        # `gate_passed: False` — indistinguishable from a gate that RAN and
        # FAILED. Use None ("not evaluated") so the matrix doesn't record a
        # false negative.
        "gate_passed": None,
        "note": "partial run — gate only evaluated on 'all'",
    }
    write_matrix(all_results, gate)
    return 0 if (args.command != "all" or gate["gate_passed"]) else 1


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    sys.exit(asyncio.run(main()))
