# Worker↔Backend Integration — Seam Audit

> 10 parallel cross-boundary agents · every finding verified against code on **both** sides of the seam · evaluated against the 7 Collegium design-philosophy moats · Aug 8, 2026 · Companion to [harness_diagnosis.md](./harness_diagnosis.md) (worker) and [backend_diagnosis.md](./backend_diagnosis.md) (backend)
>
> Honesty protocol: every claim carries file:line evidence read this session; statuses are CONFIRMED / RISK / UNVERIFIED; agents were instructed that zero findings is an acceptable result and that correcting a false prior claim is a valuable finding. The six headline blockers were additionally spot-verified by the orchestrator with direct greps (all reproduced).

| Metric | Value |
|---|---|
| Seams failing | **5** (control plane, run lifecycle, spawn/env contract, exit ordering, sessions) |
| Seams at risk | **4** (event ingest, approvals, gateway keys/cost, thread orchestration) |
| Seams passing | **1** (deltas/relay/WS transport — with gaps) |
| Raw findings | 86 across 10 agents (~50 unique after cross-agent dedup) |
| Unique BLOCKER clusters | **7** |
| Prior-diagnosis claims corrected | 24 (see honesty ledger) |

**⚠ THE META-FINDING: both sides now default to `ENGINE=custom`, but the seam contract was built for the legacy SDK runtime — and nobody re-negotiated it.** The backend injects SDK-era wires the custom engine never reads (`PERSONA_PROMPT`, `RESUME_SESSION_ID`, the `/root/.claude` volume, `interrupt`, `session_id`-from-turn-complete), while the engine reads wires the backend never sets (`RESUME_CONTEXT_ID`, `DATABASE_URL`, `CHECKPOINT_MIRROR_DIR`, per-run `MODE`, `MCP_SERVERS`). Five of the seven blocker clusters are this single skew, and most of them collapse into one fix: make `thread_env` emit the engine's actual contract. (`backend/app/core/config.py:39` + `worker/Dockerfile:36` — both default to custom.)

---

## Seam scorecard

| Seam | Verdict | Worst evidence |
|---|---|---|
| 1 · StepEvent ingest (stream→DB) | **AT RISK** | No unique constraint on `(run_id, thread_id, seq)` — redelivery double-inserts (`event.py:26-32`, `bus.py:137-141`) |
| 2 · Deltas / relay / WS | **PASS*** | *Gap:* TypingDeltas bypass the redaction StepEvents get (`graph.py:356-362` vs `events.py:19`) |
| 3 · Approval roundtrip | **AT RISK** | `always_allow` persists for the DESTRUCTIVE `file_delete` tool (`graph.py:452-453`) |
| 4 · Control plane | **FAIL** | `interrupt` has no handler in the default engine — every stop path is a no-op (`runner.py:426-458`) |
| 5 · Run lifecycle state | **FAIL** | `input_required` freezes the row, wedges blueprint awaits, releases the repo write lock while a live writable worker holds it |
| 6 · Spawn / env contract | **FAIL** | `PERSONA_PROMPT` composed, injected, stored — and dropped by the engine (`main.py:57,91-92` SDK-only) |
| 7 · Exit ordering & durability | **FAIL** | kill_replace proceeds after a 15s log-only wait; stop→resume has no wait at all; two containers share one workspace |
| 8 · Gateway keys & cost | **FAIL** | `release_key` wired on 5 of ~11 terminal paths; leaked keys stay live forever (no expiry, no reaper) |
| 9 · Sessions / fork / hydration | **FAIL** | Resume is dead under the default engine — three independent dead wires (see B1) |
| 10 · Thread orchestration & caps | **AT RISK** | Worker `spawn_swarm`/`spawn_agent` are phantom — nothing executes, `spawn_done` has no producer |

---

## The 7 blocker clusters, ranked

### B1 · Resume is dead under the default engine — triangulated by 5 agents (A1, A2, A6, A7, A9)

Three independent wires are cut, any one of which alone kills resume:

1. **The engine never emits `session_id`.** The custom engine's turn-complete event carries `{num_turns, duration_ms, is_error, usage}` only (`worker/worker/engine/events.py:163-172`; grep for `session_id` across `worker/worker/engine/` returns zero). The backend's *only* writer of `thread.session_id` gates on `detail["session_id"]` in a `"turn complete"` STATUS event (`backend/app/events/bus.py:150-156`) — a field only the legacy SDK normalizer emits (`worker/worker/normalize.py:154-167`). Under the default engine the column stays NULL forever, so the resume banner never appears (`api/sessions.py:70`) and replacements inherit nothing (`thread_manager.py:94-108`).
2. **The resume env var names don't match.** The backend injects `RESUME_SESSION_ID` (`sandbox/manager.py:154-155`); the custom engine reads only `RESUME_CONTEXT_ID` (`engine/runner.py:87`) — which **nothing in the backend ever sets** (grep-verified). On replacement, `context_id` falls back to the *new* thread's uuid (`runner.py:99`) → fresh checkpoint namespace → the engine seeds a stranger (`runner.py:266-268`).
3. **The durable substrate is missing anyway.** `engine_database_url` defaults to `""` (`backend/app/core/config.py:47`) → `DATABASE_URL` is not injected (`manager.py:152-153`) → the checkpointer falls back to process-memory `MemorySaver` (`checkpointer.py:195-205`). The JSONL replay mirror defaults to `./checkpoints` = `/app/checkpoints` (`runner.py:88`, `worker/Dockerfile:19`) — container-local, destroyed on every `remove(force=True)` (`manager.py:235`), *including healthy `finish_thread`* (`thread_manager.py:257-258`). Meanwhile the backend carefully mounts, retains, and corruption-guards a `/root/.claude` session volume (`manager.py:190-192`) that **no worker code ever writes** (grep-verified).

The `kill_replace_thread` docstring — "the replacement resumes where the killed thread left off — now actually true" (`run_manager.py:364-369`) — is false for the default runtime. **Fix:** set `RESUME_CONTEXT_ID=<prior thread's context_id>` on replacement, fail-closed on `engine_database_url` for `ENGINE=custom`, and either emit a resumable identity in the engine's turn-boundary event or gate all resume UI on `ENGINE=sdk`.

### B2 · "Stop" is a fiction on the default engine — triangulated by 3 agents (A4, A5, A7)

Every backend stop path publishes `{"type": "interrupt"}` (`events/control.py:20-21`; callers: `api/threads.py:50`, `run_manager.py:240` stop_run, `run_manager.py:320` stop_thread). The custom engine's control pump branches on `kill` / `nudge` / `spawn_done` / `mode` only (`worker/worker/engine/runner.py:426-458`) — `interrupt` falls through to a heartbeat and the turn runs on. Only the legacy SDK runtime honors it (`worker/main.py:171-173`).

The backend nonetheless stamps rows `stopped`, frees the capacity slot, and **deletes the gateway key** (`run_manager.py:232-243`) — the UI banner reads "Stopped — all work preserved" while the worker keeps mutating the workspace. The thread's actual death is a side effect: its next LLM call fails auth on the deleted key, surfacing as `"failed"` — contradicting the banner, and ingested *after* it (`bus.py:137-141` has no stage check). `stop_run`'s thread filter (`run_manager.py:226`) even omits `input_required` threads, so an approval-parked thread isn't stamped or key-released at all. **Fix:** teach the pump an `interrupt` handler (cancel in-flight turn, set `_stop`, heartbeat `stopped`), or downgrade stop paths to `kill` + `stop_container`; add a worker→backend ack and gate the DB stamp on it.

### B3 · Two containers, one workspace — the §1 corruption window is open on two paths (A4, A5, A6, A7, A9)

- **kill_replace:** publishes a lossy, un-acked kill (`run_manager.py:408`, `control.py:29-30`), waits 15s (`manager.py:248-259` — times out with a log line and returns `None`, indistinguishable from exit), then spawns the replacement on the same volumes (`run_manager.py:443-449`). The worker cannot honor kill mid-turn (`_stop` polled only at the idle loop, `runner.py:283-288`) and a 900s approval BLPOP (`runner.py:211`) blows the window by 60×. `stamp_clone` then `rmtree`s and re-clones `workspaces/<run>/<repo>` (`manager.py:53-80`) **under the old container's live bind mount** — and since a bind mount is the same host tree, the old worker sees the new clone and keeps editing it. Sharpest sub-case: a human clicks "allow" after the replace — the old worker's BLPOP returns and the "replaced" container executes the approved mutation (`runner.py:218`) alongside its replacement.
- **stop→resume:** `resume_run` has **no kill, no wait, no stage guard, no idempotency** (`run_manager.py:116-148`) — and the `interrupt` it relies on is the no-op of B2. A double-click or a resume on a running run spawns a second blueprint execution on the same session volume, unconditionally.
- Late events from the "dead" worker are still ingested — the bus has no terminal-status guard (`bus.py:137-161`).

**Fix:** `wait_for_container_exit` must return a status; on timeout escalate to `stop_container` (the pattern `abandon_run` already uses, `run_manager.py:267-268`) and *fail the replace* instead of proceeding; give `resume_run` the same gate.

### B4 · `input_required` wedges the thread row — and silently releases the repo write lock (A5, A10)

During a 900s approval wait the worker heartbeats the free string `"input_required"` (`runner.py:209-213`, also blocked-escalation at `:358-362`). The backend mirrors status verbatim while the row is in `ACTIVE_STATUSES` (`services/heartbeats.py:115`) — a tuple that deliberately excludes `input_required` (`semaphores.py:15`). Cascade, all in code:

1. First approval wait: row `"running"` → overwritten with `"input_required"`.
2. From then on **every** beat is rejected (`"input_required" ∉ ACTIVE_STATUSES`) — the row shows a *fresh* `heartbeat_at` (`heartbeats.py:108` is unconditional) with a *stale, wrong* status forever.
3. Every blueprint `_await_thread` terminal set excludes it (`ask.py:108-113`, `development.py:391-393`, `plan.py:254-256`, `debug.py:256-258`, `swarm.py:280-282`) — five of six have **no timeout**, so runs hang in INVESTIGATING/DEVELOPING/PLANNING forever; goal's guard fails the run at 2700s even if the human approved.
4. **§2 inversion:** capacity counting (`semaphores.py:31`) and the per-repo write lock (`semaphores.py:46-55`) both filter on `ACTIVE_STATUSES` — a parked *writable* thread stops counting, so a second writable thread on the same repo passes `try_acquire` while the first container still holds its writable clone. When the human approves, both write the same repo.
5. `reconcile_on_boot` skips it (`run_manager.py:638`); only a nudge (`run_manager.py:294,309`) or explicit stop unsticks the row.

**Fix:** treat `input_required` as active for status-mirroring (and explicitly exclude it in capacity accounting if desired), so post-approval beats land.

### B5 · The worker's swarm is phantom — `spawn_swarm` spawns nothing (A10, A4, A6)

`spawn_swarm`/`spawn_agent` only register in-memory entries and return `"spawned swarm of N threads"` (`worker/worker/engine/fanout.py:187-236`). The registry's consumers are a watchdog armer (`tools/__init__.py:287-311`) and a `finish()` on the `spawn_done` control message (`runner.py:438-444`) — and **`spawn_done` has no producer anywhere in the repo** (grep-verified: only the consumer exists; `LaneControl` publishes only interrupt/nudge/mode/kill/resolve_approval). No subagent loop, no container, no backend call ever starts work. The model is told siblings are running; the backend never sees them; the UI never lists them; zero capacity is reserved; zero work happens. Because no `spawn_done` ever arrives, the 8-entry registry (`SWARM_MAX_SLICES`, `fanout.py:48`) saturates permanently and every later fan-out is vetoed until the 2h watchdog flips flags (`fanout.py:270-277`).

**This resolves the cap-trinity question:** the feared 12-threads × 8-slices = 96-agent explosion cannot occur — worker-internal spawns consume no backend capacity and no compute, because they are inert. The backend counts Thread rows (`semaphores.py:31`); the worker counts phantom registry entries (`fanout.py:91-95`). **Fix:** implement real execution behind the registry (in-process subagent contexts, or a worker→backend spawn RPC flowing through `thread_manager.spawn` so capacity/locks apply, with `spawn_done` as a real backend→worker message) — or remove the tools from the surface until the wire exists.

### B6 · Gateway keys leak live forever on most death paths (A8)

`release_key` (`thread_manager.py:222-233`) is called from exactly five sites: `finish_thread` (goal blueprint only), `_mark` on spawn failure, `stop_run`, `stop_thread`, `reconcile_on_boot`. **Missing from:** `abandon_run` (kills containers, never releases), `kill_replace_thread` (old thread stamped "replaced", no release), and every worker-reported terminal state — the heartbeat persister stamps `"completed"`/`"failed"` with no release hook (`heartbeats.py:115-116`), while `stop_run` and reconcile explicitly *skip* `"failed"`/`"replaced"` threads. `mint_key` sets no expiry (`gateway/litellm.py:44-48`) and no key reaper exists. The inversion: a thread that cleanly reports `"failed"` (e.g. budget exhaustion) is excluded from both stop-path and reconcile-path release — the cleaner the death signal, the more permanent the leak. Compounds with: plaintext `gateway_key` column never cleared (`db/models/thread.py:35`), and the spawn-failure release defeated by commit ordering — the keyed row commits only *after* container start (`thread_manager.py:122-162`), so a start failure leaves a minted, unpersisted, undeleted key. **Fix:** one `terminate_thread` path (settle → release → clear column) that *all* terminal transitions route through; commit the key at mint; add gateway-side `duration` as backstop.

### B7 · `PERSONA_PROMPT` is dropped by the default engine — the §6 pipe has no receiver (A6)

The backend composes mode persona + versioned playbooks + knowledge block (`thread_manager.py:83-85`), injects it (`manager.py:133`), and stores it in spawn_context "so a kill/replace replay reproduces the exact same prompt" (`thread_manager.py:109`). The custom engine never reads it: `PERSONA_PROMPT` is consumed only in the SDK runtime (`worker/worker/main.py:57,91-92`); grep finds no `persona` reference anywhere in `worker/worker/engine/`; the engine's system prompt is the image-baked `system_prompt.md` (`graph.py:91-94`). Every thread under the default engine boots with the generic baked prompt — the entire persona/playbook/knowledge flywheel terminates at a sealed valve, silently. The backend audit's §6 PASS ("replay-identical prompts via spawn_context") describes a pipe with no receiver. **Fix:** have the engine accept `PERSONA_PROMPT` (composed above the cache cut per §6), or stop injecting it and delete the dead composition claim.

---

## High findings (deduplicated)

| # | Finding | Evidence | Agents |
|---|---|---|---|
| H1 | **Event record integrity:** no unique constraint on `(run_id, thread_id, seq)` (`event.py:26-32`); ack-after-commit crash window re-inserts (`bus.py:161`→`:177`); worker `seq` resets to 0 per activation (`events.py:33`, `runner.py:101`); backend-persisted user messages allocate from the same seq space (`api/runs.py:45-47`) → duplicate/interleaved replay rows | both sides | A1, A2, A9 |
| H2 | `always_allow` persists for DESTRUCTIVE `file_delete`: `graph.py:452-453` persists without the allowable-check; the re-verify guard covers only `terminal_exec` (`engine/approvals.py:176-182`); backend accepts `always_allow` for any card (`api/approvals.py:79-97`) | both sides | A3 |
| H3 | Double-decide race + the loser's RPUSH lingers and is consumed as a **stale decision on crash re-drive** (`services/approvals.py:198-204` no row lock; `runner.py:198-218` re-BLPOPs the same id) | both sides | A3 |
| H4 | Delivery push targets the **golden clone, not ADO**: stamp's `origin` is a local path (`manager.py:74`), `delivery.py:204-209` pushes there (polluting golden with `agent/*` refs, violating fetcher's only-writer invariant), then opens an ADO PR for a branch ADO never received. Related: `FLEET_PAT` injected into workers (`manager.py:164-168`) with **zero consumers** | backend + worker-adjacent | A6 |
| H5 | kill-replace drops `preserve_workspace`: respawn omits it (`run_manager.py:443-449`) → `stamp_clone(fresh=True)` rmtrees the killed thread's uncommitted work (`manager.py:63-68`) — the exact case goal-mode stores `preserve_workspace: True` for (`thread_manager.py:118`, never read). `handoff.py`/`git_checkpoint` are dead code | both sides | A6 |
| H6 | `reconcile_on_boot` falsely kills healthy runs: no liveness check (never calls `container_running`, never reads the heartbeat TTL key or `heartbeat_at`), never stops containers, never re-registers event streams — and sweeps the human-parked VERIFYING stage, arming a workspace-destroying resume (`run_manager.py:616-654`) | both sides | A5, A7 |
| H7 | Hard worker death is invisible forever: nothing reads container exit codes or logs (grep-verified); no heartbeat-timeout enforcement; 5 of 6 blueprint await loops unbounded; pre-heartbeat boot crashes leave the row `"running"` holding its slot and write lock until a human acts | both sides | A5, A6 |
| H8 | Abandon unregisters the event stream **without draining** (`run_manager.py:273`, `bus.py:59-61`); trailing events strand forever (no XAUTOCLAIM, no TTL, no maxlen anywhere); backend restart severs ingest for all in-flight runs | both sides | A7, A10 |
| H9 | `MODE` env is a global constant: `manager.py:145` injects `engine_default_mode` (`"development"`) for every run — ask/plan/swarm threads boot with the mutating tool surface and phantom spawn tools bound (`tools/__init__.py:169-201`); goal-mode graph branches never fire (`graph.py:98,153`) | both sides | A4, A6, A10 |
| H10 | Contracts drift: `collegium-contracts` unpinned in both pyprojects; each Docker image bakes its own copy at its own build time; no runtime `schema_version` guard (contract's own rule unimplemented); additive drift silently dropped, missing-field drift dead-letters invisibly; image tag drift already visible (0.1.0 config vs 0.2.0 Dockerfile header) | both sides | A6, A10 |
| H11 | Edit-and-resend is dead end-to-end: intent advertised (`services/runs.py:23-24`), dispatcher has no branch (falls through to fake `{"status": "ok"}`, `api/runs.py:462`), fork helpers have no production callers either side, the one smoke test calls with a wrong signature; `forked_from_session_id` has no writer | both sides | A9 |
| H12 | `settle_cost` is success-path-only: swarm/ask/goal settle; plan/debug/**development** never; stop/abandon/failed/reconcile never; all-failed swarm early-returns before its own settle loop (`swarm.py:233-243` vs `:258`) | backend, seam-visible | A5, A8 |
| H13 | TypingDeltas bypass redaction: StepEvents pass through `redact()` (`events.py:19`) but the live delta leg does not (`graph.py:356-362`; SDK path redacts nothing at all) — a secret is redacted in the durable record after streaming live to the browser | worker + relay | A2 |
| H14 | Dead-letter stream is write-only: nothing reads `:deadletter`, and the "watchdog card" the bus docstring promises (`bus.py:8-10`) is never created — a dead-lettered event is a permanent silent hole in the PHI-grade record | backend | A1 |
| H15 | Budget signals diverge ~3× by construction: worker `estimate_cost` bills all input at $2.00/1M (`llm.py:262-276`) vs gateway $0.60 with cache pricing (`infra/litellm/config.yaml:30-32`); reminders fire at ~15-24% of real budget; a hard gateway cap manifests only as a generic non-retryable turn failure. kill_replace also silently resets budget (new full-budget key + `Budget(used=0)`) | both sides | A8 |

## Medium findings (selected, all CONFIRMED unless noted)

- **Approvals:** decide-after-timeout window — no `expires_at` check in the decide path; row can say "allow" for an action the engine already denied (A3). Crash between BLPOP-return and checkpoint loses the decision → row/event/engine triple disagreement (A3). Decision vocabulary mismatch **both ways**: backend's `deny_tool` is unparseable by the worker; worker's `edited_allow` is 422'd by the API; the H-24 comment is wrong both directions (A3). Kill/SIGTERM never wakes a pending BLPOP; no backend path cleans the pending row on thread death (A3, A4). No XAUTOCLAIM on the approvals consumer group — crash strands cards silently (A3, RISK). Approval-stream registration is in-memory; backend restart black-holes new cards from still-alive workers (A3, RISK).
- **Control plane:** no ack/confirmation path for any control message; exactly-once-critical flows (kill-before-replace, trigger nudges) ride fire-and-forget pub/sub — while the approval channel one file away is durable RPUSH/BLPOP (A4). `mode` message is a domain mismatch (backend sends permission modes, engine parses blueprint modes; zero production callers — latent trap) (A4, A6). Nudge is turn-boundary on the default engine, not the advertised "graceful interrupt+inject+resume" (A4). Two divergent stop paths: `/threads/{id}/stop` does no bookkeeping; both report success for unguaranteed delivery (A4).
- **Exit/liveness:** 5s docker-stop ladder vs a worker that can legitimately need 900s — every forced stop of a busy worker is a SIGKILL mid-node; mutations since the last node boundary have no event record and no checkpoint (A7). Heartbeat persister has **no reconnect loop** — a dropped Redis connection silently freezes every thread row (A7). Workspace shredding fires only on the abandon path; retention helpers exist but nothing schedules them (the repo's own README admits it) (A7). Queue waiters are invisible (fake `thread_hint` ids on a channel whose docstring says the UI drops fake ids) and unordered (A10). POST /runs double-create: read-only modes double silently; writable modes fail loudly via the write lock (A10). First-turn crash window: `session_id` leaves the worker only at turn-complete, so a mid-first-turn death is unresumable even on the SDK runtime (A9). Playwright MCP unwired for the default engine; backend capture client is a stub (A6). Stop/spawn race can land a live thread on an INTERRUPTED run; a late blueprint error overwrites INTERRUPTED with FAILED (A5).

## Low findings (selected)

Crash-between-xack-and-relay WS gap (correct priority order, recoverable — A1) · `publish_global` broadcasts repo names cross-tenant (`relay.py:122-124` ← `repos.py:184` — A2) · two red test suites sit exactly on the seam's schema/payload (push deep-link; contracts `lane_id`→`thread_id` rename — both reproduced by running them — A2) · no TTL on approval decision lists or always-allow sets (A3) · `session_store` cross-host mirror is dead code; retention sweep never purges mirrors (A9) · orphan event streams after unregister/restart (A10) · trigger contract drift: `received_at` dropped, `schema_version` unguarded, drain hardcodes source (A10) · `LITELLM_BASE_URL` injected without `/v1` (UNVERIFIED gateway behavior — A8) · `release_key` best-effort, no retry (A8) · env-size ceiling on TASK_PROMPT/PERSONA_PROMPT fails safe but opaque (A6) · IDLE_TTL drift between runtimes, never injected (A6) · approval timeout configured on one side only — defaults match, deploys can skew (A5) · `_wait_for_heartbeat` races the worker's control subscribe (A4) · `last_active_at`/`heartbeat_at` are display-only liveness illusions (A5).

---

## Honesty ledger — 24 prior claims re-verified

**Corrected / disproven (14):**

1. **backend §5 "keys released on every terminal path" — FALSE.** Release is missing on abandon, kill/replace, worker-reported completed/failed, and all non-goal blueprint endings (A8).
2. **backend §1 "poison-pill dead-lettering (H-43)" — overstated.** Ack-after-commit is real; the dead-letter is a write-only hole, and the promised watchdog card is never created (A1).
3. **`kill_replace_thread` docstring "now actually true" — FALSE for the default runtime** (five agents, B1).
4. **harness_diagnosis line 93 "redaction is egress-only (events/deltas/approvals)" — wrong for deltas** (A2).
5. **worker #11 (always-allow hole) — confirmed but rescoped** to `ENGINE=sdk`; and the *default* engine has its own hole: `file_delete` escapes the destructive re-verify (A3).
6. **worker #1 "durable row created async after the tool returns" — FALSE as stated.** No durable row exists for worker-internal spawns; the in-memory registry is the entire lifecycle (A10).
7. **backend #2 "over-cap slices queue-retry forever" — over-precise.** Slots free at explorer idle-TTL, so slices drain in slow waves; the true permanent hang requires the `input_required` wedge (A10).
8. **backend #5 "double-click = double run + double spend" — sharpened.** Read-only modes double silently; writable modes fail the second run loudly via the write lock; no shared-volume corruption (A10).
9. **worker #2 (`_veto` never checks repo) — literally true, wrong blast radius.** Phantom spawns can't land two writers on one repo; the live two-writer path is B4 (A10).
10. **worker §1 "replay-fallback mirror dies with the shredded workspace" — right conclusion, wrong mechanism.** It dies with the container rootfs on *every* `remove(force=True)`, including healthy completion; no workspace-mapping fix can save it (A6, A7).
11. **worker #4 (at-least-once replay) — nuanced.** Replay machinery is currently inert (no `RESUME_CONTEXT_ID`, MemorySaver default), so today's dominant failure is *total state loss*; #4 goes live the moment resume is fixed — fix both together (A7).
12. **backend strengths "reconcile interrupts zombie runs incl. QUEUED/PLANNING" — partially wrong.** Including the human-parked VERIFYING stage is a bug that arms a workspace-destroying resume (A5).
13. **H-24 comment (`api/approvals.py:20-25`) — wrong both directions** on the decision vocabulary (A3).
14. **backend §5 "rate-limit overflow becomes a durable 'queued' verdict" — mis-scoped.** That's ADO trigger rate-limiting; no gateway-429→queued signal exists for the worker (A8).

**Confirmed as written (10):** backend #1 (single-process locks), backend #3 (wait-for-exit best-effort — sharpened: resume has *no* wait; abandon *does* force-stop), backend #6 (slot leak on insert failure), backend #8 (poll-and-race queue), backend #11 (settle leak — extended to `development.py` and all non-happy terminal paths), backend #12 (plaintext key — worsened: never cleared, mostly live), worker #3 (cap drift 100/12/8), worker #8 (kill not honored mid-turn; lossy channel — sharpened: even a *delivered* kill defeats the 15s wait), worker #9 (sink failures swallowed — nuanced: H-01 fixed ordering, not failure semantics), worker §5 cost-math (cached tokens billed full price; `would_exceed` never called in production).

---

## What the seam already does well (verified-OK highlights)

Channel names match **exactly** on both sides everywhere: `events:{run_id}`, `deltas:{run_id}`, `approvals:{run_id}`, `approval:{id}:decision`, `thread:{id}:control`, `thread:heartbeats` (independently verified by four agents). The StepEvent/TypingDelta JSON round-trip loses nothing. WS fan-out is owner-scoped per run — no cross-run step/delta leak. The approval happy path is genuinely solid: `approval_id` reuse across container replacement, fail-closed timeouts (900 == 900 on both sides), idempotent card creation (M-34). Ingest acks strictly after DB commit with per-message containment (H-43). Keys are minted before container start, scoped per-thread, never echoed into events/APIs/logs. Run terminal-stage guards (H-41/H-42) are real. `abandon_run` is the one fully-safe teardown (force-stop + shred + unregister). The uv workspace unifies contracts locally. IDOR guards hold on every thread-scoped endpoint. Registration precedes container start and the consumer group reads from `id="0"` — no boot-time event-loss window. **The Redis layer of this seam is name-for-name correct; what fails is the container contract and the state machines built on top of it.**

---

## Audit coverage — 10 cross-boundary slices

| Agent | Seam | Worker side | Backend side |
|---|---|---|---|
| A1 | StepEvent ingest | `forwarder.py`, `engine/events.py` | `events/bus.py`, `transcript.py`, `models/event.py` + contracts |
| A2 | Deltas/relay/WS | `normalize.py`, graph delta sites | `events/relay.py`, `ws/events.py`, `push.py` |
| A3 | Approval roundtrip | `approvals.py` ×2, graph gate | `services/approvals.py`, `api/approvals.py`, `events/control.py` |
| A4 | Control plane | `control.py`, runner pump | `events/control.py`, `api/threads.py`, `mode_engine.py` |
| A5 | Run lifecycle | `engine/runner.py`, `state.py` | `run_manager.py`, `models/run.py` |
| A6 | Spawn/env contract | `main.py`, `handoff.py` | `sandbox/manager.py`, `fetcher.py`, `playwright.py` |
| A7 | Exit ordering | `checkpointer.py`, shutdown path | `manager.py` exit path, `session_store.py` |
| A8 | Gateway keys/cost | `engine/llm.py`, budget regions | `gateway/litellm.py`, settle paths |
| A9 | Sessions/fork | `sessions.py`, resume regions | `services/sessions.py`, `hydration.py` |
| A10 | Orchestration/caps | `fanout.py` boundary, pyproject | `thread_manager.py`, `semaphores.py`, `api/runs.py` + contracts |

Cross-validation was extensive: B1 triangulated by 5 agents, B2/B3 by 3-5, B4/B5 by 2-3, H9/H10 by 3 — independent agents reaching the same root cause from opposite sides of the seam.

---

## Decision rule — fix order

§1 (never unrecoverable) outranks §2 (conflict-free) outranks §3 (swarm) — and at the seam, §1's failures are the contract skew itself:

1. **Bridge the ENGINE=custom contract skew in `thread_env`** — emit `RESUME_CONTEXT_ID` on replacement, per-run `MODE`, `DATABASE_URL`/`CHECKPOINT_MIRROR_DIR` durability, and have the engine consume `PERSONA_PROMPT`. One function collapses B1, B7, H9 and most of A6's slice.
2. **Make stop real** — `interrupt` handler in the custom pump (or stop→kill+`stop_container`), plus a worker→backend ack protocol for exactly-once-critical control messages.
3. **Gate every volume remount on actual container exit** — `wait_for_container_exit` returns a status, force-stop fallback on timeout, `resume_run` gets the same gate; replay `preserve_workspace`.
4. **Unfreeze `input_required`** — persister mirrors it as active; capacity accounting may exclude it explicitly, but the row must keep tracking worker truth.
5. **Event record integrity** — unique `(run_id, thread_id, seq)` + idempotent insert; per-activation epoch or `next_seq` seeding; drain-before-unregister; a dead-letter reader/alerter.
6. **Unified key/cost terminate path** — settle → release → clear column on *every* terminal transition; commit key at mint; gateway-side expiry backstop.
7. **Resolve the phantom swarm** — implement the spawn wire + `spawn_done`, or remove the tools until it exists.
8. **Approval edge hardening** — `always_allow` guard for `file_delete`, decide row-lock + expiry check, decision-key TTLs, reconcile the decision vocabulary both ways.
9. **Delivery remote rewrite** — `git remote set-url origin <remote_url>` in `stamp_clone`; drop or wire `FLEET_PAT`.
10. **Contracts pin + handshake** — single-source the version into both image builds; enforce the contract's own `schema_version` guard in ingest.

---

## Per-agent summaries (for 30-agents-diagnosis.md)

**agent1 (StepEvent ingest):** Plumbing sound (keys, fields, JSON round-trip, shared contracts pin all verified); the edges break — no unique constraint on `(run_id, thread_id, seq)` so the ack-after-commit crash window double-inserts; worker seq resets per activation; the dead-letter stream is write-only with its promised watchdog card never created; and the default engine never emits the `session_id` the backend's entire resume machinery captures.

**agent2 (deltas/relay/WS):** Transport verified clean (exact channel match, lossless JSON, owner-scoped WS auth); breaks are schema-level — corroborated the dead `session_id` capture and per-process seq reset; found TypingDeltas bypass the redaction StepEvents get (correcting the worker diagnosis); `publish_global` leaks repo names cross-tenant; two red test suites (push deep-link, contracts `lane_id` rename) sit exactly where a wire break needs a tripwire.

**agent3 (approval roundtrip):** Happy path genuinely solid (stream fields, decision key/JSON, id reuse on restart, fail-closed timeouts all verified); edges fail — crafted `always_allow` permanently whitelists the DESTRUCTIVE `file_delete` tool in the default engine; lock-free double-decide splits audit from execution and its stale loser poisons crash re-drives; decide-after-timeout rewrites denied history; decision vocabulary mismatches both ways (`deny_tool`, `edited_allow`); kill never wakes the 900s BLPOP.

**agent4 (control plane):** The channel string matches perfectly and the listener is well-built — but the message contract forked between runtimes: the default engine silently drops `interrupt` (every stop path is a DB-only fiction enforced eventually by gateway-key deletion), `spawn_done` has no publisher, `mode` is a domain mismatch, nudges are delayed-not-lost, and no control message has any ack — while the approval channel one file away is durable RPUSH/BLPOP, proving the right pattern exists.

**agent5 (run lifecycle):** The run-row state machine is well guarded (terminal guards, H-41/H-42 verified) but the channel carrying worker truth into it is not — `input_required` permanently freezes thread rows (wedging every blueprint await and silently releasing the capacity slot + per-repo write lock while a live writable worker holds the repo), hard worker death is invisible forever (no exit-code reader, no heartbeat-timeout enforcement, 5 of 6 awaits unbounded), and boot reconciliation falsely kills healthy runs with no liveness check while sweeping the human-parked VERIFYING stage.

**agent6 (spawn/env contract):** The container contract is built for the legacy SDK runtime while both sides default to `ENGINE=custom` — the backend injects wires the engine doesn't read (`PERSONA_PROMPT`, `RESUME_SESSION_ID`, `/root/.claude`) and the engine reads wires the backend never sets (`RESUME_CONTEXT_ID`, `CHECKPOINT_MIRROR_DIR`, `MCP_SERVERS`, per-run `MODE`); also found the delivery push landing in the golden clone instead of ADO, kill-replace dropping `preserve_workspace` (rmtree of uncommitted work), and boot crashes leaving threads "running" forever.

**agent7 (exit ordering):** "Container exits are waited on" is implemented as a 15s log-and-proceed poll on exactly one path, nothing at all on stop→resume, and a 5s SIGTERM→SIGKILL ladder everywhere else — while the worker can legitimately need 900s and never checks `_stop` mid-turn, so every forced transition opens a real two-containers/one-workspace window (sharpest: a replaced worker can still execute a late human approval); durability is inverted — the inert session volume is carefully remounted while the state the engine actually uses dies with the container layer.

**agent8 (gateway keys/cost):** The mint→inject→consume half is genuinely well-built (per-thread keys, fail-closed consumption, never leaked into APIs/logs/events); the release→settle half is broken — release is wired on a minority of terminal paths with no gateway-side expiry and no reaper (default behavior: accumulate live, budgeted, plaintext-stored keys forever), settlement runs only on success paths, and the two budget signals diverge ~3× by construction with no mid-run convergence.

**agent9 (sessions/fork/hydration):** Session continuity is broken end-to-end under the default engine by two independent causes (backend never learns a session identity; the resume signal injected is read only by the legacy runtime while the engine's own resume knob is never set); edit-and-resend is advertised but dead code on both sides behind a fake `{"status": "ok"}`; `resume_run` spawns replacements with no kill/wait or stage guard; hydration itself is fully auth-scoped with no cross-tenant leak.

**agent10 (orchestration/caps/contracts):** The seam's deepest asymmetry — the backend's half of the swarm contract (reservations, write lock, queueing) is real and wired, while the worker's half is a phantom registry that tells the model siblings exist and never executes anything, so the feared 12×8 capacity explosion is impossible precisely because the worker side is unimplemented; the live concurrency hazard is status-driven (`input_required` ejecting threads from cap + lock); contracts are unpinned across separately-built images with no runtime handshake and silent additive drift.
