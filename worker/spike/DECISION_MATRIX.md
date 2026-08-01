# Day-1 Spike — Kimi-through-Gateway DECISION MATRIX

Generated: {{GENERATED_AT}}

Thresholds are registered here BEFORE the spike runs (plan §9 Phase 0 exit: "no
number-and-a-debate"). The matrix decides Kimi vs Claude-on-Foundry mechanically.

## Thresholds (fixed pre-run)

| # | Check | Pass threshold | Why |
|---|-------|----------------|-----|
| a | Tool-call fidelity (read/edit/bash round-trips) | ≥ 95% of tool calls complete `ok` across ask + soak | Plan mode + Development mode stand on this |
| b | Streaming | first delta < 5s, deltas continuous, zero dropped connections | Phone live-trace UX |
| c | Token accounting | usage present on every ResultMessage, input+output > 0 | Cost dashboards + budgets |
| d | Structured output fidelity | Plan 5/5 valid AND Notebook 5/5 valid against Pydantic contracts | Plan mode + lane notebooks depend on schema adherence through translation |
| e | 30-turn soak | ≥ 30 turns, `is_error=false`, last-10 tool success ≥ first-10 minus 0.10 | Drift shows up CUMULATIVELY, never on turn one |
| f | PROMPT CACHING (the deciding number) | `cache_read_input_tokens > 0` on run 2 with identical prefix | If caching doesn't survive translation, per-run cost/latency is several-fold worse and NO architecture change fixes it |
| g | interrupt+inject+resume | nudge visibly incorporated (canary word present), `state_lost=false` | Phase 3 "nudge a drifting lane" exit depends on mid-work steering |

## Decision rule (fixed pre-run)

- **ALL PASS** → Kimi on Foundry is the model route. Proceed to Phase 1.
- **f FAILS** → recompute per-run cost with uncached pricing; if several-fold worse, switch to Claude-on-Foundry NOW (gateway config change, not an architecture change).
- **a, d, or e FAILS** → Claude-on-Foundry. Translation fidelity is not negotiable.
- **b, c, or g FAILS, everything else passes** → document workaround (retry/poll fallback); revisit before Phase 2 gate.

## Results

```json
{{RESULTS_JSON}}
```

## XDR observation log (fill in during the run)

- Container build on the workstation: [ OK / flagged ]
- 30-turn soak inside container under Cortex XDR: [ observed behavior ]
- Endpoint-policy owner contacted (pre-Phase-0 gate 3): [ date / response ]
