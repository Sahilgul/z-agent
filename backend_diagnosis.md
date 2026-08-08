# Backend Harness — Moat Audit

> 10 parallel agents · ~26,600 lines across `app/`, `tests/`, `alembic/`, `playbooks/` · evaluated against the 7 Collegium design-philosophy moats · Aug 8, 2026 · Companion to [harness_diagnosis.md](./harness_diagnosis.md) (worker audit)

| Metric | Value |
|---|---|
| Moats failing | **1** (§2) |
| Moats at risk | **3** (§1, §3, §7) |
| Moats passing | **3** (§4, §5, §6) |
| Evidence-backed findings | 70+ |

**⚠ The concurrency moats are single-process correct, distributed-system absent.** Reservations and the per-repo write lock exist and are wired before spawn — but they live in process memory with zero schema backing. §2/§3 hold only under the documented single-writer deployment rule. One second replica, one rolling deploy with overlap, and both moats silently invert.

**Cross-audit reconciliation (worker vs backend):** the worker audit's §2/§3 FAILs were wiring gaps, not total absence — the mechanisms live in `backend/app/orchestrator/semaphores.py`. But three caps now disagree: philosophy **100**, backend **12** (`config.py:100`), worker **8** (`fanout.py:48`). And the worker's `fanout.py` duplicates veto logic that never checks repo, while the backend's `thread_manager.py` does — the veto should live in exactly one place.

---

## Moat scorecard

| Moat | Code verdict | Test pin | Worst evidence |
|---|---|---|---|
| 1 · Harness never unrecoverable | **AT RISK** | PARTIAL | `triggers.py:201` vs `:381` — dedupe commits before `create_run`; crash = event lost forever |
| 2 · Conflict-free concurrency | **FAIL** | PARTIAL | `thread.py` — no one-writer-per-repo constraint; lock is in-memory, single-process only |
| 3 · Swarm bounded fan-out | **AT RISK** | PARTIAL | `semaphores.py` works in-process (race pinned); no DB backing; cap is 12, not 100 |
| 4 · Token intelligence, no gate | **PASS** | PARTIAL | No classifier; guardian is action circuit-breaker; intents route, never filter |
| 5 · Cost via relevant reads | **PASS** | PARTIAL | Per-thread gateway keys + reconciled spend; but plan/debug never settle cost (M-47 leak) |
| 6 · Max prompt caching | **PASS** | PARTIAL | Version-stamped knowledge block below persona; playbooks static; no timestamp poison |
| 7 · Parallel by default | **AT RISK** | UNPINNED | 3 unjustified serial fan-outs: `campaigns.py:78`, `triggers.py:343`, `evidence.py:274` |

---

## Top findings, ranked

1. **Concurrency moats correct only within one process** — `semaphores.py:20-26`: per-repo write lock and capacity reservations are an in-memory set behind `asyncio.Lock`. `thread.py` has no unique/partial index on `repo_scope`+status; no reservation table exists in any migration. The only mitigation is a documented "single-writer rule" (`base.py:4`) — correct-by-deployment, exactly the "engineers being careful" §2 forbids. Any second backend replica double-books repos and capacity.

2. **Three caps: philosophy 100, backend 12, worker 8** — `config.py:100` sets `global_thread_cap=12`; worker `fanout.py:48` has `SWARM_MAX_SLICES=8`; the philosophy states 100. Worse, `swarm.py:165-171` spawns `len(decomposition.slices)` with no re-clamp — a Lead returning 50 slices under cap 12 leaves 38 threads queue-retrying forever (`thread_manager.py:182-198`).

3. **Wait-for-exit is best-effort; no force-stop; no orphan reaper** — `manager.py:253-259`: the 15s poll times out and only logs; `run_manager.py:416-449` then mounts the session volume anyway. Kill rides lossy pub/sub (`control.py:29-30`) with no `stop_container` fallback. `reconcile_on_boot` marks threads stopped but never stops containers. The §1 corruption window opens exactly when the worker is sickest.

4. **Trigger ingest is at-most-once** — `triggers.py:201` commits the dedupe row BEFORE `create_run` at `:381`. A crash between them leaves `status="received"` forever; ADO's retry returns "duplicate" and the event is permanently lost. No reaper for stuck "received" rows (`drain_queued` covers only "queued", `:481`).

5. **Idempotency holes on every external write path** — POST `/runs` has no idempotency key (`runs.py:63-103` — double-click = double run + double spend). `delivery.py:247-264` `open_pr` has no existing-link check (crash → duplicate PR). `ado/client.py:171-200` single-shot writes (retry → duplicate PR/comment). `approvals.py:198-204` decide is check-then-act with no row lock (concurrent double-decide). `proposals.py:152` strands proposals in `accepting` if `create_run` raises.

6. **Slot leak on row-insert failure** — `thread_manager.py:124-129`: `commit_reservation` runs only AFTER `session.commit()`; if the insert raises, no release runs → reservation leaks until restart. The one error path where release != commit. No test covers it.

7. **Test harness structurally cannot see races** — `conftest.py:18` uses in-memory SQLite with `StaticPool` (serializes all DB access) + FakeRedis; `_FakeLaneManager.spawn_many` is a serial for-loop (`test_blueprints_goal.py:75-82`). A serial-fan-out or lock regression would pass green. §2/§3/§7 have no veto-grade executable pin despite `test_orchestrator_semaphores.py:102-120` pinning the N=10 concurrent reservation race in isolation.

8. **Queue is visible but unordered** — `thread_manager.py:182-198`: waiters poll-and-race with fixed 5s sleep; which waiter wins is scheduler luck. §2 demands "visible and ordered, never silent and random." Queued-detection also sniffs the string `"queued"` out of an error message (`:190`) — fragile coupling.

9. **Distiller silent knowledge loss** — `distiller.py:78-80,138-140`: a transient gateway failure makes `distill()` return `[]`, then `run_nightly` marks every summarized run mined. One hiccup permanently skips that night's distillation; recoverable only by manual re-mining.

10. **Repo URL dedupe has no DB backstop** — `repos.py:95-97` check-then-act; `models/repo.py:46`: `remote_url` has no unique constraint (only `name` does). Same URL under two names = one remote as two "repos" = two write-lock keys over one codebase. Concurrent `onboard(repo_id)` races one dest (`repos.py:137`).

11. **plan/debug blueprints leak cost settlement** — `plan.py:180-229` and `debug.py:161-202` never call `settle_cost` for planner/critic/debugger/fixer threads — the exact M-47 leak `swarm.py:260-268` already fixed.

12. **Credential-at-rest inconsistency + silent redis default** — `thread.py:35` stores `gateway_key` plaintext while `user.py:33` encrypts BYO PATs. `config.py:25` `redis_url` defaults to `memory://0` with no validator — a prod deploy missing the env gets an in-process fake bus (JWT/PAT secrets do fail fast, C-13/H-44).

---

## Moat-by-moat detail

### §1 Harness is the product — AT RISK

- Strong bones: boot reconciliation interrupts zombie runs incl. QUEUED/PLANNING; cancel-before-shred and wait-before-remount orderings are test-pinned (G-12); guarded execute never strands runs; event bus acks strictly after DB commit with poison-pill dead-lettering (H-43).
- But the corruption window §1 calls unforgivable is ajar: wait-for-exit times out at 15s and proceeds; kill is lossy pub/sub with no force-stop; orphan containers survive boot reconciliation.
- At-most-once trigger ingest (dedupe commits before run creation); non-idempotent delivery (duplicate PRs on crash-retry); proposals stranded in `accepting`; distiller marks unmined runs mined on transient failure.
- Retention is job-math only — no `expires_at`/`purged_at` columns anywhere; the 30d/12mo two-step decay has no schema marker and its sweep wiring is untested.
- Teardown never drains tracked run tasks; no alembic migration gate at startup — the app can serve against a stale schema.

**Strengths:** reconcile-on-boot incl. zombie stages · ack-after-commit + dead letters · reversible migrations, survivor-safe delete posture (no cascades on runs).

### §2 Conflict-free concurrency — FAIL

- The per-repo write lock exists and is correctly placed (acquired before spawn, released on every terminal path) — but it is a process-local `asyncio.Lock` + in-memory set with a non-locking DB count. Zero schema enforcement: no unique/partial index on `repo_scope`, no lock table.
- The documented "single-writer rule" (`db/base.py:4`) makes correctness a deployment property — precisely what §2 forbids: "not by engineers being careful."
- Queueing is visible (QUEUED stage published) but unordered — poll-and-race, not FIFO.
- What works: per-lane session volumes by construction; tenant scoping hard-enforced (`load_run_for_user`); IDOR guards pinned on every thread-scoped action; tar-slip containment pinned twice.
- Concurrent-decide and approve/reject races are guarded in-app only (no `FOR UPDATE`).

**Strengths:** tenant isolation pinned · reservation pattern documented + wired pre-spawn · IDOR regression suite (C-08..C-11).

### §3 Swarm — bounded fan-out — AT RISK

- The reservation pattern the philosophy specifies exists: `try_acquire` → row insert → `commit_reservation`, and the N=10-concurrent-at-cap-3 race is genuinely pinned (`test_orchestrator_semaphores.py:102-120`).
- But: cap is 12 (`config.py:100`) vs philosophy's 100 vs worker's 8 — three numbers, no reconciliation.
- Decomposed fan-out is unclamped: swarm trusts the Lead's slice count; over-cap slices queue-retry forever.
- Slot leak on row-insert failure; reservations are in-process only (N replicas = N empty reservation sets).
- Partial fan-out failure is handled correctly (partial success, surfaced shortfall, no orphans) — and pinned.

**Strengths:** reservation race pinned by concurrent test · lossy-spawn-safe labeling from `spawn_context` · partial-failure settle surfaced as `fanout_shortfall`.

### §4 Token intelligence — no gate — PASS

- No classifier anywhere in the backend. `classify_text` routes user intent (unmatched text flows to the Lead as conversation); guardian is a circuit breaker on spawning fix runs, never on what the model sees; mentions are scope routing.
- Playbooks teach golden taste: "cite file:line verified by read-only grep… never cite from memory"; "precise beats exhaustive."
- Knowledge retrieval is relevance ranking with lexical fallback (fails open), corpus is human-approved (PHI checkpoint enforced at service layer).
- One tripwire: top-k truncation is the only corpus path into prompts — if no agent-facing full-corpus search tool exists, retrieval becomes a de facto gate. Flagged for verification against the worker tool surface.

**Strengths:** ranking, never filtering · PHI checkpoint at service layer, pinned · retrieval fails open.

### §5 Cost via relevant reads — PASS

- Per-thread LiteLLM virtual keys with `max_budget_usd` minted before container start; gateway-metered spend reconciled post-run; keys released on every terminal path.
- Spend visible at run, thread, fleet, and PR-body levels; rate-limit overflow becomes a durable "queued" verdict, never silent.
- Smallest-chunk discipline in knowledge/evidence/ideas injection (top-k, 300/600/2000-char caps).
- One leak: `plan.py` and `debug.py` never settle spawned-thread costs (M-47 class); tests pin settle only for swarm/ask.

**Strengths:** per-thread priced keys + readback · cost in PR body for the reviewing human · bounded injections everywhere.

### §6 Max prompt caching — PASS

- Persona composition is deterministic: mode persona + versioned static playbooks + role-hint constants; knowledge block is per-run LRU-cached (cap 256), version-stamped, invalidated on approve/reject, appended AFTER the persona (dynamic below the cut), and stored in `spawn_context` for replay-identical prompts.
- No timestamps baked into rendered blocks; episodic run-ids stable within the per-run cache.
- Backend holds its side of §6 — the prefix leaks found in the worker audit (discovered-tools growth, per-turn prompt re-read) are worker-side, not here.

**Strengths:** M-37 version-stamped block cache · static playbooks from DB in stable order · `spawn_context` enables replay-identical prompts.

### §7 Parallel by default — AT RISK

- The critical paths are parallel: `spawn_many` uses `asyncio.gather`; explorer fan-out and collect are concurrent; stamping is per-spawn; onboarding fans out fire-and-forget.
- Three unjustified serializers: `campaigns.py:78-83` (sequential repo fan-out despite §3 reservations making concurrent spawns safe), `triggers.py:343,500` (serial trigger fan-out/drain), `evidence.py:274-280` (independent `verify_suite` gates run serially — up to 4×600s added latency).
- Concurrency is unpinnable in the current test harness: serial fake `spawn_many` + StaticPool SQLite mean a serial regression passes green. No test measures concurrency anywhere in the backend suite.

**Strengths:** gather-based `spawn_many` · true-dependency serialization only in blueprint nodes · concurrent subsystem startup.

---

## What the backend already does well

The reservation pattern the philosophy specifies actually exists here — `try_acquire` → row insert → `commit_reservation` — with the concurrent race genuinely pinned by test. Boot reconciliation interrupts zombie runs; the event bus acks strictly after commit with poison-pill dead-lettering; tenant isolation is hard-scoped and IDOR-pinned; playbooks are static, versioned, and teach grep-then-cite; knowledge injection respects the cache cut with a version-stamped per-run block cache; all 12 migrations are reversible; webhook dedupe is a real DB constraint; and the regression-ID culture (H/M/C/G/L series) continues the "accumulated, paid-for correctness" tradition. **The backend is closer to the philosophy than the worker — its gap is distribution, not direction.**

---

## Audit coverage — 10 slices, ~26.6k lines

| Agent | Slice | Lines | Moat focus |
|---|---|---|---|
| 1 | orchestrator core: `run_manager`, `thread_manager`, `semaphores`, `mode_engine` + tests | 2,640 | §3 reservations, §2 write lock, §1 lifecycle |
| 2 | blueprints: goal + development + assets + playbooks + `conftest` | 3,160 | §7 concurrent fan-out, §3 clamping |
| 3 | blueprints: plan + swarm + debug + ask + tests | 2,318 | §3 swarm spawn path, partial failure |
| 4 | sandbox + events + ws + gateway + tests | 2,404 | §1 wait-for-exit/shredding, event durability |
| 5 | API layer (17 routers) + `main` + `test_api.py` | 3,162 | §2 queueing, §1 idempotency |
| 6 | services: triggers, delivery, approvals, heartbeats + tests | 2,389 | §1 delivery/approval durability |
| 7 | services: sessions, transcript, hydration, push + ADO + auth + tests | 2,444 | §1/§2 session retention & isolation |
| 8 | services: knowledge, evidence, ideas, guidebooks, playbooks, distiller + tests | 2,671 | §6 injection placement, §4 ranking vs gate |
| 9 | services: repos, team, campaigns, proposals, byo_pat + tests | 2,839 | §2 fleet isolation, golden-repo tier |
| 10 | db models + alembic + core + core tests | 2,579 | §2/§3 schema enforcement, §1 retention |

---

## Decision rule applied

§1 outranks §2 outranks §3. Recommended order:

1. **Harden §1's sacred invariant** — wait-for-exit must gate (not log-and-proceed), add force-stop fallback + orphan-container reaper, fix trigger at-most-once (reap stuck `received` rows or process-then-log).
2. **Give §2/§3 schema backing** — unique partial index on `repo_scope` (one active writable thread per repo), DB reservation table or Postgres advisory locks, ordered (FIFO) queue.
3. **Reconcile the cap trinity 100/12/8** and clamp decomposed fan-out in `swarm.py`.
4. **Close the idempotency holes** — POST `/runs` idempotency key, `open_pr` existing-link check, ADO idempotent writes, approval decide row lock, proposals `accepting` rollback.
5. **Make concurrency testable** — a real Postgres+Redis contract-test tier so §2/§3/§7 regressions can't pass green.
