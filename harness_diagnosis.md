# Worker Harness — Moat Audit

> 10 parallel agents · ~14,000 lines across `worker/`, `spike/`, `tests/` · evaluated against the 7 Collegium design-philosophy moats · Aug 8, 2026

| Metric | Value |
|---|---|
| Moats failing | **2** (§2, §3) |
| Moats at risk | **4** (§1, §5, §6, §7) |
| Moats passing | **1** (§4) |
| Evidence-backed findings | 60+ |

**⚠ The concurrency moats fail in code and are unpinned in tests.** §2 (per-repo write lock) and §3 (capacity reservations, 100-cap) are not enforced in the spawn path — and no executable test would catch a regression. Per the philosophy's own decision rule, anything touching `fanout.py` should be rejected until the reservation mechanism exists.

---

## Moat scorecard

| Moat | Code verdict | Test pin | Worst evidence |
|---|---|---|---|
| 1 · Harness never unrecoverable | **AT RISK** | PARTIAL | `graph.py:519-585` replay; `mutating.py:95` non-atomic writes |
| 2 · Conflict-free concurrency | **FAIL** | PARTIAL | `fanout.py:152-174` — no per-repo write lock in spawn path |
| 3 · Swarm bounded fan-out | **FAIL** | UNPINNED | `fanout.py:201-234` — reservation race open; cap 8 vs 100 |
| 4 · Token intelligence, no gate | **PASS** | PARTIAL | No classifier anywhere; golden chain missing from prompt |
| 5 · Cost via relevant reads | **AT RISK** | PARTIAL | `readonly.py:28` — dump is the zero-arg default; silent 16KB truncation |
| 6 · Max prompt caching | **AT RISK** | UNPINNED | `graph.py:182-197` — discovered tools mutate frozen prefix mid-run |
| 7 · Parallel by default | **AT RISK** | UNPINNED | `graph.py:519-585` — sequential tool-call loop, no gather |

---

## Top findings, ranked

1. **Capacity check-then-act race is wide open** — `fanout.py:201-207, 225-234`: `_veto` reads `live_count()`, then `register()` inserts; no lock, no reservation, durable row created async after the tool returns. N concurrent `spawn_swarm` calls all pass the cap check. The reservation mechanism §3 requires does not exist.

2. **Per-repo write lock absent from spawn path** — `fanout.py:152-174`: `_veto` never inspects `req.repo`. CollisionRadar is warn-only and its docstring falsely claims refusal lives in fanout.py. Two writable threads can land on one repo; §2's "enforced BEFORE spawn" is unenforced.

3. **Swarm cap drift: 8 vs 100** — `fanout.py:48`: `SWARM_MAX_SLICES = 8` functions as a global spawn cap, contradicting §3's stated current limit of 100. Tests pin the 8 (`test_fanout_contract.py:207`), not the 100.

4. **At-least-once tool replay on crash** — `graph.py:519-585` + `mutating.py:95,135`: checkpoints land at node boundaries; a crash mid-tools-node replays non-idempotent calls. `file_edit` is saved by its hash guard; approved shell commands (`git commit`, `npm install`) re-execute blindly. File writes are non-atomic: no tmp+rename, no journal.

5. **Sequential tool-call loop** — `graph.py:519-585`: independent model-emitted parallel calls await one at a time, no `asyncio.gather`. Undocumented serialization outside the capacity semaphore — a §7 violation candidate in the hottest loop.

6. **Full-file dump is the path of least resistance** — `readonly.py:28,38`: `file_read` defaults `limit=2000`, returning most files whole with zero extra effort; §5 demands the dump be the awkward path. `file_search`/`terminal_exec` truncate silently at 16KB with no marker (`readonly.py:89,206`); `file_glob`'s 500-cap is indistinguishable from exactly 500.

7. **Frozen-prefix leaks on three fronts** — `graph.py:182-197`: each `tool_search` discovery grows the bound tools array above the cache cut; `graph.py:84-94`: system prompt re-read from disk every turn, silently empty if missing; `system_prompt.md:88` promises a skills list that is never injected. No cache-hit metrics exist to see any of it (`metrics.py`).

8. **Kill not honored mid-turn; kill channel is lossy** — `runner.py:283-288`: `_stop` only polled in the idle loop; a kill during a turn (or 900s approval wait) burns budget until turn end, then SIGKILL skips the cascade drain. `control.py:31-74`: kill/nudge ride lossy pub/sub; a kill during reconnect backoff is silently dropped.

9. **Event sink failures swallowed** — `graph.py:1127-1130`: sink exceptions log-and-continue; the turn commits while the durable event record is silently dropped. Events are §1 survivors; this is a permanent replay/audit hole with no retry or dead-letter.

10. **Cross-run state leaks in process-global registries** — `graph.py:665-668`: `terminal_manager().completed_notifications()` drained with no thread scoping (the H-10 bug class, fixed for questions, not here). `fanout.py:131`: spawn registry counts across all runs in the process and is never evicted.

11. **SDK approval bridge always-allow hole** — `worker/approvals.py:50-55`: whitelists by tool name unconditionally; the engine gate re-verifies `is_destructive` on every always-allow hit (`engine/approvals.py:64-67`). Always-allowing `Bash` once permanently whitelists `git push --force`.

12. **Background-job orphans on hard death** — `background.py:91-98`: `start_new_session` detaches the process group; a SIGKILLed engine leaves children running with `cwd` inside a workspace that gets shredded and reused. No `pdeathsig`; the 2h cap only applies while the pump lives.

---

## Moat-by-moat detail

### §1 Harness is the product — AT RISK

- Failure containment is systematic — overflow→force-compact with retry cap, 3-denial breaker, 4-tier stuck watchdog, critic cap→blocked-escalation: every loop has a bounded exit.
- Interrupt-driven approvals cross the checkpoint boundary and survive container replacement; re-driven interrupts reuse the persisted payload. Crash-resume is genuinely pinned by tests (H-19, real graph + Postgres).
- But the mutation path is not fail-safe: non-atomic file writes, no write-ahead, node-boundary-only checkpoints → at-least-once replay of non-idempotent tool calls.
- Event sink failures are log-only; kill is not honored mid-turn; SIGKILL bypasses the cascade drain; replay-fallback mirror lives under `./checkpoints` — dies with the shredded workspace unless deploy maps it.
- Test gaps: wait-for-exit before volume remount, workspace shredding, 30d/12mo retention decay, and `release == commit` slot-freeing are all unpinned.

**Strengths:** bounded exits on every loop · approvals durable + fail-closed (timeout=DENY, errors=DENY) · verbatim contract: gate-edited args are what execute.

### §2 Conflict-free concurrency — FAIL

- The per-repo write lock — the moat's core invariant — is not enforced anywhere in the spawn path. `_veto` checks saturation and width only; CollisionRadar warns after the fact.
- Process-global registries (spawn dict, terminal notifications) couple concurrent runs in one process — the exact H-10 bug class the codebase already fixed elsewhere.
- What works: per-lane keys (`context_id`, per-thread channels), ContextVar isolation for memory/questions, per-thread checkpointing, read-before-edit hash guard as optimistic concurrency.
- Read path escapes the workspace while delete path is contained — asymmetric isolation (aligned with §4 free-read, worth a conscious decision).
- Only one test pins any of this (per-thread `ask_user` isolation). Write-lock exclusivity, session-volume isolation, and queue order have zero executable enforcement.

**Strengths:** ContextVar discipline (M-13/M-14 fixes) · per-thread checkpoint namespacing · hash-guard optimistic concurrency between turns.

### §3 Swarm — bounded fan-out — FAIL

- The reservation mechanism §3 specifies — held between `try_acquire` and row insert so N concurrent spawns see each other — does not exist. The check-then-act race is open in executor threads.
- Cap drift: `SWARM_MAX_SLICES=8` hard-capped in `fanout.py` vs the philosophy's stated 100. Either the code or the philosophy is wrong; the tests pin the 8.
- The 2h spawn "drain" only flips an in-memory status flag — no commit/stop path; spawn state is lost on crash.
- Spikes never validated fan-out: matrix axes are models × checks, no N-concurrent-spawn experiment exists.
- Tests pin the saturation veto end-to-end (good) but never fire N concurrent spawns at the cap; the cross-worker DB reservation path is untested.

**Strengths:** saturation veto tested through the tool, not `_veto` · watchdog arming on dispatch (C-04) · spawn stagger justified as thundering-herd guard.

### §4 Token intelligence — no gate — PASS

- No classifier, gate, or admission layer exists anywhere in the harness. Permissions gate mutations (`file_write`, `terminal_exec`), never reads — the correct side of the line.
- `wrap_untrusted` quarantines web/MCP output rather than censoring it; redaction is egress-only (events/deltas/approvals), not model-input gating.
- Gaps in taste-encoding: the golden chain (symbol → grep → smallest range) is NOT taught in the system prompt, and no symbol-lookup tool exists — the chain's first rung is missing.
- Tripwires: the permission ruleset is read/write-agnostic (`tool: "*"` could gate reads); `_SENSITIVE_FILENAMES` wholesale redaction hints at read-path gating applied elsewhere.

**Strengths:** zero read censorship in code · fail-closed is on actions, not information · provenance without gating.

### §5 Cost via relevant reads — AT RISK

- The budget loop is exemplary: `$used/$cap` in the per-turn envelope, 50%/80% reminders, "Never auto-stops" — the human holds the kill switch, exactly §5.
- But the affordance default is inverted: `file_read`'s `limit=2000` makes the full dump the zero-arg path of least resistance; offset+limit exists but is the effortful path.
- Silent truncation hides signal: 16KB caps in `file_search`/`terminal_exec` with no marker; the match-count footer counts the already-truncated text.
- Cost math ignores cached-token discounts (`estimate_cost` bills all input at full price) — the felt budget overstates cost on cache hits. Compaction gates on a 4-chars/token estimate despite real gateway usage in state.

**Strengths:** budget visible in-loop, flagged not enforced · roster budgeted at 1800 chars · bounded tool outputs (16MiB/32KiB/24K caps).

### §6 Max prompt caching — AT RISK

- The layout is right: verbatim system message first; all per-run dynamics (thread id, budget, turn, mode envelope, roster, goal stage) ride in one transient HumanMessage appended last, never persisted.
- The leaks: discovered tools bind natively and grow the tools array above the cache cut on every discovery; the system prompt is re-read from disk every turn (drift busts the cache silently; missing file → silently empty system message).
- Prompt/content drift: `system_prompt.md` promises a skills list "at the end of this system prompt" that is never injected.
- Cache blindness: no cache-hit metric exists; the spike's cache check uses a synthetic prefix and cannot detect real prefix drift; no test pins schema byte-stability or prefix order. §6's failure mode is silent — and currently unobservable.

**Strengths:** two-tier tool surface is the textbook pattern (8 static Tier-0, rest via `tool_search` as tool output) · deterministic ordering everywhere · static module-level schemas.

### §7 Parallel by default — AT RISK

- The hottest loop serializes: tool calls execute in a sequential for-loop with per-call awaited event publish — no gather-then-merge, no documented justification.
- Spawn stagger (2s) is a non-semaphore serializer — justified for gateway thundering-herd, but `_staggered_spawn` is never called in-slice.
- What works: all sync reads dispatched via `run_in_executor`; 4 background tasks start concurrently; shutdown cancel+gather is concurrent; Redis publishes pipelined; the background-task contract (30s foreground → auto-background) is §7 done right.
- Spikes and tests are fully serial — no executable pin would catch a parallelism regression.

**Strengths:** executor-dispatched reads · concurrent startup/shutdown · background contract with lifecycle tracking.

---

## What the harness already does well

Failure containment is systematic (every loop has a bounded exit). Approvals are durable, interrupt-driven, and fail-closed in both engine and bridge. The budget is felt, never enforced — 50/80% reminders with a human kill switch. The two-tier tool surface is the textbook §6 pattern. Read-before-edit hash guards give real optimistic concurrency. And the regression-ID culture (H-10, C-04, M-25…) is exactly the "accumulated, paid-for correctness" the philosophy describes — the audit's job is to extend that discipline to the concurrency moats.

---

## Audit coverage — 10 slices, ~14k lines

| Agent | Slice | Lines | Moat focus |
|---|---|---|---|
| 1 | `engine/graph.py` + `state.py` | 1,286 | §1 recoverability, §7 graph execution |
| 2 | `runner`, `main`, `normalize`, `control`, `forwarder`, `handoff`, `sessions` | 1,388 | §1 lifecycle, §2 session volumes |
| 3 | `llm`, `compaction`, `memory`, `events`, `metrics`, prompts | 1,434 | §4 no-gate, §5 budget, §6 prefix |
| 4 | `fanout`, `goal_mode`, `watchdogs`, `hardening`, `security`, `permissions` | 1,283 | §3 cap + race, §2 write lock |
| 5 | `tools/__init__`, `readonly`, `discovery`, `diagnostics`, `deferred`, `mcp` | 1,340 | §4/§5 read affordances, §6 schemas |
| 6 | `tools/mutating`, `background`, `extended`, approvals ×2, `checkpointer` | 1,434 | §1 write path, §2 mutation isolation |
| 7 | spike core: `tracer`, `engine_matrix`, `agent_loop`, `checks` | 1,294 | Spike coverage vs moats |
| 8 | spike remainder + engine/memory contract tests | 1,491 | Test pins: engine, memory |
| 9 | `test_spine_contract`, `test_rc_modules`, goal_mode, rf_console | 1,457 | Test pins: spine, modules |
| 10 | hardening/mutating/rd_tool/fanout/watchdogs/approval tests | 1,619 | Test pins: safety surfaces |

---

## Decision rule applied

§1 outranks §2 outranks §3 — but all three fail or wobble here. Recommended order:

1. Close the §3 reservation race + reconcile the 8-vs-100 cap.
2. Enforce the §2 per-repo write lock in `_veto`.
3. Make the §1 mutation path atomic + replay-safe.
4. Pin all three with concurrent-spawn contract tests.
5. Then fix the §5/§6/§7 ergonomics: dump-default inversion, prefix leaks, sequential tool loop.
