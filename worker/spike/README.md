# Phase 0 Spike — DECISION MATRIX Gate

The gate that must pass before Phase 2 (engine core) starts. Runs the full a–g
matrix against ≥2 open models via the LiteLLM gateway, using a custom LangGraph
agent loop (not the Claude Agent SDK).

## What it validates

| # | Check | Pass threshold |
|---|-------|----------------|
| a | Tool-call fidelity (read/edit/bash round-trips) | ≥ 95% of tool calls `ok` |
| b | Streaming | first delta < 5s, continuous, zero dropped |
| c | Token accounting | usage present, input+output > 0 |
| d | Structured output | Plan 5/5 + Notebook 5/5 vs Pydantic |
| e | 30-turn soak | ≥ 30 turns, `is_error=false`, last-10 ≥ first-10 − 0.10 |
| f | Prompt caching | `cache_read_input_tokens > 0` on run 2 |
| g | interrupt+inject+resume | canary present, `state_lost=false` |

**The gate passes ONLY if ≥2 models pass ALL of a–g.** Thresholds are fixed
pre-run in `DECISION_MATRIX.md` ("no number-and-a-debate").

## Fallback ladder (reconciled R31)

1. a/d/e fail on model 1 → run the matrix on model 2 (alt open model).
2. Model 2 also fails → the common factor is the translation layer →
   **hand-rolled loop over raw OpenAI completions** (no langchain-openai);
   every other plan decision stands.
3. b/c/g fail, everything else passes → document a workaround. A **g** failure
   triggers a LangGraph interrupt/resume investigation BEFORE Phase 2 — the
   entire approval architecture stands on it.

## Running

```bash
# Inside the worker container, or on the host for a pre-Docker smoke:
export LITELLM_BASE_URL=http://gateway:4000/v1
export LITELLM_API_KEY=<gateway master or virtual key>
export SPIKE_MODELS=kimi-foundry,qwen-foundry
export GOLDEN_DIR=/golden/repos

python -m spike.matrix all --golden /golden/repos
# → writes spike-results/DECISION_MATRIX.md + spike-results/results.json
# exit 0 = gate passed, exit 1 = gate failed
```

Individual checks:

```bash
python -m spike.matrix ask        --golden /golden/repos --repo ServerApp --branch main
python -m spike.matrix structured
python -m spike.matrix soak       --golden /golden/repos
python -m spike.matrix interrupt  --golden /golden/repos
python -m spike.matrix cache
```

## Files

- `matrix.py` — CLI runner; aggregates results, evaluates the gate, renders `DECISION_MATRIX.md`.
- `agent_loop.py` — the hand-rolled ReAct loop (assistant → tools → assistant) over the gateway. NOT the full LangGraph StateGraph (that's Phase 2).
- `interrupt_graph.py` — check (g): a minimal LangGraph StateGraph using `interrupt()` / `Command(resume=)`, the mechanism Phase 3 approvals depend on.
- `checks.py` — the seven checks (a–g).
- `spike_tools.py` — minimal `file_read` / `file_edit` / `bash` tools (NOT the production tool suite).
- `DECISION_MATRIX.md` — thresholds + decision rule + results template.
- `tracer.py` — LEGACY Claude-Agent-SDK tracer (superseded; kept for history).

## Models

Gateway model aliases live in `infra/litellm/config.yaml`. The gate needs ≥2
open-model routes (`kimi-foundry` + `qwen-foundry`). The Claude-on-Foundry
fallback is removed per R2/R31 — the reconciled ladder is alt-open-model →
hand-rolled loop, never back to Claude.
