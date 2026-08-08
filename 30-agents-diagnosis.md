10 worker parallel agents diagnosis


agent1:

Both files read completely. Writing the audit report.

## Agent 1: Orchestration Spine (graph.py, state.py)

### Moat verdicts

| Moat | Verdict | Evidence (file:line) | Notes |
|---|---|---|---|
| 1 — Never unrecoverable | **AT RISK** | graph.py:1087-1115, 26-28 (checkpointer compiled in; Postgres default); 367-376 (interrupt survives container replacement); 761-777 (compaction failure contained, turn survives); 267-271, 80-81 (overflow-retry cap); 404-406, 699-702, 937 (denial/stuck/critic breakers) | Strong containment, but node-granular checkpoints mean a mid-`tools_node` crash replays already-executed, non-idempotent tool calls (519-585), and sink failures drop durable events with log-only (1127-1130) |
| 2 — Conflict-free concurrency | **AT RISK** | state.py:122-126, graph.py:610-614 (H-10: per-thread pending-questions snapshot — good); 665-668 (global `terminal_manager().completed_notifications()` drained with **no thread scoping**); 187, 313 (shared `mcp_manager()`/`tuning` singletons mutated per run) | State itself is per-run via TypedDict + thread_id checkpointing; the unscoped global notification drain can cross-deliver terminal completions between concurrent runs in one process |
| 3 — Swarm bounded fan-out | **NOT APPLICABLE** | graph.py:736-737 (spawn_agent/spawn_swarm are tools, executed elsewhere) | Graph has no fan-out node; tool_calls loop is bounded by model output |
| 4 — Token intelligence, no gate | **PASS** | graph.py:170-198 (mode-binding is capability scope, not read censorship); 396-407 (permission DENY is action policy, by design); 118-121 (budget visible in-turn) | No classifier anywhere in the slice |
| 5 — Cost via smallest reads, visible budget | **PASS** | graph.py:118-121 (spend in env envelope every turn); 324-343 (50%/80% reminders, "Never auto-stops" — human holds kill switch); 745-819 (compaction keeps context lean) | Fully aligned |
| 6 — Max prompt caching | **AT RISK** | graph.py:107-167, 234-236 (dynamic envelope correctly rides below the cut, never persisted); 84-94 (system prompt **re-read from disk every turn**, silently returns `""` if missing); 182-197 (discovered tools bind natively, growing the frozen tool prefix mid-run) | A mid-run prompt-file edit or a tool discovery silently invalidates the whole prefix cache; missing `system_prompt.md` silently degrades every turn |
| 7 — Parallel by default | **AT RISK** (leaning VIOLATION) | graph.py:519-585 (tool calls executed in a sequential `for` loop — `await call_tool_direct` one at a time, no `asyncio.gather`); 624 (per-call awaited event publish adds serial sink latency) | Model-emitted parallel tool calls (independent reads) serialize with no documented justification; only the capacity semaphore may legitimately serialize |

### Top findings (ranked by severity)

1. **At-least-once tool re-execution on crash** — graph.py:519-585. LangGraph checkpoints at node boundaries; a crash after tool call 1 of N executes means resume replays the whole node, duplicating side effects (`file_edit`, `terminal_exec`). Moat-1 hazard: idempotency is assumed, not enforced.
2. **Sequential tool-call loop** — graph.py:519-585. Independent parallel calls serialize; streak/event bookkeeping could be restructured as gather-then-merge. Undocumented serialization = suspect per Moat 7.
3. **Unscoped global notification drain** — graph.py:665-668. Unlike `get_pending_questions(tid)` (613), `terminal_manager().completed_notifications()` takes no thread key; concurrent runs in one process can consume each other's background-terminal completions into their message streams (Moat 2).
4. **Event-sink failures swallowed** — graph.py:1127-1130. Events are Moat-1 survivors ("DB events"); a sink exception leaves a permanent, log-only hole in the durable event stream. No retry/dead-letter.
5. **System prompt re-read per turn, silent-empty fallback** — graph.py:84-94. Disk read every turn; file drift busts the cache silently (Moat 6), and a missing file yields an empty system message with no error (Moat 1 silent degradation).
6. **Discovered tools mutate the frozen prefix** — graph.py:182-197. Each `tool_search` discovery changes bound tool definitions above the cache cut; design-endorsed ("bind natively") but a real per-discovery cache invalidation worth quantifying.
7. **Heuristic verify evidence** — graph.py:1015-1027. Tests judged by string-matching `"PASSED"`/`"[exit 0]"` in the last `terminal_exec`; fragile and gameable (self-acknowledged as fixture-pending).

### What this slice does well

- **Failure containment is systematic**: overflow→force-compact with retry cap (267-271), compaction failure→warn-and-skip (761-777), 3-denial breaker (404), 4-tier stuck watchdog (680-703), critic iteration cap→blocked-escalation (937-947) — every loop has a bounded exit.
- **Fail-closed approval gate**: malformed `edited_allow` denies (440-445), unknown verdicts deny (454-456), ONE interrupt per execution keeps replay semantics clean (459-465).
- **Interrupt/clarify state is checkpointed and per-run** (H-10, H-01 fixes), so human-in-the-loop survives container replacement — the core Moat-1 guarantee.
- **Budget is felt, not enforced** (env line + threshold reminders, never auto-stop) — textbook Moat 4/5.

I've read both files completely. Now let me analyze them against the 7 moats.

Let me think about each moat:

**Moat 1: Harness never enters unrecoverable state**

Evidence FOR:
- Graph compiled with checkpointer (graph.py:1087-1115, docstring 26-28). Checkpointer required for interrupt/resume surviving container replacement.
- Node failure paths: agent_node catches exceptions → returns {"error": err, "done": True} (272). Compaction failure → skips cycle, warns, doesn't kill turn (761-777). Context overflow → force-compact retry with cap _MAX_COMPACTION_RETRIES=1 (81, 267-271).
- Interrupt-driven approval gate survives container replacement via checkpointer (367-376).
- Watchdog force-stop at 12 (699-702), denial circuit breaker at 3 (404-406, 462-464), critic iteration cap (937).
- LangGraph checkpoints after each node (super-step) — with Postgres checkpointer, crashed runs resume from last checkpoint.

Concerns:
- agent_node: on error, `done: True` and `error: err` — the run ends, but checkpoint is durable, so it can be resumed/replayed. Is "done+error" recoverable? The state is durable; a human can intervene. Partial deltas may have been emitted but not checkpointed — the AIMessage is only added on success. Mid-stream failure loses the partial message but state remains consistent. OK.
- However: budget accounting — if an exception happens after tokens were consumed but before accounting, budget undercounts. Minor.
- Potential issue: `_publish_events` catches sink exceptions and only logs (1127-1130) — events lost if sink fails. The philosophy says "No silent loss, ever" re: session retention; events are survivors ("DB events"). A sink failure drops events with only a log warning — the event stream (a survivor per Moat 1: "Survivors are branches/PRs, DB events, knowledge, and the session volume") can have gaps. This is AT RISK territory. Though they fixed fire-and-forget (H-01), the catch-and-log still drops events. But arguably the node's state update is checkpointed, so the graph doesn't corrupt; the event stream has a hole.
- Another: approval_gate_node uses `interrupt(payload)` — if the broker is None... wait, line 432-435: payload falls back if broker is None, but `decision.get(...)` — decision could be non-dict if resumed with wrong type? Line 439 `decision.get("decision", "deny")` — if decision is not a dict, AttributeError propagates → node exception → LangGraph fails the run. But interrupt resume is controlled by the runner. Edge case.
- tools_node: `call_tool_direct` — exceptions? Line 585: if the tool raises, does it propagate? Depends on call_tool_direct. If it raises, the node crashes mid-loop: some tool calls already appended to new_messages but not returned — the checkpoint is only written at node end, so a crash loses the whole node update and re-executes the node on resume → tools re-executed → duplicate side effects! That's a Moat 1/2 concern: node-level checkpointing means a crash mid-tools-node re-runs ALL tool calls in that batch. Tool execution isn't idempotent (terminal_exec, file_edit). This is a real hazard: LangGraph checkpoints between super-steps, not within a node. A crash in the middle of tools_node (e.g. the 3rd of 5 tool calls) causes re-execution of calls 1-2 on resume. That's an at-least-once execution hazard. AT RISK.
- Unbounded recursion guard: agent<->compaction loop guarded by compaction_retries. agent->tools->compaction->agent loop: turn-based, unbounded until done — normal agent loop, fine.
- `build_graph(checkpointer: Any = None)` — the parameter DEFAULTS TO NONE (1087)! Docstring says "The checkpointer is REQUIRED" but the signature permits None, and compile with checkpointer=None means NO checkpointing — interrupts wouldn't work at all (interrupt() requires a checkpointer; LangGraph raises without one when interrupt is used). Actually with interrupt(), if no checkpointer, LangGraph raises an error at runtime. So in production it's passed; in tests MemorySaver. The default None is a footgun but documented. Minor.

**Moat 2: Conflict-free concurrency**

- EngineState is per-run TypedDict flowing through the graph; state isolation via checkpoint thread_id.
- ask_user pending questions: H-10 fix — per-thread keyed store, snapshotted into state (610-614, 122-126 in state.py). Good.
- BUT: `terminal_manager()` (665) is a process-global singleton — shared across concurrent runs in one process. completed_notifications() drains a GLOBAL manager — notifications from run A could be delivered to run B's messages if runs share a process. Is terminal_manager per-thread keyed internally? Can't tell from this slice. Line 666: `terminal_manager().completed_notifications()` — no thread_id argument! That looks like cross-run contamination risk. Compare with get_pending_questions(tid) which is keyed. AT RISK — needs verification but as written, no scoping key.
- `emitter._pending_tools` (629) — the EventEmitter comes from config["configurable"]["emitter"], presumably per-run. If shared, mutable `_pending_tools` dict is shared. Likely per-run.
- `mcp_manager()` (187) global singleton — shared MCP catalog; reads only (status), fine-ish.
- Module-level `_TOOL_ARG_ALIASES` — read-only, fine.
- `_read_prompt` reads from disk per call — fine.
- `tuning` from config — if shared across runs in a process, `observe_error`/`observe_healthy_turn` mutate shared tuning state. Unknown scope.

**Moat 3: Swarm bounded fan-out**

- This slice: graph has no fan-out nodes. spawn_agent/spawn_swarm are TOOLS (referenced in _tool_title 736-737), executed elsewhere via call_tool_direct. The graph itself doesn't spawn. So NOT APPLICABLE for fan-out within graph — the cap is enforced at the platform/capacity-semaphore layer, not here. No unbounded spawn paths in this slice (tools node executes tool calls sequentially but bounded by the model's tool_calls list). One note: tools_node loops over tool_calls sequentially with no bound — model could emit many tool calls in one turn; each executed serially. Bounded by model output, not a fan-out hatch.

**Moat 4: Token intelligence (no classifier/gate)**

- No classifier. Tool filtering by mode (`tools_for_mode`, `mode_allowed`) — is that a "gate"? It's capability scoping (which tools are bound), not content censorship of reads. Mode binding is a permission/UX layer, and mode-denied discovered tools drop out at bind (183-184). This is tool gating, not read gating — and moat 4 is about reading content. NOT APPLICABLE / PASS. Note: permission ruleset DENY short-circuits before the card (397-399) — that's a hard policy gate on ACTIONS, consistent with design (hard git policies). Fine.
- Budget reminders visible to agent (119-121 budget line in env envelope) — supports moat 4/5's "cost visible" requirement. PASS.

**Moat 5: Cost via smallest reads, visible budget**

- Budget used/cap in the per-turn envelope (118-121) — visible in the loop. 50%/80% reminders (324-343), never auto-stops — "a human holds the kill switch, by design" — aligned.
- Compaction: prune->summarize->splice with token trigger — keeps context small.
- Concern: `_budget_reminder` iterates (0.80, 0.50) and returns on first crossing — if a single turn jumps from 40% to 85%, only the 80% reminder fires (50% skipped) — fine, it returns the higher one. OK.
- estimate_cost + gateway hard cap mentioned (287). Aligned. PASS.

**Moat 6: Max prompt caching**

- Frozen system prompt built fresh each turn via `_read_prompt("system_prompt.md")` (91-94) — reads from DISK EVERY TURN (84-88). If the file changes mid-run (deploy), the prefix changes → cache invalidation silently. Should be read once / frozen. Also `_read_prompt` returns "" if missing (86-87) — silently empty system prompt! A missing file = empty system message = silent cache+correctness failure. That's a hazard.
- Envelope is a HumanMessage at the END of messages (234-236) — dynamic content (turn count, budget) below the cut. Aligned with moat 6: dynamic below.
- BUT: envelope is appended AFTER the conversation each turn but it's "NEVER persisted" — rebuilt per turn; conversation messages are checkpointed. The order: [system] + messages + [envelope]. System first, dynamic last. Tool definitions: bound per-turn via _bound_tools — discovered_tools GROW the tool list mid-run (182-197). Adding a tool changes the tool definitions → in Anthropic-style caching, tools are part of the prefix → cache invalidated on each discovery. The philosophy item 6 says "Dynamic tools / subagents — static shape, dynamic contents. The shape stays above the cache cut; only the contents move below it." Here discovered tools bind natively (changing the actual tools array), not a static-shape/dynamic-contents shim. That's arguably a moat-6 AT RISK: each tool_search discovery busts the prefix cache for subsequent turns. Though the design says "discovered tools bind natively" is the intended mechanism (roster fragment is below the cut; bound tools above). Hmm — the docstring at 140-141 says the roster fragment (deferred tools) rides in the envelope (below cut, cheap), and discovered tools bind natively (above cut, changes prefix). This is a deliberate trade-off but does invalidate cache on discovery. AT RISK.
- System message is re-read per turn — also a perf nit (disk read per turn).
- Messages accumulate full conversation; compaction prunes. Compaction REWRITES history (splices summary) → cache invalidation by design on compaction. Inherent, not a violation.

**Moat 7: Parallel by default**

- tools_node executes tool calls SEQUENTIALLY in a for loop (519+): `await call_tool_direct` one at a time. Parallel tool calls from the model (Anthropic emits parallel tool_use blocks routinely) are executed serially. No asyncio.gather. This is the big moat-7 question: is tool-call serialization justified? Justifications visible: watchdog streak updates per call, event ordering, approval state. But independent read-only calls (file_read, file_search, memory_search) could gather. Currently serial → latency. AT RISK / arguable VIOLATION of "parallel by default" — the only legitimate serializer is the capacity semaphore; a sequential for-loop over independent tool calls is "serialization that is suspect until proven necessary." The loop mutates shared accumulators (streaks, new_messages, out) but could restructure to gather-then-merge. I'd call it AT RISK leaning VIOLATION with the caveat that event ordering may justify it (not documented as a deliberate decision).
- Event publishing is awaited inline per tool (624) — serializes on the sink each call. Fine for ordering, small latency.
- The rest of the graph is inherently sequential (agent→gate→tools→compaction). That's the turn structure, not a violation.

**Other findings / code quality:**
- `_read_prompt` silently returns "" on missing prompt file (86-88) — system prompt or envelope silently absent. Hazard: silent degradation.
- `emitter._next` and `emitter._pending_tools` — private member access throughout (331, 621, 707, etc.) — encapsulation smell, but consistent.
- Dead code: `_entry_route` etc. fine. `state.get("context_id", state["thread_id"])` fine. `compacted_event_ids` in state (101) — is it ever written in this slice? Not in graph.py — maybe compaction.py. OK.
- TODOs: I saw no TODO/FIXME in these files. Comment at 1015-1018 says fixture "hardens this into the real evidence contract" — heuristic evidence extraction (test detection by string matching "PASSED"/"[exit 0]") is fragile — verify critic can be gamed/false-negative. Note it.
- `tools_node` on crash mid-loop: partial side effects + checkpoint only at node end → re-execution duplicates side effects (noted above, moat 1).
- `budget` dual-shape handling (Budget | dict) is defensive debt, fine.
- `goal_router_node` intake: `_first_user_story` returns "" if no user message found (996) → make_goal("") — empty goal artifact, no error path. Edge case: goal mode with no user-tagged message creates an empty goal. Minor.
- `_after_gate` reads `needs_approval` with autonomy; consistent.
- Interrupt: `decision.get("decision", "deny")` — fail-closed to deny. Good.
- `agent_node` error path sets done+error but doesn't emit an event about the error? _should_continue routes to end; the runner presumably handles. Fine.
- Line 272: `return {"error": err, "done": True}` — the AIMessage from partial stream is discarded; state stays consistent (last message is HumanMessage) — recoverable via resume/new turn. OK.
- Compaction node failure path: returns needs_compaction False — if messages still overflow context, next agent call will overflow again → force_compact → compaction_retries increments in failure path (777) — capped at 1 → then agent returns error+done. Recoverable end-state. Good.
- `with_gateway_retry_aiter(lambda: _aiter(llm, messages), max_retries=2)` — retries only stream start. Documented H-11. OK.
- state.py: `tag_message` coerces string origins — fine. `PromptOrigin(origin)` raises ValueError on unknown — acceptable.
- state.py `Budget.would_exceed` unused in graph.py (graph computes manually) — maybe used elsewhere.
- EngineState `total=False` — nodes get partial state; fine.

Also: **Moat 2 — shared `terminal_manager()`** with no thread key at line 665-666: `terminal_manager().completed_notifications()`. If terminal_manager is a process-global with per-terminal tracking, notifications might be keyed by terminal id... but the drain isn't scoped by thread_id — any run in the process drains ALL completed notifications into ITS message stream. Cross-run contamination of messages (not .git, but session state). AT RISK, flag for verification by the tools-slice agent.

Also the `log.warning("event sink failed")` — events are a Moat-1 survivor (DB events). Dropping them silently (log only) is a durability gap; arguably should retry or dead-letter. AT RISK on moat 1.

Also `_build_turn_envelope` accesses `config["configurable"].get("workspace")` — config required keys ("model", "emitter") raise KeyError if missing → node crash → run fails but checkpoint durable. Fine.

Graph builder: checkpointer default None — in production must be passed; compile without checkpointer + interrupt() → runtime error on first approval. Docstring covers it. Minor footgun.

Ranking top findings by severity:
1. tools_node mid-loop crash → whole-node replay re-executes already-executed tool calls (non-idempotent side effects: file_edit, terminal_exec) — at-least-once execution hazard (Moat 1). Line 519-585, checkpoint granularity.
2. Sequential tool execution (no gather) — Moat 7: parallel tool calls serialized without documented justification (519-585).
3. Global terminal_manager drain unscoped by thread (665-668) — cross-run notification contamination risk (Moat 2).
4. Event-sink failures swallowed with log-only (1127-1130) — DB events are survivors; silent gaps in event stream (Moat 1 durability).
5. System prompt re-read from disk every turn + silent "" on missing file (84-94) — cache-prefix drift + silent degradation (Moat 6/1).
6. Discovered tools bound natively grow the tool definitions mid-run → prefix cache invalidation per discovery (182-197) (Moat 6) — though design-endorsed ("bind natively"), so maybe AT RISK note.
7. Heuristic verify evidence string-matching (1015-1027) — fragile; documented as fixture-hardened-later.

Now the verdicts:

| 1 | AT RISK | checkpointed graph (1087-1115, 26-28), interrupt survives replacement (367-376), compaction-failure containment (761-777), overflow-retry cap (267-271, 80-81) BUT mid-node crash replays executed tools (519+), sink failures drop events (1127-1130) |
| 2 | AT RISK | per-run state, H-10 per-thread fix (610-614, state.py 122-126); but unscoped global terminal_manager drain (665-666), shared tuning/mcp_manager singletons |
| 3 | NOT APPLICABLE | no fan-out in this slice; spawn tools execute elsewhere (736-737) |
| 4 | PASS | no classifier/gate; mode-binding is capability scope not read censorship (170-198); permission DENY is action policy by design (396-407) |
| 5 | PASS | budget visible in-turn (118-121), 50/80 reminders never auto-stop (324-343), compaction keeps context small (745+) |
| 6 | AT RISK | envelope below cut (107-167, 234-236); but system prompt re-read per turn w/ silent-empty fallback (84-94), discovered tools mutate prefix (182-197) |
| 7 | AT RISK (lean VIOLATION) | tool calls executed serially, no gather (519-585); per-call awaited event publish (624) |

Word count: keep under 600. Dense evidence. Let me write it.


agent2:

Key observations:

1. **SWARM_MAX_SLICES = 8** — the philosophy says the swarm hard cap is **100**. Config drift: worker caps fan-out at 8 simultaneous slices. The philosophy says "100 (current limit)". This is either intentional worker-side sub-cap or drift. Notably philosophy says cap bounds blast radius; an 8-slice per-thread cap may be a per-thread cap vs 100 global. But there's no capacity semaphore acquisition in this slice — the runner never acquires a capacity slot before spawn; `is_saturated` is a local in-memory check in a per-process registry (not a distributed reservation). The philosophy's moat 3 says "Capacity reservations close the check-then-act race: N threads spawned concurrently would otherwise pass N cap checks before the first Thread row exists. Reservations are held between try_acquire and row insert." In the worker slice, the registry is in-process memory — concurrent spawns from the same thread run in one process, but the check `is_saturated` then register is not atomic across processes. Since it's one process per thread and spawn calls are within the single event loop, check-then-act within one thread's loop is effectively serialized by the event loop (single-threaded asyncio). But across the fleet, there is no capacity semaphore integration in the runner — no acquire/release at all. So "release == commit" is NOT APPLICABLE to the runner itself, but the fan-out path has only a local veto, not a distributed reservation. That's likely owned by the backend (Agent 1's slice?), but the spawn path in fanout.py uses in-memory registry — worth flagging: the swarm cap enforced here is 8, not 100; and no cross-process capacity reservation is visible from this slice.

2. **Moat 1 — wait-for-exit before remount**: Not visible in this slice. The runner has resume-on-restart logic (`runner.py:266-271`: `snap = await graph.aget_state(config); fresh = not snap.values`) which supports container replacement, and pending approvals survive restart (`runner.py:193-199`). But there's no wait-for-exit poll before mounting the session volume — that's container orchestration, likely in the backend. Within this slice: NOT APPLICABLE for volume mount orchestration, but the restart-resume correctness is good evidence.

3. **Signal handling**: `runner.py:514-515` registers SIGTERM/SIGINT handlers that set `runner._stop`. But there's a subtle bug: setting `_stop` exits the idle loop (`while not self._stop.is_set()`), but if the runner is mid-turn inside `_invoke_with_approvals` → `graph.ainvoke(...)` or `broker.wait_decision(...)`, nothing interrupts the await. SIGTERM during a long turn or during an approval wait will NOT stop the runner until the turn completes or the approval times out (up to 900s). The container orchestrator will likely SIGKILL after its grace period — that's abrupt kill; checkpoint persists (kill is immediate, checkpoint preserves everything per docstring line 27-28: "Kill is immediate (process exit; the checkpoint preserves everything)"). Actually the docstring says kill is immediate process exit — but the SIGTERM handler just sets `_stop`, which is only checked in the idle loop and background loops. So the "kill is immediate (process exit)" claim in the docstring is inaccurate for the in-turn case: during a turn, SIGTERM is effectively ignored until turn end. Also `_control_pump` kill path sets `_stop` but same issue: the main loop is in `_run_turn` and won't observe `_stop` until the turn finishes. Actually wait — the control pump runs concurrently; on kill, `_stop.set()` is called, but `_run_turn`/`_invoke_with_approvals` never checks `_stop`. The `while not self._stop.is_set()` loop is only the idle loop. So kill during a running turn waits for turn completion. The docstring says "Kill is immediate (process exit...)" — documented v1 posture is nudge at turn boundary; kill is claimed immediate but implementation only stops at turn boundary too. This is a moat 1 concern: orphaned compute burning budget after kill; and if the orchestrator SIGKILLs, the finally-block cascade drain (spawn registry drain) never runs → spawned subagents outlive parent, contradicting the cascade-drain contract the code itself fixed (runner.py:302-306 comment).

4. **Moat 1 — finally block robustness**: `runner.py:294-325` — cancels tasks, gathers, drains spawns, closes forwarder/broker/control, episodic. Good. But if SIGKILL arrives (orchestrator grace period), none of this runs. Also `except Exception` at line 289 returns 1, then finally runs — good cleanup on error path.

5. **Episodic memory** at `mirror_dir / f"{thread_id}-episodes.db"` (runner.py:245) — mirror_dir default `./checkpoints` (runner.py:88). If mirror_dir is a local container path and workspace is shredded, checkpoint mirror + episodic db die with container unless CHECKPOINT_MIRROR_DIR points at the durable session volume. The env docs (line 21-22) say CHECKPOINT_MIRROR_DIR optional. Session durability story: Postgres checkpointer is durable; DeltaChannel JSONL mirror and episodic SQLite are local unless env points them at the session volume. The philosophy says survivors are branches/PRs, DB events, knowledge, and session volume. If mirror dir isn't on the session volume, the "PHI-grade replay fallback when Postgres is unavailable" is lost on workspace shredding — a moat 1/2 risk worth flagging (depends on deploy env wiring not visible in this slice).

6. **Moat 2 — session volumes / cross-contamination**: runner keys checkpoint by `context_id = resume_context_id or thread_id` (runner.py:99). Per-lane mounts are orchestration-level; not in slice. The fresh-registry reset (M-13, M-14 at runner.py:108-118) addresses cross-run contamination within a reused process — good, moat-aligned. DeltaChannel keyed by thread_id/context_id (runner.py:259) — per-lane.

7. **Heartbeat**: `forwarder.heartbeat` sets TTL key ex=90 (forwarder.py:69) — heartbeat TTL means a dead container's heartbeat expires in 90s → detectable death. Good for moat 1 (harness can detect dead threads and, presumably, wait-for-exit elsewhere).

8. **Moat 7 — parallel by default**: startup in `run()` spawns 4 background tasks concurrently (runner.py:248-251) — parallel. Shutdown cancels + gathers concurrently (295-301) — parallel. No serialization beyond the event loop. main.py similar. One thing: `_invoke_with_approvals` services interrupts strictly serially (interrupts[0] only, line 207) — if multiple parallel graph branches interrupt simultaneously, they're serviced one at a time. Within a single thread that's inherent to human approvals; not a violation per se. Also `publish_events` pipelines (forwarder.py:31-38) — batched, good.

9. **Control channel — pub/sub loss**: control.py uses Redis pub/sub for kill/nudge — pub/sub is fire-and-forget; a kill published while the listener is reconnecting (backoff loop, control.py:60-63) is silently lost. The philosophy's durable-events leg uses streams for events (forwarder.py docstring: "pub/sub loss would silently hole replay") — but control messages (kill!) ride lossy pub/sub. A kill during a Redis blip is dropped; the thread keeps running until idle TTL or orchestrator kill. Moat 1/2 hazard: "The failure is visible and ordered, never silent" — a lost kill is silent. AT RISK finding.

10. **sessions.py fork_session**: private API, pinned SDK 0.2.128 (pyproject.toml:30), guarded by smoke test — degraded-UX story documented. Fine. Note dead-code risk: sessions.py + normalize.py + main.py SDK runtime are legacy ENGINE=sdk path kept "through the RE hardening soak, then the seam is cut" (main.py:245-246) — documented dead-code-soon. Also `worker.approvals.ApprovalBridge` imported in main.py:38 — outside slice.

11. **Dead code/TODOs**: No explicit TODOs in slice. `EngineRunner._pending_nudges` fine. `_main_sdk` path is deliberate legacy. `runner.py` imports `Path` etc fine. `self.delta_channel = DeltaChannel(self.mirror_dir)` — used in MirroredSaver. Note comment at runner.py:255-259 says "Previously constructed but never wired" — now wired. Good.

12. **pyproject.toml config drift**: python >=3.12,<3.13 pinned; langgraph pins exact; `langchain-openai>=1.4,<2` ranges vs exact pins — mixed pinning; comment says "Pins are deliberate. Bump only with the gate green". `collegium-contracts` unpinned (no version) — drift hazard for the event contract the whole event pipeline depends on. Also `websockets>=13,<16` listed but no import of websockets in this slice — possibly used elsewhere in worker (checkpointer postgres? no). Flag as potential dead dep in this slice's view.

13. **Moat 4/5/6**: token intelligence — runner has Compactor, SelfTuningLimit, budget surfaced via turn events (`result.get("last_usage")` in turn_boundary, runner.py:339-343). No classifier/gate in this slice. Budget cap from env BUDGET_USD with Budget(cap=5.0 default). Budget visibility to the agent happens in engine internals (other agents' slices). Prompt caching: runner assembles prompts via graph (out of slice) — NOT APPLICABLE mostly; note `f"Workspace root: {self.workspace}\n\n{self.task_prompt}"` is the HumanMessage (dynamic content correctly in user message, not system prefix — moat 6 aligned).

14. **Recursion limit 80** (runner.py:178) — hard-coded; on hitting it, ainvoke raises → caught by run()'s except → thread failed. Not silent.

15. **Moat 1: orphan processes** — terminal_exec tool processes are in tools/ (out of slice). Cascade drain handles spawns. The runner itself doesn't spawn subprocesses except handoff.git_checkpoint via subprocess.run (synchronous, blocking the event loop! handoff.py:40-50 uses subprocess.run inside what would be async contexts — but is write_handoff/git_checkpoint even called by the runner? Not in this slice. If called from async code it blocks the loop — flag minor). Actually grep: runner doesn't call handoff. It's a library; callers elsewhere (backend or tools). Can't confirm blocking-in-async here; note it.

16. **idle TTL default mismatch**: runner.py:89 default 900s; main.py ThreadConfig default 600s (main.py:65). Config drift between the two runtimes (documented env table in runner docstring says default 900; main.py docstring doesn't state default). Minor drift.

17. **approval_timeout_s** int vs idle_ttl float — trivial.

18. **Moat 3 check**: `SWARM_MAX_SLICES = 8` in fanout.py vs philosophy cap 100. And saturation check is in-process; no reservation between check and registration visible... within one event loop, check-then-act can't race (no await between? need to check fanout spawn function, but out of slice). I'll flag: runner slice shows only a local in-memory registry; distributed capacity reservations (moat 3's check-then-act closure) must live in the backend — not evidenced here; and the 8-slice cap contradicts the 100 cap unless it's a documented per-thread sub-cap.

19. `_control_pump` on kill: sets `_stop` BEFORE heartbeat (runner.py:427-435) with the stop-first doctrine — good; but kill during a turn still waits for the turn (see #3). Also note `runner.py:326` returns `0 if self.status != "failed" else 1` — kill sets "stopped" → exit 0. Good.

20. Signal handlers: `loop.add_signal_handler(sig, runner._stop.set)` — same turn-boundary issue. And in `_main_sdk` (main.py:251-252) same pattern. Also SDK runtime: SIGTERM sets `_stop`, but `client.receive_messages()` in the pump keeps running until the turn ends; the run loop checks `self._stop.is_set()` every 0.25s (main.py:113,133) — wait, the while loop breaks when `_stop` set... then finally cancels tasks — pump cancelled mid-receive. That's more immediate than the custom runner's turn-boundary-only kill. Actually for main.py sdk runtime, the `while not self._stop.is_set()` in run() IS the wait loop, so kill exits the loop and cancels the pump. For the custom runner, `_run_turn` is awaited inside the idle loop, so kill waits for the full turn + approvals. Asymmetry worth noting: custom engine kill is NOT immediate despite the docstring claiming "Kill is immediate (process exit)".

21. Moat 5: budget cap default 5.0; budget status emitted in turn bookkeeping (runner.py:14-15 "budget status"). Budget.visible to agent via state. OK — runner passes budget dict into state (runner.py:143). Aligned. The SDK path: `max_budget_usd` backstop with gateway authoritative (main.py:88) — hard cutoff by SDK is arguably "silently cut off mid-thought"? It returns is_error ResultMessage → status failed → surfaced. Fine-ish.

22. Checkpointer: MemorySaver fallback when DATABASE_URL unset (dev/test) — checkpoints in memory → container replacement loses everything in dev mode; documented. Fine.

23. runner.py `_invoke_with_approvals` — after `graph.ainvoke(Command(resume=decision))` loops back to re-check state. Solid. Edge: `started` set after invoke; if graph completes with no interrupts on first pass and `snap.next` empty — returns. Resume-on-restart with pending interrupts serviced before invoking (runner.py:193-206) — excellent moat 1 alignment.

Now verdicts:

- Moat 1: AT RISK — strong resume/checkpoint/cleanup design, but kill/SIGTERM not honored mid-turn (docstring claims immediate), SIGKILL skips cascade drain (spawn orphans), control channel lossy pub/sub can drop kill silently, mirror/episodic durability depends on env wiring.
- Moat 2: PASS (within slice) — per-lane keying (context_id, per-thread channels), M-13/M-14 fresh registry + ContextVar scoping prevent cross-run contamination; volume mounting/wait-for-exit is orchestration (out of slice) — mark PASS with scope note. Actually let me mark AT RISK? The moat-2 items in-slice are good; cross-contamination protections exist. Session volume handling is out of slice. I'll say PASS (in-slice) with note that mount/wait-for-exit is unverifiable here.
- Moat 3: AT RISK — SWARM_MAX_SLICES=8 vs philosophy 100 cap (config drift); capacity reservation/check-then-act closure not visible in slice; in-memory registry only. The runner does drain/finish properly (release-side hygiene: spawn_done → finish; cascade drain). But no semaphore acquire/release in runner — not its job presumably. Verdict AT RISK (drift + unverifiable reservations).
- Moat 4: NOT APPLICABLE (no classifier/gate; free tool access gated only by permissions ruleset — wait, COLLEGIUM_PERMISSIONS deny rules! runner.py:56-68 — a deny ruleset for tools like `git push *`. Is that a "classifier/gate" violation of moat 4? Moat 4 is about READ admission/classifiers deciding what the model may SEE, not tool execution permission. Permissions deny mutating tool calls — that's a safety capability map, not a reading gate. Not a moat 4 violation; arguably moat 1 alignment. Note it explicitly.)
- Moat 5: PASS — budget in state (143), usage surfaced per turn boundary (339-343), overrun → failed+surfaced; human kill switch via control channel. SDK backstop documented as non-authoritative.
- Moat 6: NOT APPLICABLE/PASS — prompt assembly lives in engine/prompts (out of slice); dynamic run data correctly placed in HumanMessage (130-133) not system prefix. I'll mark N/A with positive note.
- Moat 7: PASS — 4 background tasks spawned concurrently (248-251); gather-based concurrent shutdown (295-301); pipelined Redis publishes (forwarder.py:31-38, 63-66); nudges queued without serializing the turn. Approvals serial per-interrupt is inherent. No illegitimate serializers in slice. Maybe note handoff.py subprocess.run blocking if called from async (unverified caller).

Top findings ranked:
1. Kill/SIGTERM not honored mid-turn in custom engine (runner.py:283-288 loop only checks _stop between turns; _run_turn/_invoke_with_approvals never poll it; docstring line 27 claims "Kill is immediate"). Burn budget after kill; SIGKILL then skips cascade drain → orphaned spawns contradicting the drain contract (302-306).
2. Control channel uses lossy Redis pub/sub (control.py:31-74) — kill/nudge published during reconnect backoff (60-63) is silently dropped; philosophy demands failures visible, never silent. Events use durable streams but kill does not.
3. SWARM_MAX_SLICES=8 (fanout.py:48) vs philosophy's 100 cap — config drift or undocumented per-thread sub-cap; no capacity reservation between check and register visible (in-memory registry).
4. DeltaChannel mirror + episodic SQLite under mirror_dir default ./checkpoints (runner.py:88,245) — if not mapped onto the durable session volume by deploy env, the "PHI-grade replay fallback" dies with the shredded workspace.
5. IDLE_TTL default drift: 900 (runner.py:89) vs 600 (main.py:65); pyproject leaves collegium-contracts unpinned (pyproject.toml:20) despite exact-pin doctrine (line 8); websockets dep unused in slice.
6. Legacy SDK runtime + normalize.py + sessions.py = documented dead code awaiting seam cut (main.py:245-246); sessions.py depends on private SDK API (sessions.py:24-29) — pinned+guarded, fine, but it's on the default? No — cas extra only. OK.
7. heartbeat ex=90 good. EngineRunner.status "starting" never set in custom engine — main.py sets "starting" (line 77), custom runner starts at "running" (runner.py:122). Trivial.

Strengths:
- Resume-on-restart correctness: pending interrupts serviced BEFORE re-invoke so approval IDs aren't orphaned (runner.py:193-206); fresh-vs-continue detection (266-271); resumed-into-completed graph handled (273-280).
- finally-block discipline: cancel→gather before closing Redis (295-301); cascade drain of spawns (306); per-resource close.
- M-04 nudge failure isolation (388-399), M-06 heartbeat/watchdog blip tolerance (470-497), control listener reconnect loop with backoff (control.py:40-63).
- Durable events leg via streams + TTL heartbeat (forwarder.py:28-38, 68-72).
- M-13/M-14 fresh registry + ContextVar thread scoping against cross-run contamination (108-118).

Word budget: under 600 words. Dense. Let me write the report with absolute paths abbreviated as worker/... — the instructions say return file paths as absolute paths in final response. I'll use full absolute paths in evidence but that's verbose; I'll use absolute paths at least once per finding. Maybe use `worker/worker/engine/runner.py` style with the note that root is /home/sahil/My-Projects-Portfolio/z-agent. To be safe and comply, I'll write evidence as absolute paths but compact (they're long). Compromise: give absolute path in the findings' file:line refs. In the table, short relative refs keep it readable, and I'll state the root prefix once. The guideline says return file paths as absolute paths in your final response — I'll include absolute paths in the top findings section and use repo-relative in the table with root stated.

Now final_update then response.


agent3:

## Agent 3: LLM, Compaction, Memory & Prompts

### Moat verdicts

| Moat | Verdict | Evidence (file:line) | Notes |
|---|---|---|---|
| 1 Harness never unrecoverable | **PASS with gaps** | compaction.py:299-315, graph.py:1118-1130 | Honesty validator rolls back on identity-loss; tool-pair repair prevents terminal gateway 400s. But event sink failure is log-and-continue (events lost, run proceeds). |
| 2 Conflict-free concurrency | **PASS** | memory.py:165-178 | Episodic store scoped via ContextVar per run (fixes cross-run leak); per-thread SQLite. |
| 3 Swarm fan-out | **N/A (prompt-aligned)** | system_prompt.md:57-66 | Depth-one, brief-fully, swarm-only-call rules taught in prompt; no cap logic in this slice. |
| 4 Token intelligence, no gate | **PASS (no gate) / taste partially encoded** | system_prompt.md:25,29,84; graph.py:140-147 | No classifier/admission layer anywhere. Deferred-tool roster ≤0.5K tokens. But the §4 golden chain (symbol → grep → smallest range) is NOT in the prompt. |
| 5 Cost visible, human kill switch | **STRONG PASS** | graph.py:116-121, 324-343; goal.md:10 | Budget `$used/$cap` in per-turn env block; 50/80% reminders "Never auto-stops"; gateway is hard cap, human holds kill switch. |
| 6 Max prompt caching | **PARTIAL / AT RISK** | graph.py:91-94, 232-236, 170-198; llm.py:269-276; metrics.py | Static system prompt first, dynamic envelope appended LAST (below cut) — correct. But discovered tools mutate the tools array mid-run (prefix drift); no cache-hit metrics; cost ignores cached-token discounts. |
| 7 Parallel by default | **PASS (prompt-level)** | system_prompt.md:26 | "Batch independent calls… parallel execution is strongly preferred" taught explicitly. |

### Top findings (ranked)

1. **System prompt promises a skills list that is never injected** — system_prompt.md:88 says skills "are listed at the end of this system prompt," but `_build_system_message()` (graph.py:91-94) reads the file verbatim and ends at the communication section. Skills are unreachable in-prompt (moat 6 prompt/content drift; agent is told to follow pointers that don't exist).
2. **Compaction contradicts the prompt's survival guarantee** — system_prompt.md:115 promises "errors encountered, approvals granted, edits made" always survive, but compaction.py:37-42 protects only SYSTEM/USER/NUDGE and prunes TOOL, ENVELOPE, MEMORY, ASSISTANT wholesale. With `summarizer=None` (the documented default, compaction.py:138-141), pruned state leaves only "[compacted] N messages pruned" — pure loss, no summary (moat 1/4).
3. **Tool-list drift invalidates the cache prefix on every tool discovery** — `_bound_tools` appends discovered tools per turn (graph.py:180-197); the tools array sits above the message cache cut, so each `tool_search` load silently re-prices the whole prefix. §6 item 6 sanctions dynamic *contents*, not a growing definitions array (moat 6).
4. **Cache blindness: no metrics, wrong cost math** — metrics.py has no cache-hit counter (moat 6 failures are silent by design, so undetectable), and `estimate_cost` (llm.py:269-276) bills all input tokens at full price, never reading cached-token details — the agent's felt budget signal overstates cost on cache hits (moat 5/6).
5. **Event persistence is not fail-closed** — `_publish_events` awaits inline (good ordering, graph.py:1118-1128) but swallows sink exceptions after a log line; the turn commits state while the durable event record is silently dropped — replay/audit gap (moat 1).
6. **Emitter seq may reset per turn** — EventEmitter is "re-instantiated per turn" yet `_seq` starts at 0 (events.py:24-33); the comment's claim of monotonicity via `compacted_event_ids` is not implemented in this file. Cross-turn seq collisions would corrupt event ordering (moat 1, needs verification against runner).
7. **Compaction gate runs on a 4-chars/token estimate** (compaction.py:150-170) despite real gateway usage being available in state (`last_usage`, graph.py:293) — compaction can trigger ~30% late, breaching the soft limit the design says it pre-empts (moat 5).

### What this slice does well

- **Frozen static prefix done right**: verbatim system message first, all per-run/per-turn dynamics (thread id, budget, turn count, mode envelope, roster, goal stage) ride in one transient HumanMessage appended last, never persisted (graph.py:107-110, 232-236).
- **Honest compaction**: origin-based selection (never a dump), identity-based validator with full rollback, tool-pair repair in both directions, `[compacted]` marker (compaction.py).
- **Fail-closed LLM layer**: no model substitution, capability registry omits params fixed models reject, stream-start retry closes hostage connections (llm.py:281-322, 164-214).
- **Budget as felt feedback**: env-block budget line + event reminders + goal-mode teaching to "compact scope when budget reminders fire" — judgment with feedback, exactly §5.
- **Prompt quality**: trust-boundary and approval sections are exceptional; envelopes are tight, mode-scoped, and teach verification-over-claims throughout.

I have enough evidence. Let me compose the report.

Key findings:

**Moat 6 (caching):**
- System message: `_build_system_message()` reads system_prompt.md verbatim, ONE per turn, first in messages (graph.py:91-94, 232-233). Static per run. Good.
- Dynamic envelope appended at END of messages (graph.py:234-236) — below the cache cut. Env block contains turn count, budget, thread id — dynamic data correctly below cut. GOOD design.
- BUT: envelope is appended AFTER the conversation — as conversation grows, each turn's messages list changes anyway below the cut. Fine.
- Tool binding: `_bound_tools` order = tools_for_mode(mode) + discovered_tools in state order (graph.py:170-198). When the agent discovers a tool mid-run via tool_search, the tool list grows → tool defs change → full cache invalidation of the system+tools prefix. Tools in the OpenAI API are a request-level param sent before messages; any change to the tools array invalidates the cached prefix. Discovery is the design (§6 item 6: dynamic tools — "static shape, dynamic contents. The shape stays above the cache cut; only the contents move below it"). The implementation puts discovered tool DEFINITIONS into the tools array (which is above the cut / prefix), so each discovery invalidates the prefix. This is a real moat-6 concern but partially sanctioned by philosophy item 6. However the deferred-tool roster is designed precisely to avoid binding all tools; appending discovered tools still drifts the prefix. Worth flagging.
- Also no explicit `cache_control` / prompt-caching hints anywhere; gateway (LiteLLM) + Azure Foundry: prompt caching is automatic for long prompts on OpenAI models, but for kimi/qwen via LiteLLM caching support is dubious. No verification of cache-hit rate in metrics (no cache_tokens counter). Moat 6 says failure is silent — metrics.py has no cache-hit counter, llm.estimate_cost ignores cached-input discounts (input_tokens includes cached tokens; no usage.get("input_token_details") handling → cost OVERestimated, cached reads not credited). Finding: estimate_cost doesn't account for cached tokens (llm.py:269-276).
- system_prompt.md line 88: "skills available to this thread are listed at the end of this system prompt" — but _build_system_message reads the file VERBATIM with no skills appended! The prompt promises a skills list at the end, but the file ends with the communication section. Skills are NOT injected into the system message. That's a bug: system prompt references a list that doesn't exist. Either the runner appends skills elsewhere (grep found nothing in graph.py) — flag as finding (prompt/tool drift; also if skills WERE appended per-thread, that would drift the prefix per thread — but per-run it's static, acceptable).

**Moat 4 (no classifier):** No gate/classifier anywhere in the slice. Selectivity taught via prompt: system_prompt.md:25 (specialized tools), tool roster ≤0.5K tokens, memory recall deliberately (line 84). BUT: the prompt does NOT explicitly teach the golden-taste chain "symbol lookup → grep → smallest range" nor teach against full-file dumps. It says prefer code_search/file_read, truncation footers. Partial.

**Moat 5 (cost visible):**
- budget line in per-turn env block (graph.py:116-121) — agent sees "$ used of $ cap" EVERY turn. In-loop. 
- 50%/80% reminders as STATUS events (graph.py:324-343), "Never auto-stops". Human kill switch — gateway is hard cap (comment line 286-287). Aligned.
- goal.md:10 "Budget is real... compact scope when budget reminders fire" — teaches response to budget.
- Compaction: selects by origin, never dumps; honesty validator with identity check + rollback (compaction.py:299-315). Protected origins system/user/nudge never pruned. Summary marked "[compacted]".
- Concern: `estimate_cost` conservative default (2.0, 6.0) for unknown models — fine.
- Compaction trigger uses cheap 4-chars-per-token estimate (compaction.py:150-170) — should_compact gates on estimate, not real gateway usage; comment admits "real count comes from gateway" but the gate doesn't use it. Could compact late/early by ~30%. Also block-content extraction. Minor finding.
- Prune-only default when summarizer is None: drops messages with only a marker "[compacted] N messages pruned (no summarizer)" — LOSSY: if the summarizer isn't wired, tool results/assistant reasoning are dropped with NO summary. Docstring says summarizer "wired by self-tuning" — but SelfTuningLimit doesn't wire a summarizer; grep shows no summarizer wiring in this slice. Potential moat-1 loss: state the harness needs (approvals granted, edits made) could be pruned if recorded only in assistant/tool messages. The system prompt claims "errors encountered, approvals granted, edits made" always survive compaction (line 115) — but compaction.py prunes ASSISTANT and TOOL origins wholesale; approvals live in... approval decisions are events; are they in messages as envelopes? Envelopes are pruned (ENVELOPE origin in prune order, popped on exit). So the system prompt's promise "approvals granted always survive" is NOT guaranteed by compaction.py — protected origins are only SYSTEM/USER/NUDGE. That's a real moat-1/moat-4 inconsistency: prompt lies about guarantees. High finding.

**Moat 1:**
- Compaction rollback on validator failure — good. Forced path guard. Tool-pair repair both directions — prevents gateway 400 terminal failure. Good.
- Events: `_publish_events` awaits sink inline (graph.py:1118-1130) — durable order, but sink failure is only logged (log.warning) and swallowed — events lost on sink failure while the run continues; not fail-closed. The event is acted on (state updated) even if persistence failed. Flag.
- memory.py: EpisodicMemory record() commits synchronously — durable. FTS fallback LIKE with parenthesization fixed. ContextVar scoping prevents cross-run leak. Good.
- `_repair_tool_pairs` edge: strips tool_calls from AIMessage — keeps content. Good.

**Moat 2/3/7:** mostly N/A for this slice; system prompt teaches swarm depth-one, parallel batching ("Batch independent calls", line 26 — moat 7 aligned in prompt). Moat 2: memory ContextVar per-thread (memory.py:165-173) prevents cross-run contamination — aligned. EventEmitter re-instantiated per turn but seq monotonic via... comment says via compacted_event_ids; but self._seq starts at 0 per instantiation (events.py:33) — comment claims monotonic "via the state's compacted_event_ids" — unclear; if re-instantiated per turn with _seq=0, seq would restart per turn unless restored elsewhere. Potential event-ordering concern; flag as question.

Wait — events.py:24-26: "re-instantiated per turn so seq is monotonic across the thread's lifetime via the state's compacted_event_ids". If it's re-instantiated per turn with _seq=0, seq is NOT monotonic across turns unless restored. The comment is confusing — possible bug: seq collisions across turns. Flag as medium finding (event ordering = replay correctness, moat 1).

Also events.py:93: `sdk_uuid = getattr(msg, "id", None) or getattr(msg, "usage_metadata", None) and None` — operator precedence oddity: `a or (b and None)` → if msg.id is None, result is always None. Probably intended but convoluted; minor.

from_tool_result pairing: graph.py:625-629 comment says from_tool_result never called in production (tools_node emits directly) — dead code path; pending_tools popped manually. Minor.

Token visibility: usage passed in turn_boundary event detail (events.py:163-172) — visible to team.

System prompt golden taste: line 25 specialized tools; truncation footer teaching (line 29); "Recall deliberately" (line 84) teaches against reflexive memory_search. But no explicit "smallest line range" / "never dump whole file" teaching in system prompt. §4 says the norm "lives in the frozen system prompt... symbol lookup, then grep, then the smallest line range" — the prompt does NOT contain that chain. Finding (moat 4/5 gap: taste not fully encoded).

Skill list missing (line 88 vs _build_system_message verbatim) — finding.

Envelopes: ask/plan read-only teaching aligns with moat 5 (investigate deeply but no dumps — ask.md:8 "no unsolicited file dumps"). Good.

Budget in env block is per-turn dynamic — below cut (envelope at end). Correct per moat 6.

Metrics: no cache-hit metrics — silent cache regression undetectable, violating moat 6's "caught by review" only. Finding (low-medium).

llm.py moat 1: fail-closed on missing gateway env (never substitute model) — aligned. with_gateway_retry_aiter closes abandoned streams — connection pool safety, aligned.

Now the verdicts table. Keep under 600 words.


agent4:

I've read all 6 files plus the philosophy. Let me analyze carefully against the moats.

**Moat 3 (Swarm — bounded fan-out, hard cap 100):**
- Philosophy says hard cap of **100**. fanout.py has `SWARM_MAX_SLICES = 8` (line 48). That's a much tighter cap — 8, not 100. This is arguably a *stricter* bound (not a violation of "bounded", but contradicts the "100 cap is the current limit" design — though a lower cap is still bounded; the moat says "up to a hard cap of 100 (current limit)" — the code caps at 8, which is more conservative. Not a moat violation per se but a divergence from the stated current limit).
- **CRITICAL: No capacity reservation mechanism.** The philosophy explicitly says: "Capacity reservations close the check-then-act race: N threads spawned concurrently would otherwise pass N cap checks before the first Thread row exists. Reservations are held between `try_acquire` and row insert, so N concurrent spawns see each other." In fanout.py, there is NO `try_acquire`, NO capacity semaphore, NO row insert. The check is `_veto(req, live=_registry.live_count())` — a read of an in-memory dict — followed by `_registry.register(...)`. Between check and register there's no lock. Two concurrent `spawn_swarm` calls (both in executor threads!) could both read live_count=0 and both register 8 slices each → 16 > 8. The check-then-act race is WIDE OPEN. Also the tools are sync @tool objects run via run_in_executor — so they run in a thread pool, meaning concurrent invocation is real, and `SpawnRegistry` has no lock. `_registry.register` mutates a dict from multiple threads without a lock (GIL makes individual dict ops atomic, but the check-then-act sequence is not).
- Also: spawn_swarm registers all slices in a loop — the veto happens once before the loop, so within one call it's fine, but concurrent calls race.
- Also the spawns are registered but "the actual spawn is async; the tool returns the spawn handle" — the actual thread creation happens elsewhere. So the registry entry is the reservation-like thing, but there's no semaphore.
- Spawns concurrent or sequential? `_staggered_spawn` exists (line 177-182) wrapping coroutines with stagger delays — but it's NEVER CALLED anywhere in this file. The spawn_swarm tool registers synchronously in a loop and returns; the actual async spawn happens elsewhere (dispatcher). The stagger is defined but unused in this slice. The docstring says stagger 2s. Philosophy moat 7 says "Swarm blueprints spawn Explorers concurrently, never in sequence" — a 2s stagger is a deliberate serialization NOT the capacity semaphore → potential moat 7 tension, though it's justified as thundering-herd protection on the gateway. The philosophy says "Any other serialization in the codebase is suspect until proven necessary" — the stagger has a justification comment (gateway 429 thundering herd), so it's a "decision" — but worth flagging.

**Moat 2 (conflict-free concurrency, per-repo write lock before spawn):**
- fanout.py has NO per-repo write lock. `SpawnRequest.repo` exists but nothing checks whether another writable thread is on the same repo before spawn. CollisionRadar in watchdogs.py is explicitly "warn-only" — "It WARNS only — it does not refuse the spawn (that's the engine-side veto in fanout.py)" — but fanout.py's veto does NOT check repo overlap! The docstring in watchdogs.py line 12-13 claims the refusal lives in fanout.py, but `_veto` only checks worker_idle and cap — never repo. So two writable threads CAN land on one repo, and the only thing that fires is a warn-only radar. This is a CRITICAL moat 2 violation: the per-repo write lock is not enforced before spawn in this slice; the claimed enforcement point (fanout veto) doesn't implement it.
- Also `_registry` is module-level global shared across runs in one process — `live_count()` counts spawns across ALL runs/threads in the process, not per-run. The comment says "the engine owns one per run" (line 130) but it's actually a module global. So the cap applies process-wide, and `drain(parent_thread_id)` is keyed by parent so that's OK, but the saturation gate is process-wide — cross-run coupling (H-10-style bug class they fixed elsewhere but not here). Actually the comment at line 130 says "A module-level registry (the engine owns one per run)" — contradictory. reset_registry exists for tests. In production with concurrent runs in one process, the registry is shared: run A's spawns count against run B's cap. That's a moat 2 cross-contamination concern and also makes the cap wrong.

**Moat 1 (never unrecoverable):**
- Watchdogs: DriftWatchdog fires a signal, does NOT auto-stop (good — human decides). BudgetWatchdog: reminders at 50/80%, never auto-stops (aligned with moat 5: human holds kill switch). IdleGate: surfaces only. CollisionRadar: warn-only. CriticRubric: block findings route to blocked-escalation (human gate). All watchdog actions are non-destructive — they don't kill mid-write. Good for moat 1.
- BUT: `enforce_timeout` in fanout.py (line 270-277): the 2h hard cap sets `sp["status"] = "timed_out"` — docstring at top says "A thread exceeding it is drained (its work is committed, its status set to timed_out)" — but the actual implementation ONLY flips a status flag in an in-memory registry. There's no commit, no drain of actual work, no waiting on container exit. The status flip doesn't stop anything — the actual thread keeps running? The registry is just bookkeeping. So the 2h cap is a paper tiger: it marks timed_out but doesn't actually drain/commit. Moat 1 concern: "drained (its work is committed...)" is claimed but not implemented here. Also killing mid-write risk is absent precisely because it doesn't kill anything.
- Cascade drain (`drain`) similarly only flips status flags — actual stopping is elsewhere. In-memory only: if the process crashes, the registry vanishes — spawn state is not durable. Moat 1: harness state lives only in process memory (module-level dict). Survivors should be DB events; the registry is not durable. But this may be a spike-level concern.
- `quarantine_file`: moves a file mid-write? It intercepts writes to sensitive paths — `shutil.move` — if the quarantine move fails (disk full), QuarantineError raised — is that recoverable? It's a typed error, fine. But moving a file out from under a running process could break things; it's a mutation-level safety gate, acceptable.
- `reset_registry` cancels watchdogs — good (prevents leaked tasks firing against wrong spawn ids).
- `arm_watchdog`: creates task on loop — if the loop closes before cancel, task leaks; finish() cancels. OK.
- hardening.py: `run_soak` catches all exceptions → is_error=True, recoverable. `evaluate_slo` — `p95_turn_latency_s or 999` — if p95 is 0.0 (unlikely) → 999; fine. Note `s[int(len(s) * 0.95)]` — for len(s)=20, int(19.0)=19, ok; for len(s)=100, int(95.0)=95, ok; but for small lists e.g. len=1, int(0.95)=0 fine. Actually index could be out of range? len*0.95 < len always for len>0, int() floors, so max index len-1 when... int(len*0.95) <= len-1 iff len*0.95 < len, always true. OK.
- hardening.py line 155-156: `e.detail.get("ok")` — earlier guarded with `(getattr(e, "detail", None) or {})` but here `e.detail.get` without guard — comment at 138-141 says post-processing must guard, and tool_events list does guard, but lines 155-156 don't — an event with detail=None in first10/last10 raises AttributeError, escaping run_soak. Minor inconsistency: the guard applied at 142-144 but not 155-156. Actually tool_events were filtered by `(getattr(e, "detail", None) or {}).get("tool")` — an event with detail=None fails that filter (None.get → wait, `(None or {})` → `{}`, `.get("tool")` → None → falsy → excluded). So tool_events all have truthy-ish detail? No — `(getattr(e,"detail",None) or {})` means events with detail=None are evaluated with {} and excluded since {}.get("tool") is None. So all tool_events have detail dicts? Not necessarily — an event could pass if detail is a dict with "tool" key. If detail is None it's excluded. So by construction, tool_events have non-None detail. So 155-156 safe. OK.

**Moat 7 (parallel by default):**
- The 2s stagger (SPAWN_STAGGER_S) is a serializer other than the capacity semaphore — justified as thundering-herd protection, and `_staggered_spawn` still launches all coroutines concurrently (they all start, just delayed) — so it's concurrent-with-stagger, not sequential. But `_staggered_spawn` is never called in this file. The actual spawn path isn't visible here.
- spawn_swarm registers in a sequential loop but that's just registry bookkeeping (microseconds).
- goal_mode: linear stage pipeline — INTAKE→CLARIFY→EXPLORE→PLAN→IMPLEMENT→VERIFY→REBASE_GATE→PR. Stages are inherently sequential (a pipeline), which is fine — stages depend on each other. Within stages, nothing forces serialization. Goal mode doesn't itself spawn — no capacity checks in goal_mode.py at all. If goal mode spawns threads (e.g., explore via subagents), there are no capacity/reservation hooks here. The goal budget is "the SUM of all threads spawned for the goal" (docstring) but no enforcement code here.
- Goal mode: no human approval gates inside pipeline — but fanout docstring says "ONE-APPROVAL BATCH: a swarm decomposition is approved as ONE batch (the human sees all slices, approves once)". In goal mode (fully autonomous), does swarm spawn still require approval? Potential conflict: goal_mode says NO human approval gates inside the pipeline; fanout says swarm requires one-approval batch. If goal mode uses spawn_swarm, either the approval gate blocks autonomy (goal mode violation) or it's bypassed (fanout veto violation). Unresolved tension visible from this slice.

**Moat 4 (token intelligence — no classifier on reads):**
- security.py: `redact()` runs on tool OUTPUTS leaving the sandbox (events, deltas, approvals) — that's egress redaction, not read-gating of model input. Wait — "every tool output that leaves the sandbox (events, deltas, approvals) is run through redact()". Does the model see redacted output? If redaction applies to events/deltas/approvals (the human-facing surfaces), the model's own context is untouched — that's egress safety, not a read gate. But if tool outputs are redacted before entering the model's context, that IS a gate on what the model may see. The docstring says "leaves the sandbox (events, deltas, approvals)" — events/deltas are UI-facing. Ambiguous; flag as needs-verification. The moat 4 question: is there a classifier deciding what the model may see? `_SENSITIVE_FILENAMES` — "Sensitive filenames whose contents are redacted wholesale" — if the model reads .env and gets «REDACTED», that's a read gate (moat 4 violation, though a defensible security one — secrets redaction is a standard harness-safety control, and moat 4 is about intelligence gating, not secret hygiene; the philosophy says "no classifier, no gate, no admission layer deciding upstream what the model may see" — redacting secrets from model reads would literally be a gate deciding what the model may see). The set is defined but where is it applied? `is_sensitive_path` is used by quarantine (writes) — the "contents redacted wholesale" comment suggests read-path application somewhere else. In this slice, only quarantine_file (write interception) uses is_sensitive_path. So in-slice: redaction is egress + write-quarantine = mutation/egress gates = acceptable harness safety. But flag the wholesale-content-redaction comment as a potential read gate depending on where applied.
- permissions.py: glob rulesets on tool calls — `deny` on `file_write` with `.env*` — that's a MUTATION gate (file_write, terminal_exec commands) — action-level permission on writes/commands = acceptable per the audit framing ("gating mutations = harness safety"). Does it gate reads? The ruleset mechanism is generic — `tool: "*"` could match read tools (file_read, grep). Nothing in permissions.py restricts it to mutating tools. A ruleset COULD deny file_read on paths — that would be a read gate. The mechanism permits read-gating even if the shipped examples are mutation-focused. Also `decision_for_call` with `capability_default_needs_approval` — read tools presumably default allow. So: mechanism is read/write-agnostic; current examples gate mutations; risk exists. Verdict: mostly aligned (mutation gates), with a caveat that the generic mechanism can gate reads and `_SENSITIVE_FILENAMES` wholesale redaction hints at read-path gating elsewhere.
- Injection boundary markers (wrap_untrusted) — wraps untrusted content, neutralizes nested markers. That's provenance tagging, not gating — the model still sees everything. Aligned with moat 4 (free to read, with provenance). Good.

**Moat 5 (cost):**
- BudgetWatchdog: 50/80% reminders, never auto-stop — exactly moat 5's "budget overrun is surfaced, not silently enforced; human holds the kill switch." Strong alignment.
- GoalArtifact has budget_usd=20.0, cost_usd — goal budget envelope. Visible. OK.

**Moat 6 (prompt caching):**
- goal_mode STAGE_ENVELOPES: "per-turn fragments, never part of the system message... rendered transiently" — dynamic per-turn content below the cache cut, NOT in the frozen system prompt. Explicitly aligned (comment at 223-225). Good.
- hydrate_orientation prepends AGENTS.md diff to first user message (dynamic, below cut). Fine.

Now the check-then-act race detail: where is try_acquire, where is row insert? In THIS slice: neither exists. `_veto` reads `_registry.live_count()` (fanout.py:201-202, 225-226), then `_registry.register` inserts (fanout.py:207, 233). Between them: no lock, no reservation, and the actual Thread row insert happens elsewhere (async, after tool returns — "The actual spawn is async; the tool returns the spawn handle"). So the window between cap check and durable row insert is enormous (spans an async boundary). The philosophy's required "capacity reservations held between try_acquire and row insert" is absent in this slice — the registry entry could serve as a reservation, but (a) it's not locked, (b) it's process-local in-memory, (c) register happens synchronously right after veto within the same call for the slices loop — actually within one spawn_swarm call, veto then immediate register of all slices in a loop, so intra-call it's atomic-ish (no await between), but the tools run in executor threads (per arm_watchdog docstring: "they execute in a worker thread"), so two concurrent spawn_swarm calls in two threads CAN interleave between live_count() read and register. GIL doesn't save a check-then-act. Race confirmed.

Also: `is_saturated` default cap=SWARM_MAX_SLICES=8 — used as worker_idle proxy. And the 100 cap from philosophy vs 8 here: the philosophy says 100 is the current limit; this slice implements 8. Divergence worth noting (maybe other slices implement the 100 capacity semaphore; this slice's 8 is a swarm-width cap, arguably a different thing — swarm width per spawn call vs global capacity 100. But `_veto` applies it to the RUNNING TOTAL of live spawns, so it functions as a global in-process spawn cap of 8, far below 100).

Watchdog mid-write risk: none of the watchdogs kill anything — all warn/surface only. enforce_timeout flips status only. So no mid-write kill risk in-slice; the risk is the opposite — watchdogs that don't actually act (timeout doesn't drain). Moat 1: recoverable, yes, but the 2h cap's claimed "committed" drain is unimplemented in-slice.

Also goal_mode `needs_clarification` heuristic — a keyword heuristic deciding whether to ask — that's not a read classifier; it's interaction flow. Fine. Though "A later layer may add an LLM judge" — fine.

Hardening: run_load_test spawns N concurrent threads via asyncio.gather — parallel, good (moat 7 aligned in test harness). No cap on num_threads but it's a test tool.

Let me also double check fanout for "repo" — SpawnRequest.repo is never used in _veto. Confirmed: repo param accepted by spawn_agent but only passed to SpawnRequest, never checked. CollisionRadar claims refusal is in fanout.py — false. Critical finding.

Also `_registry` shared across concurrent runs (module global) — cross-run contamination of the cap + drain keyed by parent_thread_id is OK, but live_count is global. And `reset_registry` exists only for tests — in prod, registry grows forever? spawns dict never removes finished entries — memory leak-ish, and live_count only counts running so cap ok, but dict grows unboundedly (minor).

Word budget: under 600 words. Dense. Let me write the report.

Verdicts:
- Moat 1: PARTIAL/PASS-with-gaps — watchdogs non-destructive (good), but 2h timeout drain is a status-flip only; registry is in-memory-only (process crash loses all spawn state).
- Moat 2: FAIL — per-repo write lock not enforced before spawn; CollisionRadar warn-only and its claimed enforcement point (fanout veto) has no repo check. Two writable threads can land on one repo.
- Moat 3: FAIL (critical) — no capacity reservation; check-then-act race open between live_count() read and register; no try_acquire/row insert in slice; cap is 8 not 100.
- Moat 4: PASS-with-caveat — no read classifier in-slice; redaction is egress; permissions gate mutations; but mechanism is read/write-agnostic and _SENSITIVE_FILENAMES "contents redacted wholesale" hints at read-path gating applied elsewhere.
- Moat 5: PASS — BudgetWatchdog surfaces 50/80%, never auto-stops; goal budget envelope visible.
- Moat 6: PASS — stage envelopes explicitly per-turn/transient, never in system message.
- Moat 7: PARTIAL — 2s stagger is a non-semaphore serializer (justified, and _staggered_spawn is concurrent-with-delay, but unused in-slice); goal pipeline stages sequential by design (OK); swarm registration loop sync.

Top findings ranked:
1. Moat 3 race: fanout.py:201-207, 225-234 — veto reads live_count, then registers; no lock/reservation; tools run in executor threads so concurrent spawn_swarm calls interleave; actual Thread row insert is async elsewhere (line 208), so the check-to-durable-row window spans an async boundary. The philosophy's required reservation mechanism is absent.
2. Moat 2 missing write lock: _veto (152-174) never inspects req.repo; watchdogs.py:12-13 claims refusal lives in fanout.py — it doesn't. CollisionRadar (watchdogs.py:87-103) is warn-only. Two writable threads per repo possible.
3. Cap divergence: SWARM_MAX_SLICES=8 (fanout.py:48) applied to running total (162) functions as a global spawn cap of 8, not the philosophy's 100.
4. Module-global registry shared across runs (fanout.py:131): live_count/is_saturated couple concurrent runs in one process; spawns dict never evicted (unbounded growth).
5. 2h timeout doesn't drain: enforce_timeout (270-277) only flips status; docstring (20-21) claims "work is committed" — no commit/stop path in-slice; spawn keeps running.
6. Goal-mode vs one-approval-batch tension: goal_mode docstring (6-8) forbids human gates in-pipeline; fanout docstring (22-23) mandates one-approval batch for swarms — unresolved if goal mode spawns swarms.
7. _staggered_spawn defined but never called (177-182).
8. Permissions mechanism can gate reads (permissions.py:44-59 tool:"*" matches read tools) — currently used for mutations; needs convention guard.

Strengths:
- Budget watchdog = moat 5 textbook (surface, human kill switch).
- Drift/collision/idle watchdogs non-destructive — no mid-write kill risk (moat 1).
- Critic block → blocked-escalation human gate.
- Stage envelopes below cache cut (moat 6).
- wrap_untrusted neutralizes forged boundary closes (security, non-gating provenance).
- permissions fail-closed on shell-metachar glob injection (permissions.py:52-56), malformed rules never crash a turn (75-81).
- M-15 budget double-fire fix, H-10 per-thread question keying — evidence of concurrency fixes.

Update step and final summary. Keep under 600 words.


agent5:

I've read all the files completely. Let me analyze against the moats.

**Moat 4 (token intelligence — free reader, no gate):**
- file_read: full reads available on demand — offset/limit with default limit 2000 lines (`_MAX_READ_LINES = 2000`, readonly.py:28, 38). No classifier blocking reads. Reads return line-numbered text with footer `[N lines; M total]` — visible truncation signal (line 60). Good.
- file_search: ripgrep-backed, no gate on pattern; errors surface (M-12 fix, lines 90-97). Output capped at 16KB (`_BASH_MAX_OUTPUT`, line 89) — truncation is silent-ish: `out = proc.stdout[:_BASH_MAX_OUTPUT]` then appends `[N matches]` count — the count is computed on the truncated output, so if truncated, the match count reflects truncated data, and there's no explicit "truncated" marker. Actually `out.count(chr(10))` counts newlines in the truncated output — so the footer says e.g. "[150 matches]" but that's the count of lines in the truncated output, not total matches. Silent signal loss — the agent can't tell if output was cut at 16KB. That's a finding.
- terminal_exec read-only variant: this IS a gate on the read side — `_gate_command` blocks commands not on the read-only allowlist (readonly.py:164-184). But this is a safety gate for the read-only *variant* of terminal_exec; the mutating variant goes through approval. It's a command gate, not a content gate — it doesn't decide what the model may *see*, it decides what commands may run in the sandboxed path. Arguably moat 4 is about reading content; blocking `cat` chaining etc. is a security gate, not a content classifier. But note: `cat` is allowed, so full-file dump via cat is possible. However, chaining is blocked — `head -n 50 file` is allowed. Fine.
- No read-side classifier on content anywhere. web_search/web_fetch results are `wrap_untrusted` — quarantine marking, not censorship. Good.

**Moat 5 (cost — smallest chunk, visible truncation, budget):**
- file_read defaults: offset=1, limit=2000. The default limit is 2000 lines — that's a large default; the "smallest relevant chunk" path is available but the default is generous. Is full-file dump the path of least resistance? `file_read(path)` with defaults reads up to 2000 lines — for most files that IS a full dump. So the default call is effectively a dump for files <2000 lines. The awkwardness asymmetry the philosophy wants (targeted reads easier than dumps) is only partially present: offset+limit exists and is easy, but the default is a 2000-line dump, not a small window. Moat 5 says "targeted reads are the path of least resistance... a full-file dump is the awkward, effortful path." Here a full-file dump is the *default* path (one arg). That's a moderate finding — though arguably offset/limit affordance + prompt taste is the mechanism. There's no symbol lookup tool in this slice (no ctags/LSP symbol tool) — the philosophy's golden chain is "symbol lookup → grep → smallest line range". The tool surface has grep (file_search) and line-range read, but NO symbol lookup tool. That's a gap vs moat 4's "symbol lookup, then grep, then the smallest line range". Worth flagging.
- Truncation visibility: file_read footer shows `[N lines; M total]` — visible, good. file_search: 16KB cap with no truncation marker — silent. terminal_exec: 16KB cap, `[exit N]` footer but no truncation marker — silent. file_glob: 500-cap with `[N files]` — if capped at 500, shows "[500 files]" with no "more exist" marker — silent truncation.
- Token cost of tool results: not surfaced in this slice (no token counts in outputs). Budget visibility is presumably elsewhere (out of slice), but tool results themselves don't carry token costs. Note it.

**Moat 6 (frozen prefix):**
- Tool definitions: langchain `@tool` objects with static docstrings/schemas — defined at module import, deterministic. DEFAULT_TOOLS is a static list (8 names), ordering stable. `tools_for_mode` iterates `default_tool_names(mode)` — list order deterministic. Good.
- Two-tier: Tier 0 bound every turn (8 tools), deferred tools discovered via tool_search and merged into `state.discovered_tools` by the agent node. Discovered tools are appended — where? "The agent node adds state.discovered_tools on top" (line 206). If discovered tools are appended below Tier-0 in the tool list, the prefix stays stable as long as Tier-0 is first and ordering of discovered is stable. But discovery order depends on model behavior — discovered set can grow turn-to-turn, which mutates the tool list below the tier-0 prefix. Per the philosophy item 6: "Dynamic tools / subagents — static shape, dynamic contents. The shape stays above the cache cut; only the contents move below it." So deferred discovery with static shape is the design; whether the harness places them below the cache cut is in the agent-node slice (not mine), but the mechanism here supports it: roster_fragment is a bounded (1800-char) fragment — presumably injected into prompt. Roster is sorted (line 170: `sorted(visible_index(mode).items())`) — deterministic ordering. Good.
- BUT: `build_index()` iterates `ALL_BUILT_TOOL_BY_NAME.items()` — dict insertion order, deterministic at import. `visible_index` filters. `search` sorts by (-hits, name) — deterministic. Good.
- MCP: `build_index` folds in `mcp_manager().catalog()` — the catalog depends on runtime connection state (`st.connected`). So the index contents vary with MCP runtime state. Roster fragment includes MCP entries only "after a catalog snapshot" (line 173-178). If the roster fragment is part of the frozen prefix, runtime-dependent MCP catalog contents would drift the prefix — but the design says MCP server *definitions* are frozen (item 5) and dynamic tools have static shape/dynamic contents. MCP tools are never bound by default (mcp.py:8-10) — they fold into the discovery index. So MCP tool schemas never enter the bound tool list unless discovered — good for cache. But the roster fragment content changes when MCP connects/disconnects — if the roster is in the system prompt (frozen), that's drift. The roster is explicitly a "fragment" with a char budget — likely injected per-turn below the cut. Can't fully verify placement from this slice; flag as a question/risk: roster_fragment is deterministic given a fixed catalog, but catalog is runtime-state-dependent (mcp.py:166-168 filters on `st.connected`), so roster contents can change turn-to-turn as servers connect/fail. Also `roster_fragment` calls `visible_index(mode)` which calls `build_index()` which calls `mcp_manager().catalog()` — fresh each call. Deterministic ordering (sorted), dynamic contents.
- One more moat-6 concern: `tool_search` results return full JSON schemas as tool *output* (discovery.py:219-223) — contents below the cut via tool result, shape static. That's exactly the prescribed pattern. Good.
- Schema stability: `_tool_schema` uses pydantic `model_json_schema()` — deterministic per version. Docstrings static. Good.

**Moat 1 (never unrecoverable):**
- All dispatchers catch exceptions and return typed error dicts: readonly.call_tool (245-246), deferred.call_deferred_tool (164-166), __init__._call_extra_tool (313-314), mcp.call (162-164). Tool failures return recoverable errors, never crash. MCP partial success: one server's failure never fails the batch (mcp.py:52-59, 95-99). Retry with backoff, stale-client eviction (M-19). Lazy auth failures contained. Excellent.
- file_read on huge files: `p.read_text` reads the WHOLE file into memory before slicing (readonly.py:52-58) — a 10GB file would OOM the worker. No size guard. That's a moat-1 robustness gap (and ironic vs moat 5: the implementation always pays full-file cost even when serving a 50-line window). Real finding.
- file_glob: os.walk with no depth/dir pruning — could walk node_modules forever; capped at 500 matches but walk continues? No — breaks at 500 (lines 132-135). OK, but it breaks out — fine. Symlink outside workspace skipped (115-118). No followlinks=False specified — os.walk default followlinks=False, fine.

**Moat 7 (parallel by default):**
- Read tools are sync functions run via `loop.run_in_executor` (readonly.py:235, deferred.py:159, __init__.py:297) — non-blocking, so parallel tool calls can run concurrently in the executor. terminal_await is async. MCP calls async with wait_for. Nothing serializes reads. Multiple read tool calls in one turn can fan out — supported structurally. Good. (Whether the agent node issues parallel tool calls is out of slice.)

**Moat 2/3:** mostly out of slice. Moat 2: read tools are workspace-scoped, read-only — no cross-thread writes. `_resolve` allows absolute paths anywhere (readonly.py:210-214) — file_read can read ANY path on the host (/etc/passwd, other threads' workspaces). Note: file_delete contains to workspace (M-10) but file_read does NOT. Is that a moat-2 violation (cross-contamination of sessions)? Reading another lane's files isn't corruption, but it breaks isolation/confidentiality between engineers' runs. Worth flagging as a moat-2-adjacent finding: `_resolve` permits absolute-path escape for reads; the delete path was fixed (M-10) but the read path wasn't. Moat 4 says "free to read ANYTHING" though — so read freedom is philosophically aligned; the tension is with per-lane isolation. I'll note it under moat 2 as neutral/observation.

Moat 3: spawn tools referenced but out of slice; watchdog arming in _call_extra_tool (C-04 fix) noted.

Now verdicts per moat:
1. PASS (strong) — typed recoverable errors everywhere; one gap: file_read reads whole file into memory (OOM risk on huge files).
2. N/A-ish / observation — read tools don't write; absolute-path reads escape workspace isolation (file_read can read other lanes), delete contained but read not.
3. N/A in slice — spawn dispatch + watchdog arming present (__init__.py:284-311).
4. PASS — no read-side content classifier/gate; full reads on demand; grep-then-read affordances. Gaps: no symbol-lookup tool (philosophy's chain starts at symbol lookup); terminal_exec read-only variant is a command allowlist gate (security, not content — acceptable but note).
5. PARTIAL — offset+limit exists and footer makes read truncation visible; BUT default limit=2000 makes full-dump the one-arg default path (dump is the path of least resistance for most files); file_search/terminal_exec 16KB caps truncate silently (no truncation marker, match count computed post-truncation); file_glob 500-cap silent; no token-cost surfacing in tool results.
6. PASS with risks — static schemas, stable ordering (sorted roster, deterministic search tie-break), two-tier with static shape/dynamic contents; MCP fold-in only via discovery (never bound by default). Risk: roster fragment contents depend on MCP runtime connection state (catalog filters on connected) — if roster sits above the cache cut, connect/disconnect drifts the prefix; also discovered-tools merge ordering is in the agent node (out of slice).
7. PASS — all reads dispatched via run_in_executor/async; no serializer.

Top findings ranked:
1. (Moat 5, high) file_read default limit=2000 = full-file dump as the zero-effort default; philosophy wants dump to be the awkward path. readonly.py:28,38.
2. (Moat 5, high) Silent truncation: file_search caps stdout at 16KB with no "truncated" marker and the `[N matches]` footer counts only the truncated text (readonly.py:89,100); terminal_exec same 16KB silent cap (readonly.py:206); file_glob 500-cap shows "[500 files]" indistinguishable from exactly-500 (readonly.py:132-138).
3. (Moat 1, medium) file_read slurps entire file into memory before slicing — huge file OOMs the worker; also pays full read cost regardless of window (readonly.py:52).
4. (Moat 4, medium) No symbol-lookup tool — the golden chain "symbol → grep → line range" is missing its first rung; only grep + line-range exist.
5. (Moat 6, medium) MCP catalog in index/roster is runtime-state-dependent (connected filter, mcp.py:168; discovery.py:98-104,176-178) — roster contents can change turn-to-turn as servers flap; fine if roster is below the cut, drift if frozen.
6. (Moat 2, low) file_read absolute-path escape reads outside workspace (readonly.py:210-214) — free-read aligned (moat 4) but breaks per-lane isolation that file_delete was hardened against (M-10).
7. (Moat 6, low) tool_search direct dispatch hardcodes mode="development" (call_tool_direct, __init__.py:251-254) — a call in ask/plan mode would index dev-mode tools, potentially exposing mode-denied tool schemas. Fail-closed bypass.

Wait, let me check #7: `call_tool_direct` for tool_search passes `mode="development"` and `bound=default_tool_names("development")` — regardless of the actual run mode. So in ask mode, tool_search would search the development-mode visible index, which includes file_edit, file_write, spawn_agent etc. — tools that are mode-denied in ask. The discovery result returns full schemas and marks them "discovered". Whether the agent node then binds them is another matter (mode_allowed is checked at binding per the docstring "the single fail-closed check used by binding, the discovery index, and the roster" — but here discovery index is built with hardcoded development mode). This is a real fail-closed bypass in the direct dispatch path. Good catch — severity medium-high for moat 1/4 (it's a gate correctness issue; mode gates are a safety feature not a read classifier, so it weakens the mode fail-closed contract).

Also note strengths: M-12 (rg error surfacing), M-19 (stale client eviction), C-02 (mutating dispatch precedence), C-04 (watchdog arming), M-14 (contextvars propagation), M-10 (delete containment), partial-success MCP, idempotency keys, deterministic ordering everywhere (sorted roster, (-hits, name) sort), alias resolution L-04.

Word budget: under 600 words for final response. Let me write it densely.


agent6:

Two more quick checks — whether `ApprovalGate` (the non-interrupt variant) is actually wired anywhere, and how the tools node handles intra-batch execution.

Good. Now I have a good picture. A few more checks:

1. Is there a write-lock check in mutating tools? No — grep for write.?lock found nothing in worker. Mutating tools trust the spawn-time capacity semaphore. Also `_resolve` only enforces workspace containment.

2. Checkpointing granularity: LangGraph checkpoints at super-step boundaries (after each node). Tool execution happens inside `tools_node` — a checkpoint is written after the tools node completes, not between individual tool calls or before mutation. So if the run dies mid-tools-node (e.g., after file_write touches disk but before checkpoint), on resume LangGraph re-executes the tools node from the last checkpoint → mutation may re-apply (at-least-once semantics for tool execution). Actually, LangGraph's `aput_writes` persists task results incrementally via `put_writes` — writes (tool results) are persisted as pending writes, so a completed tool call's ToolMessage may be persisted before the node completes... Actually in LangGraph, when a node raises mid-way, partial writes to state via `put_writes`... the tools_node returns a dict at the end, so writes happen at node completion. If the process dies mid-node, the node's returned state is lost, but the file mutation on disk already happened. On resume, the node re-executes → file_edit with read-before-edit hash guard would REFUSE on retry (hash mismatch since the file already changed) — that's actually a clever idempotency-ish property: expected_hash refusal gives "at-most-once-ish" behavior on naive replay but surfaces as an error the agent must re-read and retry. But file_write create-new would error "already exists". So replay is detectable but not seamless — recovery depends on the agent re-reading.

Wait — but do tool calls inside a tools_node batch run sequentially? From the graph code: `tools_node` iterates tool calls in a for loop — sequential within a turn. And importantly, pending writes: LangGraph's Pregel persists task results via aput_writes as tasks finish. For a single node task, if it crashes mid-way, no writes are persisted (writes are returned at end). So the checkpoint is AFTER the whole batch — meaning a crash between mutation N and node end replays mutations 1..N. file_edit hash guard catches this; terminal_exec does NOT — a shell command re-executes blindly (e.g., `git commit` runs twice, `npm install` twice). That's at-least-once with no idempotency guard for terminal_exec.

3. Approval durability: ApprovalBroker path uses interrupt() → checkpoint persisted → runner waits on Redis BLPOP → resume. Pending approval survives restart (the interrupt is in the checkpoint). On resume after container replacement, the gate node re-executes from the checkpoint, re-interrupts... wait, LangGraph interrupt semantics: on resume with Command(resume=decision), interrupt() returns the decision. If worker restarts mid-wait, the checkpoint contains the interrupt, and a new runner can re-publish the card and await the decision — approvals survive container replacement (documented in approvals.py lines 142-148). Double-apply: the decision key `approval:{approval_id}:decision` is BLPOP'd once; on re-drive a NEW approval_id is generated (card_payload generates uuid4 each time), so the old decision wouldn't be consumed... but the interrupt payload contains the approval_id; the runner re-publishes the SAME payload from the checkpoint (the interrupt value is persisted), so the same approval_id is reused — the human's earlier decision may have landed on the old key, and BLPOP would consume it. Hmm, but the payload from the interrupt is deterministic on replay, so the re-driven runner sees the same approval_id and would BLPOP on the same key — if the human already decided, the decision is still in the Redis list (BLPOP pops it — but if a previous runner consumed it and then died, the decision is LOST — BLPOP is destructive). That's a real edge: decision consumed by a dying runner = approval lost → on re-drive, blocks until timeout → deterministic DENY (safe direction, fail-closed).

Double-application: if approval granted, mutation executes, then crash before checkpoint → replay re-interrupts? No — on replay, interrupt() replays from the checkpoint: the resume value is stored in the checkpoint (LangGraph stores resume values as writes), so the decision replays and the tools node re-executes → double mutation possible for terminal_exec. file_edit guarded by hash. This is the at-least-once issue again.

4. worker/approvals.py (ApprovalBridge) is the claude-agent-sdk path — always_allow has NO destructive-command exclusion! Look: `if tool_name in AUTO_ALLOW_TOOLS or tool_name in self.always_allowed` — no is_destructive check. And `decision.get("decision") == "always_allow"` → adds to always_allowed unconditionally, including for destructive Bash commands. Compare with engine/approvals.py which excludes destructive. Also the ApprovalBridge persists always_allow per tool NAME (e.g., "Bash") — so always_allow on Bash allows ALL future bash commands including destructive ones, whereas engine gate re-checks is_destructive every time (line 66: `if tool_name in self._always_allowed and not is_destructive`). This is an inconsistency between the two approval paths — the SDK bridge allows always-allow to bypass destructive re-verification. That's a safety gap (though approvals are harness safety not moat-4; still a moat-1/2-adjacent finding and contract drift between two gates).

5. Pending approval in worker/approvals.py: approval request published via XADD to `approvals:{run_id}` — durable stream, so pending approvals survive restart of the SDK worker; decision consumed via BLPOP — same destructive-consume edge.

6. Moat 2: mutating tools do NOT verify write-lock ownership — no lock check anywhere. `_resolve` enforces workspace containment only. The write lock is enforced at spawn time by capacity semaphore (per philosophy: "Enforced by the capacity semaphore before a thread is spawned"). WORKSPACE_DIR env is per-thread, and per-thread owned .git means threads have separate clones — so two threads can't write the same files by construction. BUT background jobs: output files live in WORKSPACE_DIR/.collegium/terminal — inside the workspace, which gets shredded at run end. Job output files are destroyed on shred; in-memory jobs die with the process. What if a background process outlives the workspace? `start_new_session=True` gives it its own process group, and the pump's cancellation path SIGKILLs on shutdown — good. But if the engine process dies hard (SIGKILL on the container), the background child process is NOT killed (no parent-death signal), and it can keep writing into a path that then gets shredded/reused — a leaked process writing into a reused workspace path. JOB_TIMEOUT_S caps at 2h but only while the pump lives. Container death → orphan risk real. Mitigated by container-level isolation (each thread in its own container?) — but within this slice's code, there's no pdeathsig.

7. Moat 7: background.py is well done — auto-background, lifecycle tracking, completion notify, watch with debounce, kill escalation, ceiling. Orphan risk only on hard process death.

8. Moat 6: tool schemas — MUTATING_TOOLS defined as module-level @tool functions with static docstrings → frozen. Good. file_write imports diagnostics inside function — fine for caching. Schemas static.

9. Checkpoint granularity: per-superstep (node boundary), not write-ahead around individual mutations. The mirror (DeltaChannel) appends AFTER inner aput — mirror failure doesn't fail checkpoint (best-effort, documented). Resume = at-least-once for side effects; exactly-once only for state.

One more check: does anything checkpoint BEFORE executing a mutation? The approval gate interrupt persists the checkpoint BEFORE execution (the gate node interrupts before tools node). So there's a checkpoint before the mutation batch — meaning on crash the replay re-executes ALL approved mutations in the batch (at-least-once). Read-before-edit hash gives file_edit/file_write refusal-on-replay; terminal_exec has no such guard. Also note: file_write's hash-refusal returns an error to the agent, who must re-read — recovery is agent-driven, not transactional.

Also worth noting: `file_edit`/`file_write` use `p.write_text` — NOT atomic (no tmp+rename). Partial write on crash leaves a truncated/corrupted file. OSError mid-write → partial file. No rollback. Moat-1 gap.

Also `ApprovalGate` class (engine/approvals.py lines 35-136) appears to be legacy alongside ApprovalBroker (the interrupt-driven one). Two approval implementations coexist — the BLPOP-blocking gate inside graph execution would block the event loop... actually gate.request blocks on BLPOP inside the node — but the docstring for ApprovalBroker says the graph gate node uses interrupt and the runner blocks. ApprovalGate may be legacy/unused. Let me verify quickly whether ApprovalGate is referenced anywhere.


agent7:

I have everything I need. No CI files exist (no .github, no azure-pipelines). The spike has a contract test (`worker/tests/test_spike_contract.py`) that runs without the gateway, but the live matrix itself is manual (`scripts/run-spike.ps1`, `python -m spike.matrix all`).

Let me now compose the report. Key findings:

**Moat 1 (harness never unrecoverable):**
- `agent_loop.py` reveals failure modes the production engine must handle: malformed tool_call from gateway (line 230-238: answered with error ToolMessage to keep call/result pairing intact), stream errors mid-turn (line 206-210: break, records error), empty stream / no ai_message (line 212-214), tuple chunk unwrapping bug L-11 (line 190-198), chunk accumulation (line 199-205). These are documented lessons.
- Unrecoverable states in spike: `stamp_workspace` raises `SystemExit` (checks.py:43, tracer.py:147) — kills the whole matrix on missing golden repo; acceptable in spike. The `run_agent_loop` breaks on stream error and returns partial recorder — the run is "done" but errored; no retry/resume. That's fine for spike.
- engine_matrix.py has the M-24 fix: MAX_RESUMES=50 cap on the interrupt pump (lines 185-200) — a real harness lesson learned (buggy graph re-interrupting forever hung the matrix). This is exactly a moat-1 failure mode discovered by the spike and fixed.
- engine_matrix.py H-18: WORKSPACE_DIR env save/restore (lines 218-233) — cross-model workspace corruption found and fixed.
- M-13: reset_registry per model (line 222-224) — leaked spawns/watchdogs across sequential runs.
- These M-/H-/L- numbered comments show the spike surfaced real production-relevant failure modes.

**Moat 2 (conflict-free concurrency):**
- stamp_workspace owns its .git (tracer.py:142-144 comment "the stamp owns its .git (plan §3)") — aligns with per-thread owned .git.
- But: `stamp_workspace` does `shutil.rmtree(dest)` then re-clones (checks.py:44-47) — no locking; two concurrent matrix runs on the same dest would collide. Spike is sequential so OK.
- engine_matrix runs models SEQUENTIALLY (run_engine_matrix loops `for model in models` with await) — deliberate serialization in spike, and the M-13/H-18 fixes are precisely about shared-process state leaking between sequential runs. Validates the production concern about module-level registries (ContextVar fixes in fanout.py/memory.py).

**Moat 3 (swarm bounded fan-out):**
- engine_matrix does NOT test fan-out dimensions. Matrix axes: models × checks (a-g), not parallelism. No N-concurrent-spawn test, no capacity reservation check, no 100-cap verification. The only fanout touchpoint is `reset_registry()` import from worker.engine.fanout (line 223). ABSENT for bounded fan-out verification.

**Moat 7 (parallel by default):**
- Spike matrix is serial by design (models sequential, checks sequential within a model). Acceptable for a gate, but the spike never validates parallel stamping or concurrent explorers. The M-13 comment notes sequential-in-one-process as the motivation. PARTIAL at best — actually for "spike coverage" it's mostly ABSENT; the serialization leaks (M-13, H-18) are the concurrency lessons, which is moat 2 territory.

**Moat 6 (max prompt caching):**
- check_cache (checks.py:153-180, tracer.py:354-383): THE deciding number (f). Two runs, same big prefix, asserts cache_read_input_tokens > 0 on run 2, computes cache_hit_ratio_run2. Threshold registered pre-run in DECISION_MATRIX.md:19.
- BUT: it detects whether caching survives gateway translation — it does NOT detect prefix drift per se. It uses a static synthetic prefix ("You are investigating..." * 400), not the real assembled prompt. It cannot catch a dynamic field sneaking into the frozen prefix of the production system prompt — the exact silent cache-killer the philosophy warns about (§6 failure mode). It validates the transport, not the assembly. PARTIAL.
- Also `_extract_usage` handles both OpenAI (`prompt_tokens_details.cached_tokens`) and Anthropic (`cache_read_input_tokens`) shapes (agent_loop.py:124-140, duplicated in checks.py:183-199) — good gateway-translation awareness.

**Moat 4 (token intelligence, free reader no classifier):**
- checks.py asserts token accounting present (c: usage on every ResultMessage, input+output > 0 — DECISION_MATRIX.md:16). No classifier anywhere in spike — tools are file_read/file_edit/bash, unrestricted. The ASK_PROMPT/SOAK_PROMPT demand file:line citations — the "golden taste" norm. But no check validates selective reading behavior (smallest relevant chunk). PARTIAL.

**Moat 5 (cost via smallest relevant reads):**
- Token accounting check (c) exists; usage recorded per run. But no budget-visibility check, no cost-per-run threshold in the matrix. The soak drift check (e) is about tool success, not token growth. No invariant like "context grows sublinearly" or "no full-file dumps." PARTIAL/weak.

**Cross-cutting:**
- CI wiring: NO CI files exist in repo (no .github, no azure-pipelines). `worker/tests/test_spike_contract.py` runs the offline contract tests (tool taxonomy, gate evaluation, template rendering) via pytest — that's wired into whatever runs pytest, but the live a–g matrix is manual (`scripts/run-spike.ps1`, README). So the lessons can silently rot: the gate is only re-run manually. pyproject.toml:8 says "Bump only with the gate green" — a process guard, not automated.
- Duplication/divergence: `_extract_usage` is duplicated verbatim between agent_loop.py:124-140 and checks.py:183-199 (checks.py imports from agent_loop but redefines _extract_usage locally rather than importing it — actually checks.py defines its own private copy; agent_loop's is not exported in __all__). `stamp_workspace` is duplicated between tracer.py:142-158 and checks.py:39-54 (near-identical; tracer's has the plan §3 comment, checks' doesn't). tracer.py is explicitly SUPERSEDED (line 1-7) but kept — dead code that still compiles against claude_agent_sdk. DECISION_MATRIX thresholds live in the template; engine_matrix reuses evaluate_gate/render_matrix from spike.matrix (good — single source for gate logic).
- engine_matrix.py:375-381: the M-23 fix — gate_passed None vs False distinction, noting "matrix.py was fixed, this file re-introduced the false negative" — direct evidence of duplication-induced divergence between spike files.
- Production engine references spike lessons: fanout.py:138-143 (M-13), memory.py:165-168 (M-20), runner.py:108-112 — the spike's sequential-in-one-process exposed shared-state bugs that production fixed with ContextVars. This is the spike doing its job.
- hardening.py:1-7 — the 30-turn soak graduated into a production "hardening harness" — lesson institutionalized.

Now the verdict table. Verdicts mean "spike coverage of this moat":

1. Moat 1: COVERED — agent_loop + engine_matrix surfaced real failure modes (malformed tool calls, stream errors, infinite interrupt loop M-24, env corruption H-18, registry leak M-13). Spike itself has unrecoverable bits (SystemExit, break-on-error) but they're contained and mostly not copied; production fixed the leaked ones.
2. Moat 2: PARTIAL — owned .git stamp validated; but no concurrent-run conflict testing; sequential matrix only exposed shared-process state leaks (valuable but different axis).
3. Moat 3: ABSENT — no fan-out/cap testing; matrix axes are models × checks a–g only.
4. Moat 4: PARTIAL — no classifier (good, aligned), token accounting asserted, citation norms in prompts; but no check on reader selectivity.
5. Moat 5: PARTIAL — usage recorded and gated (c), but no cost-per-run threshold or read-size invariant.
6. Moat 6: COVERED (for transport) — check (f) is "THE deciding number" with pre-registered threshold; but PARTIAL on prefix-drift detection — synthetic static prefix, not the real assembled prompt. I'll call it COVERED with a note, since the spike's job was gateway translation. Actually the prompt asks "Could it detect prefix drift (the silent cache-killer)?" — answer: no, it can't; it only proves the gateway passes cache tokens through. I'll mark COVERED with explicit note that prefix-drift detection is absent.
7. Moat 7: ABSENT — everything serial; no parallel validation.

Top findings ranked by severity:
1. No CI wiring for the live matrix — lessons rot (scripts/run-spike.ps1 manual; only offline contract tests in pytest; no .github/azure-pipelines at all).
2. Cache check can't detect prefix drift — synthetic prefix, two cold calls; the §6 silent killer would pass this gate.
3. Duplicated `_extract_usage` / `stamp_workspace` between tracer.py and checks.py; M-23 comment proves divergence already happened (engine_matrix re-introduced a fixed bug).
4. tracer.py is superseded dead code still in tree — risk of someone running the Anthropic-protocol tracer against the open-model route (its own docstring warns).
5. No fan-out/cap coverage (moat 3) — the 100-cap and reservation race are untested by any spike.
6. `stamp_workspace` rmtree+clone with no lock — fine sequentially, would be a hazard if the matrix ever parallelizes (moat 7 tension).
7. engine_matrix MAX_RESUMES=50 returns partial result silently-ish — actually it returns interrupt_count, and gate checks state; fine.

Strengths:
- Pre-registered thresholds ("no number-and-a-debate") in DECISION_MATRIX.md — mechanical gate, exit code 0/1 (engine_matrix.py:390).
- Real-engine gate (engine_matrix drives build_graph() with production wiring, Postgres checkpointer) — certifies the thing that ships, not a toy.
- M-/H-/L- numbered lesson comments show failure-mode discovery fed back into production (fanout ContextVar, memory ContextVar, runner fresh registry).
- Malformed-tool-call resilience lesson (agent_loop.py:230-238) — keep call/result pairing intact.
- Soak drift metric (last-10 vs first-10 tool success) graduated into production hardening.py.
- Offline contract tests exist (test_spike_contract.py) for taxonomy/gate/template.

Word budget: under 600 words. Let me write it densely.


agent8:

## Agent 8: Spike Remainder + Engine/Memory Contract Tests

### Moat verdicts

| Moat | Verdict | Evidence (file:line) | Notes |
|---|---|---|---|
| 1 Harness recoverable | PARTIAL | `test_memory_contract.py:72-103`; `interrupt_graph.py:148-152,175-188` | Compaction rollback (fault-injected, identity-asserted) is a true no-data-loss pin. But zero resume-after-crash / checkpoint-integrity / lost-event tests; spike explicitly defers durability ("not Postgres durability", MemorySaver only). |
| 2 Conflict-free concurrency | UNPINNED | `test_memory_contract.py:210-219` (only thread-scoped memory search) | No write-lock exclusivity or session-isolation test in this slice. Likely lives in `test_fanout_contract.py`/`test_spine_contract.py` (other slices). |
| 3 Swarm 100-cap / reservation race | UNPINNED | — | Nothing references cap, semaphore, or reservations in any assigned file. |
| 4 Token intelligence (no gate) | PARTIAL | `test_engine_contract.py:131-137` | Pins that `file_read`/`file_search`/`file_glob` are readonly and never need approval — the "free to read" half. No pin on "no classifier" or cost visibility. |
| 5 Cost via smallest reads | PARTIAL | `spike_tools.py:23-54`; `test_engine_contract.py:274-278`; `test_memory_contract.py:32-47` | offset/limit affordance + truncation footer pinned in spike; `Budget.would_exceed` and prune-tools-first pinned. "Surfaced to engineer, human kill switch" untested. |
| 6 Frozen prefix / caching | UNPINNED | `matrix.py:76`; `DECISION_MATRIX.md:19`; `test_spike_contract.py:106,120-124` | Caching is "the deciding number" in docs but pinned only by synthetic-boolean gate tests; real check f needs a live gateway. No engine test asserts prefix byte-stability or static-above/dynamic-below ordering. |
| 7 Parallel by default | UNPINNED | `matrix.py:48-65` | No parallelism test; spike runner itself serializes models in a for-loop. |

### Top findings (ranked)

1. **Moat 6 has no veto-grade test anywhere in the slice** — `test_spike_contract.py:106-124` feeds `{"caching_survives": True/False}` into `evaluate_gate`, which pins gate arithmetic, not caching. The philosophy (§6) calls prefix drift a silent correctness boundary "caught by review, not by metrics" — and indeed no test catches it.
2. **Moat 1's core invariants are untested at engine level** — `test_engine_contract.py` pins event shape, seq monotonicity (`:40-48`), and redaction, but never crash-resume, checkpoint integrity, or event durability. The one genuinely behavioral recoverability pin is compaction rollback (`test_memory_contract.py:96-103`: `new is messages`, `after_tokens == before_tokens`).
3. **`test_spike_contract.py` substantially guards dead code** — `spike_tools.py:3` declares itself "NOT the production tool suite"; `test_tracer_normalizer_kwargs_fixed` (`:77-88`) source-greps `tracer.py`, which `README.md:64` marks "LEGACY… superseded; kept for history" and which can't even be imported. These pin a Phase 0 gate that already ran, not living moats.
4. **Moats 2 and 3 are entirely absent from this slice** — no capacity semaphore, reservation-race, per-repo lock, or 100-cap assertion exists in any assigned file; the slice cannot veto a concurrency regression.
5. **Shallow spots amid strong ones** — `test_graph_compiles` (`test_engine_contract.py:158-160`) asserts only `is not None`; `_should_continue` tests (`:163-177`) are routing-shape checks that wouldn't catch a state-corruption regression.

### What this slice does well

- **Regression-anchored behavioral tests**: incident IDs (C-01, C-02, H-08, H-20, M-22, M-23) with fault injection — e.g. monkeypatched `_PRUNE_ORDER` forcing protected-message loss to prove rollback fires (`test_memory_contract.py:82-103`), and shell-chaining smuggling blocked outright (`test_engine_contract.py:254-263`).
- **Secrets redaction pinned at the event boundary, including `reasoning_content`** (`test_engine_contract.py:319-331`) — chain-of-thought can't leak keys.
- **Doc → threshold → test chain**: `DECISION_MATRIX.md` fixes thresholds pre-run; `matrix.py:70-78` encodes them; `test_spike_contract.py:111-151` pins the gate against them — a model of "no number-and-a-debate," even if for a one-shot gate.
- **Failure-honesty fixes in spike code**: M-22 (`interrupt_graph.py:175-188`) stops real errors masquerading as passing interrupt checks; M-23 (`matrix.py:136-139,181-189`) renders "not evaluated" distinctly from "failed" — the spike itself models moat-1's no-silent-loss ethos.

I have all the evidence I need. Other test files exist (test_fanout_contract.py, test_spine_contract.py, test_hardening_contract.py) that likely pin moats 2/3 — but those belong to other agents' slices. My verdicts cover only my slice, and I should note where a moat is pinned elsewhere (fanout contract likely pins moat 3).

Now let me compose the report. Key analysis:

**Moat 1 (harness never unrecoverable):**
- spike/interrupt_graph.py explores interrupt/resume (crash-adjacent: resume-after-interrupt, state_lost flag). Lines: interrupt_graph.py:134-201, state_lost at :185, :199; M-22 fix at :175-188 distinguishes real errors from interrupts — good. But it uses MemorySaver (line 148-152) explicitly noting "not Postgres durability (that's Phase 2/4)" — so crash recovery durability is NOT explored, only interrupt/resume mechanism.
- test_engine_contract.py: no resume-after-crash, no checkpoint integrity, no lost-events assertions. It pins event shape (seq monotonic :40-48), pairing, redaction. Monotonic seq is a weak recoverability-adjacent invariant (event ordering) but no crash test.
- test_memory_contract.py: compaction rollback tests (:72-103) are the strongest moat-1-aligned tests — honesty validator rolls back, returns ORIGINAL messages unchanged, no data loss, token count restored. That's a real no-data-loss invariant with fault injection. Verdict: PARTIAL.

**Moat 2 (conflict-free concurrency):** Nothing in this slice tests per-repo write lock or session isolation. test_episodic_memory_search_scopes_by_thread (:210-219) tests thread-scoped search isolation in episodic memory — a weak session-isolation signal at the memory layer. Verdict: UNPINNED in this slice (likely pinned in test_fanout_contract.py / test_spine_contract.py — other slices).

**Moat 3 (100-cap / reservation race):** Nothing. UNPINNED in this slice.

**Moat 4 (token intelligence, no classifier):** Nothing directly; engine contract tests capability registry/approval gate (:131-144) which is an approval gate, not a read classifier. The readonly gate on terminal_exec is a write-gate, not a read-gate — consistent with moat 4 (reads are free). No test asserts "no classifier on reads." Verdict: UNPINNED (arguably N/A — absence of a gate is hard to pin; but a test asserting file_read needs no approval exists at :131-135 — that IS a weak pin: readonly tools need no approval in supervised mode).

Actually test_readonly_tools_need_no_approval (:131-137) pins that file_read/file_search/file_glob are readonly and need no approval — that's a moat-4-aligned pin (free reading, no gate on reads). I'd call moat 4 PARTIAL: the "free to read" half is pinned; the "no classifier" and "cost visibility" halves are not.

**Moat 5 (cost via smallest reads):** spike_tools.file_read has offset/limit with actionable footer (:23-54) — the affordance exists in spike. test_spike_contract tests the footer presence (:31 "lines" in output). Budget.would_exceed tested (:274-278). Compaction tests pin context-limit behavior. But no test pins "smallest relevant read" behavior or budget surfacing to engineer. Verdict: PARTIAL — budget math and compaction pruning pinned; the "surfaced, not silently enforced" and targeted-reads-as-default are not.

**Moat 6 (frozen prefix / prompt caching):** spike check f (cache) exists in matrix.py THRESHOLDS (:76) and DECISION_MATRIX.md:19 — documented as "the deciding number" — but the actual check_cache is in checks.py (not my slice), and test_spike_contract only tests gate evaluation with synthetic `caching_survives` booleans (:106, :120-124). Nothing tests that the prompt prefix is frozen/byte-stable/cache-stable in the engine. PromptOrigin tagging exists (state.py, tested :281-284) which is prefix-ordering infrastructure, but no frozen-prefix assertion. Verdict: UNPINNED (documented in spike, gated only by live spike run, no engine test).

**Moat 7 (parallel by default):** matrix.py run_matrix iterates models SEQUENTIALLY (:48 for model in models: await ...) — the spike runner itself serializes across models, mildly anti-moat-7 but it's a spike CLI. No test pins parallelism. Verdict: UNPINNED.

**Contract quality:**
- test_engine_contract.py: mostly behavioral with regression IDs (C-01, C-02, H-20, M-22, M-23). Strong tests: command chaining injection (:254-263), direct dispatch executes mutating (:223-232), destructive never always-allowable (:235-243), redaction of reasoning content (:319-331), temperature omission (:363-378), alias normalization (:381-410). Some shallow: test_graph_compiles (:158-160) asserts only non-None; _should_continue tests are shape-level.
- test_memory_contract.py: strong — fault-injection rollback (H-20, :72-103), floor enforcement (H-08, :132-162), self-tuning limits. These are genuinely behavioral.
- test_spike_contract.py: guards dead experiment code. It tests spike_tools (explicitly "NOT the production tool suite" spike_tools.py:3), gate evaluation logic (matrix.py — spike CLI), and a source-text check on tracer.py which is "LEGACY... superseded; kept for history" (README:64). test_tracer_normalizer_kwargs_fixed (:77-88) is a source-grep test guarding a legacy file that can't even be imported (claude_agent_sdk not installed). So spike contracts largely guard dead/one-shot code: the gate already ran; the matrix is a Phase 0 artifact. The taxonomy tests (kind: success|error) mirror the production ToolResult taxonomy, so they have some carryover value, but production has its own call_tool tested in test_engine_contract.py — duplication.

**README/DECISION_MATRIX doc-to-test linkage:** DECISION_MATRIX.md fixes thresholds pre-run ("no number-and-a-debate") and matrix.py THRESHOLDS (:70-78) encode them; test_spike_contract pins evaluate_gate against those thresholds (:111-151). That's a decent doc→code→test chain, but for a one-shot gate, not a living moat. Check f (caching) is documented as "the deciding number" (DECISION_MATRIX.md:19) yet pinned only by a synthetic-boolean gate test — the real behavior requires a live gateway run.

Top findings ranked:
1. Moat 6 unpinned in engine: no test asserts frozen static prefix / cache stability; only spike check f via live run, and gate test uses synthetic booleans. (test_spike_contract.py:106-124, matrix.py:76)
2. Moat 1 only partially pinned: no resume-after-crash/checkpoint-integrity/lost-event tests in engine/memory contracts; interrupt_graph uses MemorySaver and explicitly defers durability (interrupt_graph.py:148-152). Compaction rollback (test_memory_contract.py:72-103) is the one strong no-data-loss pin.
3. Moats 2 & 3 unpinned in this slice: no write-lock, capacity semaphore, reservation-race, or 100-cap test (likely in test_fanout_contract.py — other slice).
4. test_spike_contract.py guards dead code: tracer.py is legacy/superseded (README.md:64) and tested via source-grep (test_spike_contract.py:77-88); spike_tools explicitly not production (spike_tools.py:3-7). The contracts pin a Phase 0 gate that has already run, not living moats.
5. matrix.py runs models sequentially (matrix.py:48-65) — anti-moat-7 in the spike runner itself, though low-stakes for a CLI.
6. Shallow tests: test_graph_compiles asserts only non-None (test_engine_contract.py:158-160).

Strengths:
- Regression-pinned behavioral tests with incident IDs (C-01, C-02, H-08, H-20, M-22, M-23) — real fault-injection, not shape checks.
- Compaction honesty-validator rollback with identity assertion (`new is messages`) and token restoration — veto-grade no-data-loss pin.
- Redaction at event boundary including reasoning_content — secrets can't leak via CoT.
- Doc→threshold→test chain for the spike gate.
- interrupt_graph.py M-22 fix distinguishes real errors from interrupts — avoids masking failures as passes.

Word budget: under 600 words. Let me write it tightly.


agent9:

## Agent 9: Spine & Module Contract Tests

### Moat verdicts

| Moat | Verdict | Evidence (file:line) | Notes |
|---|---|---|---|
| 1. Never unrecoverable | **PARTIAL** | test_spine_contract.py:305-334, 627-664 | Crash-resume strongly pinned: approval survives graph rebuild (in-mem) AND fresh Postgres connection. But wait-for-exit, workspace shredding, retention decay, and `release == commit` slot semantics are entirely absent. |
| 2. Conflict-free concurrency | **PARTIAL (weak)** | test_goal_mode_contract.py:123-144 | Only pin: per-thread ContextVar isolation for `ask_user` pending questions (H-10 regression). Per-repo write lock, session-volume isolation, deterministic queue order: zero coverage. |
| 3. Swarm cap / reservation race | **UNPINNED** | test_rc_modules.py:523 (name-only) | `spawn_agent`/`spawn_swarm` appear only in a registry binding list. `SWARM_MAX_SLICES=8` saturation, `live + incoming > cap` check-then-act (fanout.py:155-169), 2h timeout: untested in this slice (deferred to test_fanout_contract.py, outside my slice). |
| 4. Token intelligence (no classifier) | **UNPINNED** | — | Nothing asserts absence of an admission gate; `wrap_untrusted` quarantine test (test_rc_modules.py:150-153) is anti-injection, not token-intelligence. |
| 5. Budget visibility | **PARTIAL** | test_spine_contract.py:536-554 | Budget *warning event* at 80% pinned. But "budget visible in-loop to the agent", overrun *flagged-not-killed*, and smallest-read affordances are not asserted. |
| 6. Frozen prefix / caching | **UNPINNED** | — | No assertion on prompt assembly order or cache-cut placement anywhere in the slice. |
| 7. Parallel by default | **UNPINNED** | — | No concurrency is exercised: every test drives a single thread on `MemorySaver` sequentially. No assertion that spawns/stamps/mounts fan out. |

### Top findings (ranked)

1. **Swarm cap untested at contract level** — test_rc_modules.py:517-536 reduces `spawn_swarm` to a name-resolution check while fanout.py:162 raises on `live + incoming > SWARM_MAX_SLICES`. A regression deleting the cap or the reservation race guard would pass this slice green.
2. **No write-lock or session-isolation pin** — Moat 2's core invariant (one writable thread per repo, per-lane `~/.claude` mounts) has zero executable enforcement here. The single H-10 isolation test (test_goal_mode_contract.py:123) is valuable but covers only clarify-question state, not repos or sessions.
3. **Slot-leak path unpinned** — philosophy §1 makes `release == commit` (slot frees on insert failure) a named trust-destroying bug class; no test in this slice fails a spawn mid-reservation to prove the slot is released.
4. **Postgres evidence is opt-out, not enforced** — test_spine_contract.py:635-636 skips without DATABASE_URL; the strongest Moat-1 proof (interrupt durability across connections) silently vanishes in default CI runs.
5. **Compaction retry counter assertion is inverted-weak** — test_spine_contract.py:260 asserts `compaction_retries == 0` post-recovery but never asserts the retry *limit* trips on persistent overflow (unrecoverable-loop guard missing).
6. **No frozen-prefix or no-classifier guard** — Moats 4/6 are pure review-bar moats; nothing executable would catch a dynamic field sneaking above the cache cut or a new gate being added.

### What this slice does well

- **Genuinely behavioral, not shape-checking**: negative assertions abound — file must NOT exist before approval (test_spine_contract.py:294), denied tool never executes across 3 denials (:365-367), edited args execute and originals provably do not (test_rc_modules.py:462-463), debounce must yield *exactly one* watch hit (:274-277, with the M-25 comment explaining why the weak assertion was replaced).
- **Real graph, real tools**: ScriptedLLM drives the assembled graph with actual filesystem effects, so these catch semantic regressions (gate routing, denial breaker, critic escalation), not just refactors.
- **Docstring-embedded incident lineage**: H-10, C-04, C-05, M-25 comments tie tests to the failures that motivated them — exactly the "accumulated, paid-for correctness" the philosophy describes.
- **Coverage gap noted**: `approvals.py`, `runner.py`, `watchdogs.py`, `hardening.py`, `memory.py`, `llm.py` (prefix assembly), and `fanout.py` behavior have no contract in this slice — the harness-riskiest modules are the least pinned.


agent10:

## Agent 10: Hardening/Fanout/Tool-Surface Contract Tests

### Moat verdicts

| Moat | Verdict | Evidence (file:line) | Notes |
|---|---|---|---|
| 1 — Never unrecoverable | PARTIAL | test_hardening_contract.py:98-137, 217-258, 282-311; test_fanout_contract.py:137-196 | Resume/durability/rollback strongly pinned; recovery *loop* and several failure scenarios absent |
| 2 — Conflict-free concurrency | PARTIAL | test_mutating_contract.py:30-126; test_approval_bridge.py:77-124 | Content-hash read-before-edit + restart-durable always_allow pinned; per-repo write lock, per-thread `.git`, queueing not in this slice |
| 3 — Swarm cap + reservation race | PARTIAL | test_fanout_contract.py:35-67, 82-88, 207-208 | Saturation veto pinned end-to-end; the check-then-act race and the 100 cap are NOT tested |
| 4 — Free reader, no gate | PARTIAL | test_mutating_contract.py:253-262 | Readonly tools proven to skip the approval gate; "no read classifier" and "full reads remain available" unpinned in this slice |
| 5 — Smallest-read cost discipline | PARTIAL | test_rd_tool_surface.py:44-61, 67-78 | ≤10-schema default bind and roster token budget pinned; offset+limit / grep-then-read affordances not asserted |
| 6 — Frozen prefix | UNPINNED | test_rd_tool_surface.py:40-42 (only adjacent pin) | DEFAULT_TOOLS name list pinned, but no schema-content stability, prefix-order, or cache-cut test exists |
| 7 — Parallel by default | UNPINNED | test_fanout_contract.py:199-200 | Only a serialization constant (`SPAWN_STAGGER_S == 2.0`) is pinned; no concurrency behavior asserted |

### Top findings (ranked by severity)

1. **Reservation check-then-act race has zero coverage — the core of moat 3.** `test_spawn_agent_vetoed_when_worker_saturated` (test_fanout_contract.py:35-54) pre-fills the registry synchronously, then spawns once. No test fires N concurrent spawns at a cap of N−? and asserts only the remainder get in. A regression letting N racers slip N+1 threads past the cap passes every test here. The tested registry is also in-process; the cross-worker DB reservation path (philosophy §3, lines 101-104) is untested in this slice.
2. **The 100 cap is never pinned.** `test_swarm_max_slices_is_8` (test_fanout_contract.py:207-208) pins the per-call swarm width (8), not the global hard cap of 100 Threads. If the global cap constant or its enforcement drifted, nothing here fails.
3. **Watchdogs pinned as signal-emitters, not as recoverers.** test_watchdogs_contract.py:17-47, 83-124 asserts drift/budget/idle *signals* fire correctly but never that a fired watchdog leads to drain/nudge/requeue and a healthy resumed state. Moat-1 scenarios entirely missing: wait-for-exit before session-volume remount (philosophy:27-29), workspace shredding, 30d/12mo retention decay, and `release == commit` slot-freeing on spawn-error paths (philosophy:35-36).
4. **Read-tool affordances (offset+limit, grep-then-read) unpinned.** test_rd_tool_surface.py is a *discovery* surface contract (bind count, roster budget, tool_search, fail-closed modes, MCP fold-in) — excellent at that, but it never asserts `file_read` exposes offset+limit, that targeted read is the paved default, or that full-file read remains available (moat 4's "nothing is unreachable").
5. **Moat 6 gap confirmed.** No test pins tool-schema byte stability or frozen-prefix ordering; a dynamic field sneaked into tool defs (the silent cache-killer, philosophy:229-232) would pass.
6. **Shallow literal pins.** THREAD_TIMEOUT_S/SPAWN_STAGGER_S/SWARM_MAX_SLICES (test_fanout_contract.py:199-208) pin constants, not enforcement; SLO tests (test_hardening_contract.py:441-488) are pure-function tables.

### What this slice does well (moat-aligned strengths)

- **Regression-IDs baked into docstrings** (C-02/03/04, G-02/03/04/05, H-19, M-26/27, L-05/12/13): each test names the exact bypass it killed — veto tested *through the tool* not `_veto` (test_fanout_contract.py:35-54), dispatch routing proven for the dual-registered `terminal_exec` (test_mutating_contract.py:165-192).
- **True moat-1 behavioral pins**: H-19 runs the real graph, resumes with a nudge, and asserts turn-1 user message + tool result survive (test_hardening_contract.py:217-258); G-02 forces the otherwise-unreachable rollback branch and asserts the original list returned unchanged, tokens identical (test_hardening_contract.py:282-311).
- **Approval durability across restart** (test_approval_bridge.py:77-99): fresh bridge instance, in-memory empty, and the card provably does NOT reappear — plus run-scoped sharing across threads and `allow_once` non-persistence.
- **Watchdog lifecycle**: spawn arms the 2h watchdog via the async dispatch path and drain cancels it (test_fanout_contract.py:157-196) — the one place the recovery loop IS closed.

I've read all 6 files plus the philosophy. Let me analyze each moat.

**Moat 3 (fan-out, 100 cap, reservation race):**
- test_fanout_contract.py pins `SWARM_MAX_SLICES == 8` (line 207-208), not 100. The philosophy says hard cap of 100. The test pins an 8-slice swarm width cap. There's a registry saturation veto (test_spawn_agent_vetoed_when_worker_saturated, test_spawn_swarm_vetoed_when_worker_saturated). But CRITICAL: there is NO test of concurrent spawns racing — no N concurrent spawns slipping N+1 past the cap. The saturation tests pre-fill the registry synchronously and then spawn once. The reservation check-then-act race (reservations held between try_acquire and row insert) is NOT tested. Also, the tests here are worker-side registry (in-memory, per-worker), not the DB capacity semaphore/reservation. So Moat 3 is PARTIAL at best: cap constant pinned (but at 8, not 100 — the 100 global cap isn't tested in this slice at all; maybe another agent's slice covers it), concurrent race NOT tested.

Wait — is SWARM_MAX_SLICES=8 the swarm width cap (per spawn_swarm call) rather than the global 100 cap? Yes — "Max width cap: 8 slices" (line 83). The 100 cap is the global Thread cap enforced by capacity semaphore + reservations in the backend. This slice has no test of 100, no test of concurrent spawn race. The saturation veto tests a per-worker in-memory registry.

**Moat 1 (never unrecoverable):**
- test_hardening_contract.py: Redis stream durability (events survive consumer disconnect), approval bridge to backend stream, DeltaChannel JSONL mirror replayable + task boundaries, MemorySaver resume preserves conversation (H-19 — real graph run + resume), rollback drill (compaction preserves protected messages; G-02 forced rollback branch), sink failure swallowed (G-03), mid-stream LLM failure emits partial delta then errors (G-04), invalid mode_request rejected (G-05), SLO evaluation, StepEvent JSON round-trip.
- test_watchdogs_contract.py: drift watchdog (rate drop, stall, priority), collision radar warn-only, budget reminders 50/80, idle gate, critic rubric blocks.
- Missing scenarios: wait-for-exit before replacement mounts session volume (philosophy line 27-29) — not tested here. Workspace shredding at run end — not tested. Session retention two-step decay (30d/12mo) — not tested. Capacity semaphore release==commit — not tested here (maybe another slice). Watchdogs are tested as units (signal detection) but NOT the recovery loop: does a drift signal actually trigger a recovery action (nudge/kill) and does the harness recover to a healthy state? test files only assert signal emission, not recovery. The 2h spawn watchdog drain IS tested in fanout (test_timeout_watchdog_drains_long_running_spawn) — that's a recovery invariant. But "watchdog fires → harness recovers, no unrecoverable state" is only partially pinned.
- Verdict: PARTIAL.

**Moat 2 (conflict-free concurrency):**
- In this slice: collision radar (warn-only, v1) in watchdogs — that's advisory, not the per-repo write lock. test_mutating_contract pins read-before-edit content-hash guard (multi-actor contract) — that's relevant to mutation safety. Approval durability across restart in test_approval_bridge (always_allow survives bridge restart, shared across threads, allow_once doesn't persist). 
- Not in this slice: per-repo write lock, per-thread .git, durable session volumes, deterministic queueing.
- Verdict for this slice: PARTIAL (hash guard + approval persistence pinned; lock/queue not in slice).

**Moat 4/5 (token intelligence / cost):**
- test_rd_tool_surface.py: Actually this file is about tool discovery surface — default bind ≤10 schemas, roster ≤0.5K tokens, tool_search, fail-closed mode gating, MCP fold-in, discovered tools survive checkpoint/compaction. Hmm, wait — the assigned description said "test_rd_tool_surface.py — does it pin the read-tool affordances (offset+limit present, grep-then-read path, no read-side gate)?" But the actual file is about tool discovery (deferred tools, roster, tool_search). It does NOT test offset+limit on file_read, grep-then-read path, or absence of read-side gate. It does pin "readonly never hits the gate" in test_mutating_contract (test_call_any_tool_routes_readonly_directly — gate.request.assert_not_called()), which is relevant to moat 4 (no gate on reads). But that's the approval gate, not a read-classifier gate.
- Actually the file pins: default bind ≤10 schemas (cost/moat 5-6 surface), roster fragment budget (token cost of the roster), fail-closed discovery (mode-denied absent). The fail-closed discovery is mode gating of MUTATING tools (file_write, spawn_agent absent in ask mode) — that's a safety gate on mutation, not a read gate. 
- Does it assert full-file reads remain AVAILABLE? No. No offset+limit assertion. No grep-then-read. So Moat 4: PARTIAL (read tools never approval-gated is pinned in mutating contract; but the "no classifier on reads" and "full reads available" aren't pinned in this slice). Moat 5: PARTIAL (roster token budget and ≤10 schema bind are cost-surface pins; but smallest-read affordances not pinned here).

**Moat 6 (frozen prefix):**
- Any test pinning tool-schema stability? test_rd_tool_surface pins DEFAULT_TOOLS exact list (test_tier0_contract_names) and ≤10 bound schemas — that's a pin on the default tool set, which contributes to frozen tool defs. But there's no test that tool schemas are byte-stable across turns/runs, no frozen-prefix ordering test, no cache-cut test. The philosophy says tool definitions frozen. The DEFAULT_TOOLS list pin is a weak proxy. Verdict: PARTIAL/UNPINNED. I'd say PARTIAL leaning UNPINNED — the default tool names list is pinned but schema-content stability is not. I'll say PARTIAL with a note, or UNPINNED. The prompt says "Likely absent — note the gap." I'll mark UNPINNED with note that DEFAULT_TOOLS name list is the only adjacent pin.

Actually, let me reconsider: test_tier0_contract_names asserts the exact DEFAULT_TOOLS list — if someone added a dynamic tool to the default bind it would fail. But it wouldn't catch schema *content* drift (a field added to file_read's schema). So it's a shallow pin. Verdict: PARTIAL.

**Moat 7 (parallel by default):**
- In this slice: fanout tests register spawns but don't assert concurrency (no parallel stamp timing, no concurrent spawn assertion). SPAWN_STAGGER_S == 2.0 pinned (a serialization constant!). Nothing asserts parallel-by-default behavior. Verdict: UNPINNED in this slice.

**Contract quality:** Mostly behavioral and excellent in places: H-19 resume test runs a real graph; M-26 drives the real Forwarder; C-02 dispatch routing; G-02 forces the rollback branch; C-03 end-to-end veto; C-04 watchdog arming via dispatch. The docstrings reference regression IDs (C-02, C-03, C-04, G-02, H-19, M-26, M-27, L-05, L-12, L-13) showing these were hardened after review. Shallow spots: constant pins (SPAWN_STAGGER_S, THREAD_TIMEOUT_S, SWARM_MAX_SLICES), SLO evaluation tests are pure-function table tests, watchdog tests are unit-level signal tests without recovery-loop assertions.

**Top findings ranked by severity:**

1. **Moat 3 race untested (CRITICAL)**: No test spawns N threads concurrently against the cap; saturation tests pre-fill the registry synchronously (test_fanout_contract.py:46-54). The reservation check-then-act race — the central mechanism of moat 3 — has no coverage in this slice. A regression allowing N concurrent spawns to slip N+1 past the cap would pass all these tests. Also, the registry tested is an in-process, per-worker registry; the cross-worker DB reservation path is untested here.

2. **100-cap never pinned**: SWARM_MAX_SLICES == 8 is pinned (line 207-208), which is the per-call swarm width, not the global 100 Thread cap from philosophy §3. No test in this slice asserts 100.

3. **Moat 1 recovery loop untested**: watchdogs tested only as signal emitters (test_watchdogs_contract.py) — no test that a fired watchdog leads to recovery (nudge/drain/requeue) and a healthy resumed state. Missing scenarios: wait-for-exit before volume remount (philosophy:27-29), workspace shredding, 30d/12mo retention decay, release==commit slot freeing on error paths.

4. **Moat 4 read affordances unpinned in this slice**: test_rd_tool_surface.py is about discovery/roster, not read affordances — no offset+limit assertion, no grep-then-read default, no assertion that full-file read remains available. Only adjacent pin: readonly tools skip the approval gate (test_mutating_contract.py:253-262).

5. **Moat 6 frozen prefix effectively unpinned**: only the DEFAULT_TOOLS name list (test_rd_tool_surface.py:40-42) — no schema-content stability, no prefix-order/cache-cut test.

6. **Shallow constant pins**: SPAWN_STAGGER_S==2.0, THREAD_TIMEOUT_S==2h, SWARM_MAX_SLICES==8 (fanout:199-208) pin literals but not behavior; a change to enforcement semantics with constants intact would pass.

Strengths:
- Regression-hardened behavioral tests with incident IDs (C-02/03/04, G-02/03/04/05, H-19, M-26/27, L-05/12/13) — these were written after real review findings and pin the exact bypass.
- H-19 resume test runs the real graph twice and asserts turn-1 messages survive (hardening:217-258) — a true moat-1 behavioral pin.
- Approval durability across restart (approval_bridge:77-99) simulates container replacement with a fresh bridge and asserts the card does NOT reappear — moat-1/2 grade.
- G-02 forces the otherwise-unreachable rollback branch and asserts the original list is returned unchanged (hardening:282-311).
- C-03 veto tested end-to-end through the tool, not the private function (fanout:35-54).
- C-04 asserts the 2h watchdog is armed via the async dispatch path and cancelled on drain (fanout:157-196).

Word budget: under 600 words, dense. Let me write the final response.






10 backend parallel agents diagnosis


agent1:

## Agent 1: Orchestrator Core

### Moat verdicts (code)

| Moat | Verdict | Evidence | Notes |
|---|---|---|---|
| 1 Harness never unrecoverable | PASS | `run_manager.py:214-276` (stop/abandon stamp terminal + cancel task + shred), `run_manager.py:616-654` (boot reconciliation interrupts zombies incl. QUEUED/PLANNING, stops threads, releases keys), `run_manager.py:175-210` (guarded execute never strands; CancelledError re-raised, terminal stages never overwritten) | Wait-before-remount enforced at `run_manager.py:416-419`; cancel-before-shred at `:269-272`. Shred-on-abandon present; retention 30d/12mo is worker-side, N-A here. |
| 2 Conflict-free concurrency | AT RISK | `semaphores.py:41-58` (per-repo write lock checked + reserved before spawn), `thread_manager.py:86` (acquired BEFORE row insert/container) | Lock = in-process `asyncio.Lock` + in-memory `_reserved_writable` (`semaphores.py:20,26`). NOT cross-worker/DB-level — a second backend instance or a row committed by another process between check and insert slips through (no `SELECT ... FOR UPDATE`/unique constraint). Deterministic queueing via `spawn_many` poll loop (`thread_manager.py:180-198`). |
| 3 Swarm | AT RISK | `semaphores.py:25,35-68` (`try_acquire` + `_reserved` + `commit_reservation`/`release`), cap at `semaphores.py:37` from `config.py:100` | Reservations held between try_acquire and row insert — race closed in-process. But cap is **12**, not the philosophy's stated 100 (docstring `semaphores.py:1` even says 12). Slot leak: `commit_reservation` runs only AFTER row insert (`thread_manager.py:124-129`); if `session.commit()` at `:125` raises, `release` is never called → reservation leaks. Gateway/container failure paths are safe only because the row already exists (status flipped to `failed`, `:138,:151`). |
| 4 Token intelligence | N-A | — | No reads/classifiers in this slice. |
| 5 Cost | PASS | `thread_manager.py:132-139` (per-thread LiteLLM key with `max_budget_usd` minted before container start), `:203-220` (gateway-metered cost readback reconciles thread+run), `:222-233,:270,:285-286` (keys released on every terminal path) | Budget visible per-thread; kill switch = stop/abandon (human). |
| 6 Max prompt caching | N-A | — | Prompt assembly not in this slice (spawn_context snapshot at `thread_manager.py:109-120` aids replay, not caching). |
| 7 Parallel by default | PASS | `thread_manager.py:200` (`asyncio.gather` over all specs — concurrent spawn/stamp), `blueprints/base.py:52-74` (nodes sequential by design = deterministic state machine, not accidental serialization) | Only serializers: capacity semaphore + per-repo write lock (both moat-sanctioned). Queue poll `sleep(5s)` is backpressure, not serialization. |

### Test pins

| Moat | PINNED/PARTIAL/UNPINNED | Evidence |
|---|---|---|
| 1 | PINNED | `test_orchestrator_run_manager.py:308-345` (reconcile incl. QUEUED/PLANNING zombies), `:198-231` (cancel-before-shred order), `:368-391` (CancelledError not flipped to FAILED), `:646-677` (wait-before-spawn order) |
| 2 | PARTIAL | `test_orchestrator_semaphores.py:29-36,90-99` (write lock blocks 2nd writer, released by `release`) — but no test of cross-instance safety; `test_orchestrator_thread_manager.py:93-116` (lock released on gateway failure, real capacity) |
| 3 | PARTIAL | `test_orchestrator_semaphores.py:64-74` (reservation counts before row), `:102-120` (**N=10 concurrent gather at cap 3 → exactly 3 succeed — race PINNED**), `:77-87` (commit hands off to row). **UNPINNED: row-insert-failure slot leak** — no test where `session.commit()` between acquire and `commit_reservation` raises. |
| 5 | PINNED | `test_orchestrator_thread_manager.py:210-257` (settle/reconcile, key release, error swallow), `:318-355` (finish_thread order: stop→stamp→release) |
| 7 | PARTIAL | `test_orchestrator_thread_manager.py:275-315` (spawn_many spawns all, queues past cap once, one failure doesn't sink swarm) — concurrency of `gather` asserted implicitly, not timed. |

### Top findings (ranked)

1. **`thread_manager.py:124-129`** — Slot-leak hole: if `session.commit()` (row insert) raises, `capacity.commit_reservation` is skipped and no `release` runs → reservation leaks until restart. Violates release==commit on the one error path the alias can't cover.
2. **`semaphores.py:37` + `config.py:100`** — Cap is **12**, philosophy says **100**. Either the cap or the philosophy is stale; swarm blast-radius promise ("up to 100") is not what the code offers.
3. **`semaphores.py:20-26,44-53`** — Per-repo write lock is process-local (asyncio.Lock + in-memory set + non-locking DB count). Not safe across multiple backend instances/workers; a concurrent commit by another process between the count and the insert double-books a writable repo.
4. **`thread_manager.py:180-198`** — Queueing is poll-and-retry with fixed `sleep(5s)`, not an ordered FIFO queue; under contention, which waiter wins is scheduler luck — "deterministic" only in that it retries, not in order.
5. **`run_manager.py:209`** — `if row.stage == RunStage.FAILED.value` compares after `session.close()` on an expired instance; works only because `transition` was just committed in the same scope — fragile if refactored.

### Strengths

- Reservation design is exactly the philosophy's pattern, documented inline (`semaphores.py:21-24`); concurrent-gather test proves no over-booking.
- Failure-path discipline is exceptional: C-15/C-16/H-36/H-37/H-40/H-41/H-42 regressions each pinned with a named test; gateway/container spawn failures mark failed and release locks and keys.
- `kill_replace_thread` waits for container exit before remounting the session volume, and `abandon_run` cancels the task before shredding — both order-pinned in tests.


agent2:

## Agent 2: Goal/Development Blueprints

### Moat verdicts (code)

| Moat | Verdict | Evidence | Notes |
|---|---|---|---|
| 1 Harness never unrecoverable | PASS | `goal.py:587-604` wedged-thread bound (`THREAD_MAX_WAIT_S=2700`); `goal.py:400-412` red gate after `MAX_FIX_ROUNDS` raises → run FAILS; `goal.py:476` fixer `preserve_workspace=True` so re-stamp can't wipe the impl; `goal.py:228,243,383,481` every agentic node calls `finish_thread` (no lingering idle containers holding slots/locks). Caveat: `base.py:49-74` `execute` has **no try/finally** — a mid-fan-out raise (e.g. `_await_thread` RuntimeError at `goal.py:601` after `spawn_many` succeeded) orphans running explorer containers; cleanup depends on callers outside this slice. | Conftest fakes (`conftest.py:399-417`) never fail mid-swarm except scripted lossy case; partial-spawn cleanup is unpinned (see below). |
| 2 Conflict-free concurrency | PASS | Blueprints never touch locks directly — they delegate: `goal.py:483-490` / `development.py:217-224` `_writable_repo` gates writable scope by mode permissions; the write lock engages in `thread_manager.spawn` → `capacity.try_acquire(repo_name)` (`semaphores.py:35-59`, reservation + DB check). Per-thread owned `.git`: `stamp_clone` (`manager.py:53-80`) does `git clone` from golden per run+thread, no worktree; `manager.py:206-209` skips re-mounting the writable repo among context repos. Lock lifecycle respected: fix loop comment `goal.py:480-481`. | |
| 3 Swarm | PASS | Fan-out width = user `fanout` clamped to `global_thread_cap`: `goal.py:212-213` `requested = max(1, min(int(...fanout...), cap))`. Spawns go through `spawn_many` → per-spec `spawn` → `capacity.try_acquire` reservations (`semaphores.py:20-24, 56-58`) — no check-then-act race; over-cap spawns queue with relay note (`thread_manager.py:182-198`). Distinct angles via `EXPLORE_ANGLES` modulo (`goal.py:232-239`). Cap is 12 in tests/config default, not the 100 the philosophy names — a config value, not this slice's bug. | |
| 4 Token intelligence | PASS | No read classifier anywhere; playbooks/AGENTS.md teach golden-taste reading: `ServerApp.AGENTS.md:3-6` ("verify with grep/glob/read… tree is ground truth"), `fleet-scoping.md:23-25,32-33` ("cite file:line verified by read-only grep… never cite from memory"), `repro-first.md:20,30`. Hints reinforce read-only precision: `goal.py:69-75` ("precise beats exhaustive"). | |
| 5 Cost | PASS | Bounded prompt sizes: explore summary truncated to 12 000 chars (`goal.py:510`), critic notes to 1500 (`goal.py:297`), failure tails to ±800 chars (`goal.py:467-468`), evaluator signal tail 500 (`development.py:257`). Cost settle per thread at ship (`goal.py:451-452`). Kill switch / budget visibility lives outside slice. | |
| 6 Prompt caching | PASS | Persona composition is stable and cache-safe: `_persona` = mode persona + playbooks + role hint, joined deterministically (`goal.py:492-504`, `development.py:226-236`); playbooks are versioned static files served from DB (`playbooks.py:139-168`), per-run caching of the knowledge block at `thread_manager.py:79-85` with replay-identical `spawn_context`. Role hints are static module constants (`goal.py:69-120`) — only `{branch}`/`{round_no}` interpolated. | |
| 7 Parallel by default | PASS (one AT-RISK edge) | Explorers spawn CONCURRENTLY: `thread_manager.spawn_many` uses `asyncio.gather(*(_spawn_one(s) ...))` (`thread_manager.py:200`), and `goal.py:242-243` gathers `_await_thread` + `finish_thread` across all explorers. Parallel stamping falls out of per-spawn `stamp_clone` in `run_thread_container` (`manager.py:199-205`, called via `asyncio.to_thread`, `thread_manager.py:143`). No serial dependency between context mounts and writable clone (independent loop `manager.py:206+`). Explorer fan-out is read-only so no write-lock serialization. Sequential nodes (plan→refine→develop) are dataflow-justified. **AT-RISK edge:** critic→reviser rounds (`goal.py:283-317`) are serial by design (each round consumes the prior critique) — justified. | |

### Test pins

| Moat | PINNED/PARTIAL/UNPINNED | Evidence |
|---|---|---|
| 1 | PARTIAL | `test_blueprints_goal.py:564-572` wedged-thread raise pinned; `:409-430` bounded fix loop pinned; `:472-528` finish_thread per node + per fix round pinned. UNPINNED: partial-spawn cleanup on mid-fan-out raise — `:531-561` only pins label alignment under a dropped spawn, not container/slot cleanup. |
| 2 | PARTIAL | Write-scope gating pinned (`test_orchestrator_development.py:167-196, 353-374`; `test_blueprints_goal.py:328-351` writable True). UNPINNED: actual per-repo lock exclusion, owned `.git` vs worktree — `FakeThreadManager.spawn` (`conftest.py:406-417`) never touches capacity or sandbox. |
| 3 | PARTIAL | Cap clamp + distinct angles + fan-out shape pinned (`test_blueprints_goal.py:212-227`). UNPINNED: capacity-reservation race — `_FakeLaneManager.spawn_many` (`test_blueprints_goal.py:75-82`) is a **serial for-loop**, and `conftest.py:18` uses in-memory SQLite + `conftest.py:203-338` FakeRedis; concurrent `try_acquire` semantics are invisible to this harness. |
| 4 | PARTIAL | Playbook injection into persona pinned (`test_orchestrator_development.py:337-350`). Golden-taste content itself is doc-only — unpinnable by code tests. |
| 5 | UNPINNED | No test asserts truncation bounds (12000/1500/800) or `settle_cost` coverage beyond `:468`. |
| 6 | PARTIAL | Deterministic persona composition implicitly pinned by `:337-350` (prompt ends with role hint). No test asserts byte-stability of the prefix across threads of a run. |
| 7 | UNPINNED | No test measures concurrency. `test_blueprints_goal.py:212-227` asserts 3 spawns occurred but the fake runs them serially; `asyncio.gather` at `goal.py:242-243` is exercised but concurrency is unobservable with instant completed threads. A regression to serial `for` + `await` would pass every test. |

### Top findings (ranked, file:line)

1. **Moat 7 concurrency unpinned by harness** — `_FakeLaneManager.spawn_many` serial loop (`tests/test_blueprints_goal.py:75-82`) + instant-completed threads mean a serial fan-out regression in `goal.py:240-243` or `thread_manager.py:200` would pass green.
2. **Partial-fan-out orphans** — `goal.py:240-249`: if `_await_thread` raises (wedged explorer), gathered siblings keep running with no cleanup in `base.py:49-74` execute; `finish_thread` gather at `goal.py:243` never runs.
3. **In-memory SQLite + FakeRedis hide races** (`tests/conftest.py:18,85-114,203-338`): single `StaticPool` connection serializes all DB access — transaction/lock races the capacity reservations (`semaphores.py:35-59`) exist to close are structurally untestable here.
4. **Development `_await_thread` unbounded** (`blueprints/development.py:382-394`) — no `max_wait_s` unlike goal's wedged-thread guard (`goal.py:584-604`); a wedged developer/evaluator hangs a supervised run forever.
5. **Fix-round serial dependency is lock-correct but untested against real lock** — `goal.py:480-481` relies on `finish_thread` freeing the write lock before next fixer; pinned only via fake counters (`test_blueprints_goal.py:504-528`).

### Strengths

- Textbook Moat 7 swarm shape: `spawn_many` gather + per-spawn stamp + lossy-spawn-safe labeling read from each thread's own `spawn_context` (`goal.py:573-578`) with a dedicated regression test (`test_blueprints_goal.py:531-561`).
- Moat 2 hygiene throughout: explorers/planner/critic/reviser/evaluator all `writable_repo=None` (`goal.py:221,260,291,306`; `development.py:152-158`); developer workspace preserved across fix rounds with explicit lock-lifecycle comments.
- Moat 1 evidence honesty: deterministic gates persist `test_run` Events the PR gate reads (`goal.py:542-566`); evaluator-failed steps can never roll back (`development.py:294-327`, regression-pinned at `test_orchestrator_development.py:286-308,444-468`).
- Playbooks are exactly the Moat 4/6 ideal: static, versioned, mode-scoped, teaching grep-then-read and never-cite-from-memory (`ServerApp.AGENTS.md:60-105` invariants; `drizzle-transactions.md`; `repro-first.md`).


agent3:

All files read completely, plus the `spawn_many`/`spawn` capacity path in `thread_manager.py` to verify moat-3 claims end-to-end.

## Agent 3: Plan/Swarm/Debug/Ask Blueprints

### Moat verdicts (code)

| Moat | Verdict | Evidence | Notes |
|---|---|---|---|
| 1 Harness | AT RISK | swarm.py:272-283, plan.py:244-257, debug.py:247-259, ask.py:100-115 | `_await_thread` polls forever, no timeout; a thread stuck non-terminal wedges the run. Mitigations: terminal set includes interrupted/replaced (H-38), missing row = failed (swarm.py:277), decompose degrades to one slice (150-158), all-failed → FAILED + re-publish (233-243). Threads durable as DB rows; blueprint-level crash recovery not visible in slice. |
| 2 Concurrency | PASS | swarm.py:137,201; plan.py:140,173; debug.py:127,154; ask.py:93 | Every spawn is `writable_repo=None`; write lock never engages, read-only explorers exempt by construction (thread_manager.py:187). All spawns route through `capacity.try_acquire` pre-insert (thread_manager.py:86) → `commit_reservation` post-insert (129). |
| 3 Swarm | AT RISK | swarm.py:109, 162-173; thread_manager.py:86,128-129,170-201 | Concurrent spawn via `asyncio.gather` (200); reservations close check-then-act; partial failure = partial success, surfaced (`fanout_shortfall` 173, notes 215-216, 251-253), failures `_mark`ed in DB — no orphans, no rollback (correct for read-only). **Gap:** hydrate clamps the *request* (109) but `_fanout` spawns `len(decomposition.slices)` with no re-clamp — a Lead returning 50 slices under cap 12 yields 38 threads queue-retrying forever (thread_manager.py:182-198 `while True`). Cap default is 12, not philosophy's 100 (config.py:100). |
| 4 Token intelligence | PASS | plan.py:51-52,160-161,192-193,309-325; ask.py:76-78; swarm.py:52 | No classifier/gate anywhere; prompts teach grep-then-cite; citation lint flags drift, never blocks or crashes. |
| 5 Cost | AT RISK | swarm.py:199 (12k cap), 258-268; ask.py:147-148; plan.py:_present 180-229; debug.py:_present 161-202 | **plan.py and debug.py never call `settle_cost`** for planner/critic/debugger/fixer threads — the exact M-47 leak swarm.py:260-268 fixed. Agent-visible budget is worker-layer (N-A here). |
| 6 Caching | N-A | thread_manager.py:84-85 | Persona composition here; cache cut is worker-layer. Per-run knowledge block = static shape/dynamic contents, consistent with §6. |
| 7 Parallel | PASS | swarm.py:171,178; thread_manager.py:200 | Fanout + collect concurrent; decompose→fanout and draft→critique serialization are true data dependencies. Only the semaphore serializes. |

### Test pins

| Moat | Pin | Evidence |
|---|---|---|
| 3 | PARTIAL | Cap clamp + queued note pinned (test_orchestrator_swarm.py:141-159); read-only specs (212-226); partial-failure settle (289-301); full-chain 4 threads (305-317). UNPINNED: parallel spawn (fake `spawn_many` is sequential, 58-72), reservation race, shortfall-from-fanout, slices-over-cap. |
| 7 | UNPINNED | No concurrency assertion in slice. |
| 2 | PARTIAL | "READ-ONLY" prompt (224), `writable is None` (test_debug:180, test_ask:245). |
| 1 | PARTIAL | All-failed→FAILED (swarm:278-286, ask:282-294); ghost-thread (swarm:349-354); real poll loop (swarm:321-346); degrade (197-208). No timeout/crash-recovery pin. |
| 4/5 | PARTIAL | Lint-no-crash (plan:258-273); settle pinned for swarm/ask — its *absence* in plan/debug is unpinned. |

### Top findings (ranked)

1. **Cost-settle leak, M-47 class** — plan.py:180-229 and debug.py:161-202 never settle spawned threads; keys/spend leak exactly as swarm pre-M-47.
2. **Unclamped decomposed fan-out** — swarm.py:165-171 trusts slice count; over-cap slices queue-retry forever (thread_manager.py:182-198). Moat 3's bound applies to requests, not decompositions.
3. **No await timeout** — all four blueprints can hang indefinitely on a stuck thread.
4. **Swarm concurrency unpinned** — the moat-3 heart (parallel spawn, reservation race) has no in-slice test.
5. Cap default 12 vs philosophy's 100 (config.py:100); over-cap note says "queued" but excess is dropped (swarm.py:121-123).

### Strengths

Reservation held try_acquire→insert→commit; per-spec failure isolation; notebooks read from the event stream, never self-report (swarm.py:303-305); control-plane repro is tamper-proof (debug.py:99-109); tests carry real regression memory (H-51, H-49, M-63, A2, C1).

I now have the full picture. Let me compose the audit report.

Key facts:

**swarm.py:**
- `_hydrate` (swarm.py:80-123): cap from `get_settings().global_thread_cap` (default 12, config.py:100 — NOT 100 as philosophy states; philosophy says "hard cap of 100 (current limit)" — config default is 12. That's a divergence worth noting but it's config, and philosophy says cap is a "current limit, not an axiom" — 12 is a valid current limit, arguably more conservative. But the philosophy explicitly says "hard cap of 100 (current limit)". The code clamps `requested = max(1, min(int(requested), cap))` at swarm.py:109. Over-cap request publishes a note "queued: requested N threads, cap C — running R" (swarm.py:116-123). Note: the note says "queued" but the code CLAMPS (runs fewer), it doesn't queue the excess — the message is slightly misleading ("queued: requested 10, cap 4 — running 4" — actually the excess is dropped, not queued). Minor.
- `_decompose` (126-159): spawns ONE lead thread via `thread_manager.spawn` (goes through capacity.try_acquire at thread_manager.py:86). Graceful degradation to single slice on unparsable output (150-158) — moat 1 friendly.
- `_fanout` (162-173): calls `thread_manager.spawn_many(ctx.run, specs, context_repos)` — one call, and spawn_many (thread_manager.py:170-201) uses `asyncio.gather(*(_spawn_one(s) for s in specs))` — CONCURRENT spawn. Each `_spawn_one` calls `self.spawn` which does `capacity.try_acquire(repo_name)` (thread_manager.py:86) BEFORE Thread row insert, and `capacity.commit_reservation(repo_name)` after row insert (thread_manager.py:129) — reservation held between try_acquire and row insert. So the check-then-act race is closed in the thread manager, and swarm goes through it (no raw spawn). Partial failure: `_spawn_one` returns None on non-queued ThreadSpawnError; failed threads are marked "failed" in DB (`_mark`); gather returns list with Nones filtered out (line 201). swarm.py records `fanout_shortfall = len(slices) - len(threads)` (line 173) and surfaces it in auto_summary (synthesize, line 215-216) and "N explorer thread(s) failed" in _complete (251-253). So partial spawn failure = partial success, surfaced, not orphaned, not rolled back. Threads that DID spawn are tracked in DB (Thread rows) — durable.
- Queuing: spawn_many retries in a while-loop when the error contains "queued" — deterministic queueing with relay status "queued" per waiting thread (thread_manager.py:194-198).
- `_collect` (176-183): `asyncio.gather(*(_await_thread(lid)...))` — concurrent await. Reads notebooks from EVENT STREAM (tamper-proof), not self-report.
- `_synthesize`: notebooks JSON truncated to 12000 chars (line 199) — bounded prompt, cost-aware.
- `_complete`: all-failed → run FAILED + re-publish (H-39). Cost settle for explorers AND decompose/synthesis threads (M-47).
- Moat 2: all threads read-only — `writable_repo=None` everywhere (spawn at 137, 201; spawn_many passes writable_repo=None at thread_manager.py:187). Per-repo write lock never engaged — documented in module docstring (line 13-15) and spawn_many docstring (178-179). Correct: read-only explorers exempt by construction.
- Moat 1 concerns: `_await_thread` poll loop (272-283) has NO TIMEOUT — if a thread row gets stuck in a non-terminal status (e.g. "running" forever because the watchdog dies), the blueprint hangs forever. Terminal set includes interrupted/replaced (H-38). Missing thread → "failed" (line 277). But no timeout = a hang risk; the harness can wedge mid-swarm. Also: if the blueprint process crashes mid-swarm, explorer Thread rows are durable in DB, but the Run's orchestration state — is it recoverable? The blueprint itself is in-memory ctx.artifacts; a crash between fanout and collect loses explorer_thread_ids from artifacts (though Thread rows exist with run_id, so rehydration is possible in principle). No blueprint-level checkpointing visible in this slice. That's arguably owned by the orchestrator engine (outside slice), but worth flagging as AT RISK for moat 1: recovery path not visible in blueprint; threads durable in DB.
- Also `_await_thread` opens a NEW DB session every 2s per thread — with 100 threads that's 100 pollers × session/2s. Fine-ish.
- Cap check nuance: hydrate clamps requested to cap, but the DECOMPOSE step is agentic — the Lead could return MORE slices than requested_fanout, and _fanout spawns one thread per slice with NO re-clamp against the cap! decomposition.slices length is unvalidated against `requested_fanout` or cap. If the Lead returns 50 slices while cap is 12, spawn_many will attempt 50 spawns; 12 pass, the rest queue-retry... actually they'd queue (retry loop) rather than fail — the while True loop retries forever on "queued" errors, so excess slices QUEUE until slots free. That's deterministic queueing (moat 2/3 "requests beyond capacity queue rather than corrupt") — but it means the swarm can exceed the cap transiently? No — capacity.try_acquire blocks excess; they queue. But there's no upper bound on slices from the Lead; a rogue decompose could create a 10,000-slice decomposition and the blueprint would attempt 10,000 spawns (queued, but each retrying every 5s forever — a thundering-herd of sleepers, and the run would take forever). The cap clamp only applies to the REQUESTED count, not the DECOMPOSED count. This is a real gap: no `slices = slices[:cap]` in _fanout. AT RISK for moat 3's "bounded fan-out".
- Counter-proposal: decompose hint says Lead may produce fewer slices; counter_proposal recorded in auto_summary. Good.

**plan.py:**
- Read-only: writable_repo=None for planner (140) and critic (173). Citation linting via collegium_maps against golden repo, never crashes (309-325). Critic gets fresh thread, verifies via grep. Targeted reads taught in prompts: "Cite every file/symbol claim as path:line, verified by read-only grep" (51-52), "read-only grep on the mounted golden repo" (160-161). No whole-file dumping in blueprint prompts. Budget visibility: not in blueprint scope (budget_usd on Thread, settle elsewhere — plan blueprint doesn't settle costs for its threads! Wait — plan.py has NO settle_cost calls. Compare swarm.py M-47 fix and ask.py:148. Plan blueprint spawns planner + critic threads but never settles their cost in _present. Hmm — maybe the orchestrator engine settles on run end (outside slice). But swarm and ask do it in-blueprint; plan and debug do NOT. debug.py also lacks settle_cost. That's an inconsistency: gateway keys for planner/critic/debugger/fixer threads — are they released? settle_cost does spend readback; release_key deletes the gateway key. swarm.py comments say settle releases keys ("their gateway keys and spend leaked (never released/folded into the run)" — M-47). So plan.py and debug.py leak gateway keys/spend the same way swarm used to! That's a concrete moat-1/5 finding: plan.py and debug.py never call thread_manager.settle_cost for their spawned threads — the exact bug class fixed by M-47 in swarm.py still present in plan/debug.
- Moat 4: no classifier/gate. Lint flags drift, never blocks (192-193: flagged in payload, "never crash on lint failure"). PASS.
- Blast radius computed from fleet graph (102-108), failure swallowed to empty list — fine.

**debug.py:**
- reproduce: control-plane runs tests deterministically, persists test_run event (tamper-proof evidence). Read-only: writable_repo=None (127, 154). Diagnosis citations linted (133-140). No settle_cost (same leak as plan). _present sets available_actions review_plan + start_plan — human holds the kill switch (moat 5).
- Note debug.py:102: thread_id="control-plane" pseudo-thread for events — fine.

**ask.py:**
- Single read-only thread, guidebook seed, "grep/glob/read... smallest" navigation protocol (76-78). settle_cost called in _complete (147-148). Failed thread → run FAILED + re-publish (H-39). General-assistant mode without repo — no file access.
- Budget visibility to the agent? Not in these blueprints — token spend surfacing would be in the worker/prompt layer (outside slice). N-A-ish; note.

**Tests:**
- test_orchestrator_swarm.py: pins node order/determinism (116-121), hydrate default fanout=3 (130), spawn-N parsing (134-138), CAP CLAMP + queued note (141-159), unknown repo raise, fleet-wide fallback, decompose parse + degrade-to-single-slice (197-208), fanout specs read-only persona_prompt "READ-ONLY" (224), spawn_many called once with specs (221-225), collect reads notebooks from events (229-242), synthesize counter-proposal in summary, empty-swarm no spawn, all-failed → run failed (278-286), trajectories + settle incl. partial failure (289-301), full chain 4 threads (305-317), await polling real loop (321-346), missing thread = failed (349-354).
- What's NOT pinned: CONCURRENCY of spawn_many (fake spawn_many is sequential; no test asserts asyncio.gather/parallel stamping — concurrency lives in thread_manager, presumably tested elsewhere, but in THIS slice the parallel-spawn behavior is unpinned). The cap check pinned only at hydrate clamp level (monkeypatched global_thread_cap=4), not the reservation race. Partial-spawn path: fanout_shortfall computed (test asserts ==0) but NO test with spawn_many returning fewer threads than slices — the shortfall>0 path (auto_summary note at 215-216) is only exercised via synthesize test with fanout_shortfall=2 injected directly (270), not via fanout. The failure-partial-spawn path (some explorer threads failed) IS pinned in complete test (291-301: e1 failed → "1 explorer thread(s) failed"). Decompose returning MORE slices than cap → unclamped fanout: unpinned (no test).
- plan tests: hydrate paths, work-item swallow with call recording (M-63), draft/critique/present, citation lint no-crash, playbooks in persona (WU6), critic_notes always list (C1). Good depth. No cost-settle test (because code doesn't settle — the leak is unpinned).
- debug tests: reproduce confirm/pass, golden-repo-not-cwd regression (A2), test_run event persisted (C5), diagnose read-only + lint, propose, present actions, full execute with per-persona threads (H-49). Good.
- ask tests: hydrate variants, general-assistant, await polling, complete trajectories/settle/failed. Good.

Moat verdicts:
1. Harness never unrecoverable: AT RISK — _await_thread has no timeout (swarm.py:272-283, plan.py:244-257, debug.py:247-259, ask.py:100-115); a thread stuck non-terminal wedges the run forever. Mitigations: terminal set incl. interrupted/replaced (H-38), missing row = failed, decompose degrade, all-failed → FAILED. Thread rows durable in DB. Blueprint crash mid-swarm: orchestrator-level recovery not visible in slice.
2. Conflict-free concurrency: PASS — all four blueprints spawn with writable_repo=None (swarm.py:137,201; plan.py:140,173; debug.py:127,154; ask.py:93); write lock never engaged; read-only explorers exempt by construction; spawn goes through capacity.try_acquire before row insert (thread_manager.py:86,129).
3. Swarm: AT RISK — concurrent spawn via spawn_many asyncio.gather (thread_manager.py:200); reservations close check-then-act (86,128-129); cap clamp at hydrate (swarm.py:109) + queued note (121-123); partial failure = partial success surfaced via fanout_shortfall (173, 215-216, 251-253), no orphans (failed marked in DB), no rollback (correct for read-only). BUT: decomposed slice count is never re-clamped to cap — a Lead returning N>cap slices yields N spawn attempts that queue-retry forever (swarm.py:165-171; thread_manager.py:182-198). Also config cap default is 12, not 100 (config.py:100) — philosophy says 100; not a violation (cap is "current limit") but note divergence.
4. Token intelligence: PASS — no classifier anywhere; lint flags drift without blocking (plan.py:192-193, 309-325); prompts teach grep-then-cite (plan.py:51-52, ask.py:76-78, swarm.py:52).
5. Cost: PARTIAL/AT RISK — smallest-reads teaching in prompts; synthesis prompt bounded to 12k chars (swarm.py:199); auto_summary truncation. But plan.py and debug.py never settle_cost their spawned threads — the exact M-47 leak swarm fixed (swarm.py:260-268) persists for planner/critic/debugger/fixer threads (gateway keys + spend not folded into run). Budget visibility to agent: outside slice (N-A here).
6. Prompt caching: N-A in this slice — persona_prompt composition happens here (mode persona + hints + playbooks + knowledge block in thread_manager.spawn:84-85) but cache-cut ordering is a worker-layer concern. Note: per-run knowledge block appended to persona_prompt (cached per run) — shape static, contents dynamic; consistent with §6 item 6.
7. Parallel by default: PASS (with caveat) — fanout concurrent (gather), collect concurrent (swarm.py:178), decompose→fanout→collect serialization is a data dependency (legitimate). Caveat: plan draft→critique and debug diagnose→propose are serial — also data dependencies. Only capacity semaphore serializes spawn. The swarm test fake is sequential so concurrency is unpinned in-slice.

Test pins:
- Moat 3: PARTIAL — cap clamp pinned (test 141-159), read-only explorer specs pinned (212-226), partial-failure-in-complete pinned (289-301), full-chain thread count pinned (317). UNPINNED: concurrent/parallel spawn (fake spawn_many sequential, no gather assertion), reservation race (lives in thread_manager, not pinned here), fanout shortfall path from spawn_many returning fewer threads, decomposed-count-over-cap.
- Moat 7: UNPINNED in this slice (no concurrency assertion).
- Moat 2: PARTIAL — read-only pinned via persona_prompt "READ-ONLY" (224) and writable None (debug test 180, ask test 245); write-lock exemption not directly asserted (would be capacity-layer test).
- Moat 1: PARTIAL — all-failed→FAILED pinned (swarm 278-286, ask 282-294), missing-thread pinned (swarm 349-354, ask 149-155), real poll loop pinned (swarm 321-346), degrade-to-single-slice pinned (197-208). UNPINNED: await timeout (none exists), blueprint crash recovery.
- Moat 4/5: PARTIAL — lint-no-crash pinned (plan 258-273), citation collection pinned (303-305), prompt-content assertions ("grep", "READ-ONLY") pinned; cost-settle pinned for swarm (289-301) and ask (252-265) but its ABSENCE in plan/debug is unpinned (no test catches the leak).

Top findings ranked:
1. plan.py/debug.py cost-settle leak (M-47 class) — plan.py:_present (180-229) and debug.py:_present (161-202) never call thread_manager.settle_cost for planner/critic/debugger/fixer threads; swarm.py:260-268 fixed exactly this. Gateway keys + spend leak. Moat 1/5.
2. Decomposed slice count never re-clamped to cap — swarm.py:165-171 fans out len(decomposition.slices) with no min(len, cap); hydrate clamp (109) applies only to the REQUEST. Lead returning 50 slices under cap 12 → 38 threads queue-retry forever (thread_manager.py:182-198 while True). Moat 3 bounded fan-out weakened.
3. _await_thread no timeout in all four blueprints — a thread stuck in non-terminal status wedges the run indefinitely (swarm.py:272-283 etc.). Moat 1.
4. Swarm concurrency unpinned — test fake spawn_many is sequential (test_orchestrator_swarm.py:58-72); nothing in-slice asserts asyncio.gather parallel spawn or the reservation race. Moat 3/7 core claim untested at blueprint level.
5. Cap default 12 vs philosophy's stated 100 (config.py:100) — divergence to confirm (may be intentional current limit).
6. Over-cap note says "queued" but excess is dropped, not queued (swarm.py:121-123) — misleading UX, minor.

Strengths:
- spawn_many: gather-based concurrent spawn, per-spec retry only on "queued", non-queue failures isolated (None, marked failed), reservation held try_acquire→row insert→commit_reservation.
- Collect reads notebooks from event stream, not self-report (swarm.py:303-305).
- Decompose degrades to single slice instead of dying (150-158).
- fanout_shortfall + failed explorers surfaced in auto_summary; all-failed → run FAILED with stage re-publish (H-39).
- Citation lint never crashes, flags drift (plan.py:309-325); debug repro is control-plane executed, tamper-proof test_run event.
- Tests are unusually deep: H-51 real poll loop, H-49 per-persona threads, M-63 swallow-path observability, A2 golden-root regression, C1 list-shape pin.

Word count: need under 600 words. Dense. Let me write it.


agent4:

Key observations now complete. Let me synthesize.

**Moat 1 (sandbox/manager.py):**

- `wait_for_container_exit` (manager.py:248-259) — poll loop, 0.25s interval, 15s timeout. BUT: on timeout it only logs a warning and RETURNS — the caller (run_manager.py:416-419) then proceeds to spawn the replacement which mounts the session volume. So the wait is best-effort: after 15s, the replacement mounts while the old container may still be alive → the exact corruption the moat forbids. This is AT RISK, not full PASS. Also note `control.kill` (pub/sub, control.py:29-30) is fire-and-forget — if the worker never receives the kill (Redis pub/sub lossy), the container keeps running, wait times out after 15s, and the replacement mounts anyway. Also, kill_replace never calls `stop_container` on the old container — it relies on the worker self-terminating on the kill message. If the worker is hung, the container stays alive past the 15s timeout. Actually wait — let me double check: kill_replace_thread calls `self.control.kill(thread_id)` (pub/sub) then `wait_for_container_exit`. It never force-stops the container. So a hung worker → timeout → corruption risk. That's a genuine finding: no force-stop fallback before the wait expires, and no hard failure after timeout.

- Test pin for wait-for-exit: `test_kill_replace_waits_for_old_container_before_spawn` (test_orchestrator_run_manager.py:646-675) — pinned at the orchestrator level (outside my test slice, but referenced). Within my slice's tests (test_sandbox_manager.py), `wait_for_container_exit` itself is NOT tested — no test of the poll loop, timeout behavior, or the proceed-on-timeout semantics. So PARTIAL.

- Shredding: `shred_workspace` (manager.py:261-266) — rmtree with `ignore_errors=True`. Called at run end in run_manager.py:272 (abandon path) and test_orchestrator_run_manager.py shows it's called on finish paths too. `ignore_errors=True` means a failed shred is silent — workspace remnants survive silently. Minor. Survivors: session volume mounted per-lane (manager.py:190-192), events to DB (bus.py), transcript JSONL. Does anything the harness needs live only in the workspace? `.mcp.json` is stamped into workspace but re-derivable. The session volume is separate (sessions_dir, not workspaces_dir). Evidence dir separate (playwright.py:41). OK.

- Orphan containers on backend crash: `reconcile_on_boot` (run_manager.py:616-654) marks threads stopped and releases keys but does NOT stop containers — it never calls stop_container or lists containers by name prefix. Containers are `remove=False` (manager.py:225), so on backend crash, running containers keep running (they're detached), and exited containers persist as corpses with no reaper. The workers may self-exit when their work completes, but a backend crash mid-run leaves containers running with no backend to ingest their events; on boot, reconcile marks threads stopped but leaves the containers running — orphaned writers on session volumes. If the user then resumes a thread, a new container mounts the same session volume while the orphan may still be alive → moat-1 exposure. AT RISK finding. Actually, do heartbeats handle this? services/heartbeats.py exists (not in slice). The heartbeat service may stop dead containers — not in my slice, but grep showed heartbeats.py:113 mentions kill_replace/finish_thread/abandon. I shouldn't over-claim; I'll note reconcile_on_boot doesn't stop containers and no name-prefix reaper exists in the code I saw.

**Moat 2 (session volumes):**
- `session_subpath` = sessions_dir/run_id/thread_id (manager.py:46-50) — per-lane. Pinned by test_session_subpath_creates_dirs (test_sandbox_manager.py:96-108, M-70).
- Resume mounts the PRIOR thread's session dir (manager.py:190-191) — cross-thread by design for resume, guarded by wait-for-exit. 
- Shared mounts: pip/npm cache volumes shared rw across ALL threads (manager.py:194-197) — deliberate shared caches, not session state. Fine for moat 2 (caches, not .git or sessions).
- Context repos: read-only bind of golden (manager.py:206-210) — shared read-only, fine.
- Workspace dest keyed by run_id only, not thread_id (manager.py:63: workspaces_dir/run_id/repo.name) — two writable threads in the SAME run on the same repo share one workspace path; fresh=False exists precisely because the second writable thread reuses it (manager.py:53-60). That's a deliberate run-scoped workspace, not per-thread. Moat 2 says per-thread owned .git — but within a run, the fix loop shares. This is a design decision documented in the docstring; the per-repo write lock is at thread level but the workspace is run-scoped. Given the docstring explicitly decides this (goal-mode fix loop), I'd call it PASS with a note — actually wait, could two CONCURRENT writable threads in the same run collide on the same workspace? The docstring says fresh=False is for "a run's SECOND writable thread (goal-mode fix loop)" — if the second thread spawns while the first is still alive, both mount workspaces_dir/r1/ServerApp rw simultaneously → two containers writing one workspace. Is there a guard? kill_replace marks old thread terminal before spawning, and the fix loop presumably runs after the first thread completes. Not verifiable in my slice. Worth flagging as a note.

**Moat 1 (events/bus.py):**
- Durable: Redis Streams + consumer group + xack AFTER DB commit (bus.py:161, 177). At-least-once: ack after commit; crash between commit and ack → redelivery → duplicate Event row? Is there idempotency? `session.add(Event(...))` — no dedupe on (thread_id, seq) or sdk_message_uuid visible. Redelivery would insert a duplicate row unless the DB has a unique constraint. Not in my slice (Event model). next_seq advance is monotonic-guarded (bus.py:143). So at-least-once without visible idempotency in this file — possible duplicate events on redelivery. Worth flagging as a note (dedupe may live in the model/constraints, outside slice).
- Poison pill: dead-letter + ack (bus.py:122-133), and H-43 generic-exception dead-letter (bus.py:102-120). If the dead-letter write itself fails (Redis down), message stays unacked → retried (bus.py:116-118). Good.
- Transient deltas ride pub/sub only (lossy by design, no seq, never enter history) — relay.py:92-119. Fine.
- Ordering: relay delivers strictly by seq per docstring (relay.py:1-4); the relay itself doesn't reorder — UI renders by seq. Streams preserve order per key; single consumer CONSUMER="backend-1" processes in order. publish_step happens after commit+ack, in message order. Ordering guarantee is by seq at render, not by delivery — reasonable.

**Moat 6 (gateway/litellm.py):**
- This file is key management + spend readback only. The actual LLM proxying is LiteLLM itself (external process); workers call ANTHROPIC_BASE_URL=gateway directly. So cache-control header preservation is LiteLLM's config, not this code. N-A for cache headers in this file — but note: no per-request mutation here because there are no requests proxied here. Prompt caching concerns: mint_key scopes models (litellm.py:47). No cache-busting in this file.
- Token usage accounting: `key_spend` (litellm.py:58-62) + `read_spend_reconciled` with grace window (64-72). Also the "turn complete" event carries `usage` in detail (bus test line 81) — usage flows through events.

**Moat 5 (cost):**
- Per-lane virtual key with max_budget (litellm.py:42-51) — correctly priced enforcement at gateway. BUDGET_USD injected into thread env (manager.py:135). Spend read back at run end with reconciliation poll. Cost visible. PASS for this slice.

**Tests:**
- wait-for-exit: orchestrator-level pin exists (test_orchestrator_run_manager.py:646-675, G-12) but NOT in my slice's test files; manager's wait_for_container_exit itself untested (no poll/timeout test in test_sandbox_manager.py). PARTIAL.
- Shredding: test_shred_workspace_removes_dir + missing-noop (test_sandbox_manager.py:275-284) — PINNED (though ignore_errors=True silent-failure path unpinned).
- Event durability: test_process_persists_event_and_acks asserts xack (H-52, test_events_bus.py:52-55); dead-letter tests (108-139); H-43 loop-level dead-letter not directly tested? test_loop_handles_xreadgroup_response_error covers ResponseError. The H-43 per-message exception path in _loop — is it tested? Looking... no direct test of a _process raising non-validation error inside _loop being dead-lettered. PARTIAL-to-PINNED. Ack-after-commit ordering not pinned (can't easily be). Redelivery/duplicate not pinned.
- Relay: slow-consumer eviction + sentinel pinned (M-53/M-67, test_events_relay.py:78-92). Ordering by seq NOT pinned (UI-side). 
- WS: auth pins solid (4401/4404, ownership), payload+order pinned (H-53).
- Gateway: spend reconcile poll count pinned (M-68); retry backoff pinned incl. transport errors (M-52 implied).
- Session path placement pinned (M-70). Preserve-workspace pinned (fresh=False, no rmtree, no git). stop_container actually stops+removes pinned (M-71). Purge newest-mtime pinned (M-72).

Top findings ranked:
1. AT RISK — wait_for_container_exit times out at 15s and only logs (manager.py:253-259); kill_replace then mounts the session volume anyway (run_manager.py:416-419, 443-449). The moat's "only thing that prevents corruption" is a best-effort poll with proceed-on-timeout. Also no force-stop fallback: kill is pub/sub fire-and-forget (control.py:29-30); a hung worker never dies, wait expires, replacement mounts a live volume.
2. AT RISK — orphan containers on backend crash: reconcile_on_boot marks threads stopped and releases keys but never stops containers (run_manager.py:616-654); containers run remove=False (manager.py:225) with no name-prefix reaper; a resuming thread can mount a session volume an orphaned container still writes.
3. Note — at-least-once ingest without visible idempotency: ack after commit (bus.py:161,177) means crash-between → redelivery → duplicate Event row unless a DB unique constraint dedupes (model outside slice).
4. Note — run-scoped workspace (workspaces_dir/run_id/repo, manager.py:63) shared by a run's writable threads; safe only if the second writable thread never overlaps the first — enforced nowhere in this file.
5. Minor — shred_workspace uses ignore_errors=True (manager.py:266): silent shred failure leaves remnants; no log.
6. Minor — shred happens on abandon path (run_manager.py:272) but session_id capture depends on a single event title string "turn complete" (bus.py:150-156) — if the worker renames it, resume silently breaks. (Test pins the contract though.)

Strengths:
- H-43 poison-pill isolation; dead-letter never drops, Redis-down → retry (bus.py:102-120).
- Ack after DB commit; transcript failure can't drop event (bus.py:165-177).
- M-72 newest-mtime purge; M-70 path pin; M-71 stop/remove verification; M-53 sentinel; M-68 poll-count pin; H-52 ack pin; H-53 payload+order pin.
- Per-lane virtual keys with max_budget + reconciled spend readback (litellm.py:42-72).
- WS per-user scoping enforced at endpoint (ws/events.py:48-51).

Word budget: under 600. Let me write it.


agent5:

All evidence gathered. Compiling the report.

Now I have the full capacity picture:

- POST /runs → `run_manager.create_run` (runs.py:96) — NO capacity check at the API/run level. Run row created in QUEUED stage, `_execute` task tracked async. Response is 200 with the serialized run (stage="queued" presumably in the payload via run.stage).
- Capacity enforcement happens at thread spawn: `capacity.try_acquire` (thread_manager.py:86) BEFORE container start — reservation held between try_acquire and row insert (semaphores.py:23-24, 61-68: release == commit).
- Queue behavior: only `spawn_many` (swarm path) retries on "queued" with a poll loop + relay "queued" status announcement (visible). Single-thread spawn (self.spawn called elsewhere, e.g. Lead spawn in blueprints) — ThreadSpawnError propagates. Need to check: does the blueprint catch it? The "queued" reason string matching is string-sniffing ("queued" not in str(exc)) — fragile but functional. Importantly, queueing is via retry-poll, not an ordered queue — there's no FIFO order guarantee: multiple waiting threads poll every 5s and race try_acquire; order is random, not ordered. The philosophy says "visible and ordered, never silent and random". Visibility: yes (relay publish). Ordered: NO — poll-and-race is random order. That's an AT RISK/partial violation note for moat 2 at the orchestrator boundary, though my slice is the API layer. For the API layer specifically: POST /runs accepts without capacity check — but that's arguably correct per design (queue rather than reject); the run is created QUEUED and visible. There's no 202/409; it's 200 + queued stage. Deterministic queueing is delegated down-stack.

- Idempotency: POST /runs has NO idempotency key — double-click = two runs (two rows, two _execute tasks). VIOLATION of retry-safety. Also POST /sessions/{id}/resume — resume_run transitions the same run row to QUEUED again and re-executes; double-click on resume → two _execute tasks on the same run? Let me not go too deep; note it.

- Approvals double-apply: services/approvals.py:194-211 — `session.get`, check `decision is not None`, set, commit. TOCTOU: two concurrent decide POSTs with different sessions can both read decision=None before either commits → double-resolve published to worker BLPOP twice. No row lock (no with_for_update), no unique constraint guard. API maps ValueError → 409 (approvals.py:96), and the test pins the 409 on "already decided" (test_api.py:884-895) but only the sequential case, not concurrent. AT RISK.

- Webhooks: triggers.process inserts dedupe row FIRST with unique (source, external_id, revision) constraint → duplicate returns {"status": "duplicate"} (triggers.py:188-196, 317-319). Idempotent on duplicate delivery. Out-of-order: revision is part of the dedupe key, so different revisions both process — no ordering guard visible, but loop prevention + rate cap exist. Mostly PASS for idempotency.

- main.py startup: lifespan starts ingest, approval_service, heartbeat_persister, fetch loop, reconcile_on_boot (zombie reconciliation — good moat 1). NO alembic migration check/upgrade at startup (alembic exists but not invoked in lifespan) — deploys must migrate out-of-band; serving against a stale schema is possible. Graceful shutdown: stops ingest/approvals/heartbeats/fetch/relay/control/prewarm — but NOT run_manager tracked tasks (no run_manager.shutdown; _track'd tasks at run_manager.py:62 are never drained/cancelled in teardown). In-flight runs' asyncio tasks die with the event loop — reconcile_on_boot on next boot picks up zombies (that's the mitigation, and it's a deliberate design: reconcile on boot). So AT RISK mitigated by reconcile_on_boot.

- Moat 5 cost visibility: runs._serialize exposes cost_usd + tokens (runs.py:86); threads endpoint exposes cost_usd/budget_usd per thread (runs.py:175); campaigns.py:36-38 /stats/cost dashboard; team.py:95-108 admin metadata stats with total_cost_usd. PASS. No silent enforcement in API layer (budget enforcement would be in gateway mint_key max_budget_usd — thread_manager.py:133 — that's a hard cap at the gateway, arguably silent enforcement, but outside API slice; flag in notes).

- Moat 4: no read classifier in API layer. classify_text in runs.py:249 is user-intent classification (button/text routing), not content gating. Knowledge PHI checkpoint is human approval (knowledge.py:53). PASS/N-A.

- Moat 7: repos.py:90 uses asyncio.create_task(onboard(...)) — fire-and-forget async onboarding, parallel. runs intent handlers await sequentially but that's control flow. N-A/PASS.

- Moat 1 partial rows: create_run commits run row then registers + tracks task (run_manager.py:104-113) — if _execute fails, run row exists with stage; reconcile_on_boot handles zombies. API-level: _persist_user_message uses FOR UPDATE (runs.py:45) — good. Error paths in routers mostly map exceptions to 4xx without partial writes. repos.py add_repo: register_repo commits, then asyncio.create_task(onboard) — if onboarding fails, repo row exists with status machine (status_detail) — visible failure, acceptable. auth.py first_login: multiple sessions but each committed independently — display_name update separate commit (auth.py:98-105); if that fails, user is still active with pin — minor partial state but recoverable.

Test pins assessment:
- Moat 2/3 queue-full behavior: UNPINNED. No test for capacity-full on POST /runs or spawn queueing in test_api.py. Fanout pass-through pinned (test_api.py:439-445). create_run happy path + 422 unknown mode pinned (383-408).
- Double-approval: PARTIAL — 409 on already-decided pinned via mocked service (884-895), but no real double-POST concurrency test.
- Webhook idempotency: PARTIAL — bad signature 401 pinned (1374-1381), full ingress pinned (1384-1405), but duplicate-delivery ("status": "duplicate") NOT tested in test_api.py.
- Moat 1: PARTIAL — 404/IDOR guards heavily pinned (G-06, C-08..C-11), resume-same-run pinned (979-1016), reconcile_on_boot not tested here.
- Moat 5: PARTIAL — cost fields appear in serializers but no test asserts cost_usd/tokens presence in run serialization; team stats exact-cost pinned (240-251); /stats/cost not tested in this file (maybe elsewhere).
- Moat 4: N-A — nothing to pin (no classifier exists).
- Moat 7: UNPINNED in this file.

Also note runs.py:312: `session.get(Run, run_id).mode if session.get(Run, run_id) else None` — double session.get, minor.

Also POST /runs returns 200 (not 201/202). Minor.

One more check: does the runs.py create_run response distinguish queued vs accepted? It returns _serialize(run) with stage — run.stage after transition QUEUED. So client sees stage="queued" — visible. Good.

Also worth noting: runs.py intent endpoint — APPROVE_PLAN flows through plan_service.approve_plan which raises ValueError if not draft → 422 (pinned by test 1040-1050: stale approve → 422, never double-apply). That's the plan-approval idempotency — pinned.

Word budget: under 600 words. Let me write the report.


agent6:

All files read. Let me verify the dedupe constraint and session config that two code paths depend on.

All files read completely. Let me analyze against the moats.

Key observations:

**triggers.py (Moat 1 — CRITICAL):**
- `_log_event` (line 188-209): dedupe row inserted FIRST with unique (source, external_id, revision) constraint; IntegrityError → returns None → `{"status": "duplicate"}` (line 317-319). This IS the idempotency contract — duplicate webhook = no duplicate run. STRONG.
- BUT: not fully atomic — the dedupe row commits (line 201), then run creation happens later (line 381) via `run_manager.create_run`. If process crashes between commit of log row and `create_run`, the event is logged "received" but never processed — a restart doesn't re-drive it (no recovery sweep for status="received" rows). Actually wait — the log status is "received" and there's a drain_queued for "queued" rows, but nothing re-processes "received" rows stuck mid-flight. So crash mid-process = event lost (dedupe says "duplicate" on retry from ADO, but the run never got created). Hmm — ADO service hooks do retry on non-2xx; if process() fails before returning, the ingress presumably returns an error, ADO retries, and the retry hits `_log_event` → returns None → "duplicate" → the event is NEVER processed. That's a real at-most-once gap: crash between dedupe-insert and run-creation loses the event permanently. This is an AT RISK finding for Moat 1. The dedupe-first pattern is exactly the tension: insert-first-then-process gives at-most-once unless there's a recovery path for status="received" rows. There is none visible in this file.
- Verdict recording durable: `_persist_verdicts` deletes + re-inserts (idempotent, line 223-240); `_set_log` writes status; durable in DB. Good.
- Rate limiting (guardrail 3) queues with status="queued" and `drain_queued` re-starts them — durable, recoverable. Good.
- Multi-trigger fan-out: `for t in matched:` (line 343) — SEQUENTIAL loop with `await` per trigger. Moat 7: triggers do NOT fan out concurrently — matched triggers are processed one at a time (each awaits create_run). Also drain_queued processes items serially (line 500). This is a Moat 7 AT RISK/VIOLATION — small N, but the philosophy says "parallel by default." It's a serial loop over matched triggers.
- Trust: autonomy="gated" always (line 384, 525). Good.
- HMAC signature verify fail-closed (532-539). Good.

**delivery.py (Moat 1):**
- Delivers branches → pushes → PRs → merge. 
- Idempotency: `open_pr` — no idempotency key or dedupe. If called twice, creates TWO PRs in ADO (no check for existing open PrLink before creating). Crash mid-delivery: push succeeded, PR created in ADO, but crash before PrLink commit (line 263-264) → PR exists in ADO, no DB record → retry creates a SECOND PR (branch already exists → push may fail or PR create may conflict). The ADO PR create for same source/target would likely fail on ADO's side (ADO rejects duplicate active PR for same source/target branch — returns 409), so there's some natural backstop, but the code doesn't catch it and link to the existing PR. Best-effort, not at-least-once with idempotency keys. AT RISK.
- `merge_pr`: complete_pull_request first, then DB update; if DB fails → logged critical for reconciliation (M-38, lines 342-352), re-raised. Good handling of the dual-write, though reconciler is a log line, not an automated replayer.
- Retry safety of merge: merge_pr filters status="open" — after successful merge status="merged", so a second merge_pr call raises "no open PR". Safe.
- mark_merged closes native-UI loop (G-16). Good.
- Evidence package: tamper-evident sha256 pinned in PR body vs DB copy (lines 59-64, 236). Strong audit.
- FLEET_PAT never in command line — env only (line 206-209). Good security.
- commit_pending: `checkout -B` + `add -A` + commit under bot identity — deterministic safety net; "checkout -B keeps working tree + HEAD so commits never lost." Moat 1 aligned (nothing lives only in workspace — uncommitted work gets committed before shred presumably).

**approvals.py (Moat 1):**
- Approval rows durable in DB (line 144-152). Idempotent create: `existing = session.get(Approval, approval_id)` skip re-insert (M-34). Good.
- Double-decide safe: `decide` checks `approval.decision is not None` → ValueError (line 199-200). BUT: it's a check-then-act within a single session without row locking (`session.get` then set). Two concurrent decide calls in different sessions could both pass the None check and both commit — last-writer-wins. No `SELECT ... FOR UPDATE` / no DB constraint on decided state. Race window small but real. AT RISK (minor).
- Timeout semantics: `_expire_stale` sweep every 30s stamps decision="timeout" (distinguishing from human deny — audit trail), fans out approval_resolved. Worker's BLPOP already denied. Good: timeout = DENY + notify, Autonomous never bridges (docstring line 3).
- After `decide` commits, `control.resolve_approval` publishes to worker's blocking BLPOP (line 207). If decide's DB commit succeeded but service crashes before resolve_approval, worker times out → DENY. A human approved but the worker denies on timeout — the DB row says approved; is there reconciliation? Not visible here; the timeout-deny happens worker-side. That's a potential inconsistency but arguably safe-fail (deny is the safe direction).
- Note line 208: `approval.run_id` accessed AFTER session.close() — detached instance attribute access. It works only because run_id was loaded before close (attributes loaded while attached stay accessible unless expired). commit() expires attributes by default in SQLAlchemy (expire_on_commit=True default)! So `approval.run_id` at line 208 after session.close() would trigger DetachedInstanceError IF expire_on_commit is True. Depends on session config in app.db.base — worth flagging as a possible bug. Actually they returned `approval` (line 211) and tests access `out.decision` etc. — test passes presumably, so the session factory likely has expire_on_commit=False. Can't confirm from my slice, but the test test_decide_records_and_publishes passes and accesses out.decision, out.decided_by, out.decided_at after close — so expire_on_commit must be False in their config. OK, not a finding, but fragile pattern.
- Approvals survive restarts? The consumer group xgroup_create with id="0" mkstream — messages persist in Redis stream; on restart, `register_run` must be called for streams to be consumed. Where does run_streams get repopulated on restart? Not in this file — `register_run` called externally. If backend restarts, run_streams (in-memory set) is EMPTY → no streams consumed until re-registered. Pending approvals in Redis streams would stall. Is there recovery? Not in this file. However, cards are durable in DB (Approval rows) so UI can re-fetch; but the xreadgroup consumption of NEW requests depends on registration. Flag as a question/AT RISK for restart-survival: in-memory `run_streams` is volatile state that approvals routing depends on.

**heartbeats.py (Moat 1):**
- It's a persister: Redis pub/sub → Thread.heartbeat_at DB stamp. Throttled writes (10s) with status-change bypass (M-41 fixes first-beat drop).
- Dead-worker detection: NOT here — this file only stamps liveness. TTL key lives worker-side in Redis ("worker heartbeats into Redis every 15s (a TTL key + pub/sub)"). What happens on death — requeue/fail/orphan — is NOT in this file. The docstring says "watchdog ground truth" — the frontend watchdog reads Thread.heartbeat_at. So dead-worker DETECTION exists via stale heartbeat_at (3 min threshold per comment), but remediation (requeue/fail) is out of slice. From this slice: heartbeats provide the ground truth; the failure-handling is elsewhere. N-A/PARTIAL for death-handling; the persister itself is solid: terminal-status protection (stale beat never resurrects — line 115), rollback on error never kills loop (line 118-120).
- One concern: `_persist` updates `heartbeat_at` even for terminal threads (line 108 outside the ACTIVE_STATUSES guard) — by design ("but liveness still recorded").

**Moat 5 (cost):** In this slice — triggers have rate_limit_per_hour (a cost/blast-radius bound), queued not silently dropped (visible status, drained later). No budget-overrun flagging in these files; delivery evidence package carries total_cost_usd into the PR body (line 243 — cost visible on the PR!). That's cost visibility surfaced to the human reviewer. Good touch. No silent kills in slice. Trigger rate-limit overflow → status="queued" (visible, durable), not dropped. Aligned.

**Moat 7 (parallel):** process() loops matched triggers SEQUENTIALLY with awaits (line 343); drain_queued serial (line 500: `for item in items` with awaits). delivery git ops serial by nature (same workspace — justified). Approvals xreadgroup count=50 batch. Fan-out in triggers is serial — for a multi-trigger match this is a minor serialization; arguably justified for deterministic ordering, but the philosophy says serialization must be a justified decision. It's not documented as such. AT RISK (minor).

**Tests:**
- test_services_triggers.py: duplicate-revision idempotency PINNED (test_duplicate_revision_is_idempotent, line 102-109 — asserts only 1 run created). Loop prevention pinned. Fail-closed identity pinned. Flap/nudge pinned. Rate-limit queue pinned, drain pinned, drain-owner pinned (G-19). Per-trigger DB-level rate scope pinned (M-39). Signature fail-closed pinned. NOT pinned: crash between dedupe-insert and run-create (at-most-once gap); no test for process() raising mid-way leaving status="received" row.
- test_services_delivery.py: evidence gate pinned (incomplete blocks), PlanStep-rows-over-JSON pinned (C1), rebase conflict abort pinned, open_pr ordering + sha256 pinned, merge native-UI handoff pinned (G-16), mark_merged pinned + no-open-raises. NOT pinned: duplicate open_pr (double PR creation / no idempotency); merge_pr DB-failure-after-ADO-success path (M-38 reconciliation branch untested — no test forcing commit failure after complete_pull_request); no crash-mid-delivery recovery test.
- test_services_approvals.py: double-decide pinned (line 80-90). Timeout sweep pinned (expire_stale → decision="timeout" + fanout; throttle between sweeps; DB error swallowed G-17). Idempotent create? test_create_card_persists_and_fans_out — but no test calling _create_card twice with same approval_id to pin the M-34 skip-reinsert path. Actually let me check... no duplicate-create test. UNPINNED for M-34 idempotency. Poison-message ACK path (M-35) untested — no test for _create_card raising → ack-and-continue. Concurrent decide race untested (and can't easily be in SQLite).
- test_heartbeats.py: status-change-bypass pinned, throttle pinned, None-status pinned (C-17 fixed vacuous tautology), rollback-on-error pinned (G-22), stale-beat-never-resurrects pinned (terminal protection). NOT pinned: dead-worker detection/remediation (out of scope — persister only); the pub/sub _loop itself untested (only _persist/_handle paths... actually _handle not directly tested either — _persist tested directly).

Missing dead-worker test: heartbeat TTL expiry → what happens. Out of slice's code too (no detection code in heartbeats.py). So UNPINNED/N-A: no code in slice does detection; detection presumably elsewhere (watchdog). The heartbeat persister is the ground-truth writer, well-tested.

Let me also verify the TriggerEventLog unique constraint exists in the model to confirm the idempotency contract claim. Quick grep for the model.


agent7:

## Agent 7: Edge Services & Integrations

### Moat verdicts (code)

| Moat | Verdict | Evidence | Notes |
|---|---|---|---|
| 1 Harness never unrecoverable | PASS (slice) / AT RISK (gaps) | `session_store.py:77-107` upload/materialize/purge never raise; `transcript.py:10-11` writes best-effort, `transcript.py:23-27` lives outside `sessions_dir`; `responder.py:96-100` missing-run returns durable verdict not 500 | Two-step decay hooks exist here (`session_store.purge`, `transcript.delete`); the sweeper itself is `sandbox/manager.py:268-290` (outside slice). No partial-hydration failure mode in `hydration.py` (all reads degrade to `[]`/fallback). |
| 2 Conflict-free concurrency | PASS | `sessions.py:55-78` replay hard-scoped `run.created_by != user_id → []`; `session_store._key` (`session_store.py:45-46`) is `{run_id}/{thread_id}.tar.gz` — per-lane by construction; `responder.py:52-64` nudges existing thread instead of duplicate-spawn (M-40) | Cross-contamination guarded by key namespacing + tar-slip containment (`session_store.py:59-74`, C-12 real-path check). |
| 3 Swarm bounded fan-out | N-A | — | No fan-out in slice. |
| 4 Token intelligence (no classifier) | PASS | `guardian.py:35-67` breaker counts attempts/signatures from event log — an action-level gate on *spawning fix runs*, never on what the model sees; `responder.py` routes full comment text (truncated at 4000 chars, `responder.py:35`) into the prompt | Guardian is a circuit breaker in code (the "liver rule"), not a read classifier. No upstream admission layer anywhere in slice. |
| 5 Cost visible | N-A | — | Demo seed carries `usage` tokens (`seed_users.py:94`) but no cost logic in slice. |
| 6 Prompt caching | N-A | — | Personas are static strings (`responder.py:24-30`, `seed_users.py:27-77`); no per-turn prompt assembly here. |
| 7 Parallel by default | N-A | — | Slice is event-driven singles; nothing serializes that could fan out. |

### Test pins

| Moat | PINNED/PARTIAL/UNPINNED | Evidence |
|---|---|---|
| 1 Retention decay (30d/12mo) | PARTIAL | `test_services_session_store.py:42-53` pins purge idempotency on the mirror; `test_services_sessions.py:112-124` pins volume-exists gate. **UNPINNED**: no test in slice wires `session_store.purge`/`transcript.delete` to the sweeper, no 12mo events-TTL test here. |
| 1 Transcript survivor | PARTIAL | `transcript.delete` exists for TTL (`transcript.py:63-69`) but **no test file for transcript.py in slice** — append/read/malformed-line skip unpinned here. |
| 2 Session isolation / per-lane | PINNED | `test_services_sessions.py:94-100` owner-scope; `test_services_session_store.py:74-122` tar-slip + sibling-prefix (C-12/G-15); `test_services_guardian_responder.py:170-184` resume-from-volume. |
| 4 Guardian is action-gate not classifier | PINNED | `test_services_guardian_responder.py:103-131` max-attempts + repeated-signature halts; `:134-145` gated + system-owned. |
| 1 Push never fails action / idempotent push | PARTIAL | `test_services_push_autonomy.py:27-45` tallies + prune + no-op; **UNPINNED**: no test that a push send failure doesn't propagate (default sender skip path only implied), no idempotent-push-dedup test. |
| 1 ADO write idempotency | UNPINNED | `test_ado_client.py:122-162` pins happy-path PR create/complete only; no retry/duplicate-comment test. |

### Top findings (ranked, file:line)

1. **ADO writes have no retry/idempotency** — `ado/client.py:171-200` `create_pull_request`/`complete_pull_request` are single-shot POST/PATCH with `raise_for_status`; a caller retry after a timeout creates a duplicate PR/comment. No idempotency key, no dedupe. (Responder/guardian re-triggers amplify this.) Moat 1 risk.
2. **Retention sweep wiring untested in slice** — `session_store.purge` and `transcript.delete` are the decay hooks but no test here proves the sweeper calls both (volume AND mirror AND transcript) — silent-loss surface if drift. Moat 1.
3. **`transcript.py` has zero direct tests in slice** — malformed-line skip (`transcript.py:49-52`), `after_seq` null-seq fix (L-19, `:53-60`), and append-on-committed-event contract are unpinned. Moat 1.
4. **Push sender exceptions could propagate through injected senders** — `push.py:88-99` calls `send(...)` without try/except around the call itself; the *default* sender is safe, but a raising injected sender escapes `send_to_user`. Minor Moat 1.
5. **`hydration.PrewarmPool.prewarm` partially mutates then reports** — `hydration.py:78-95` appends to `self.requested` per repo with no transaction; a mid-loop failure leaves recorded intent for some repos (acceptable for a stub, but the pattern would be a partial-state hazard in the real pool). Moat 1 (low).

### Strengths

- **Tar-slip containment done right and pinned twice** (`session_store.py:59-74`; tests `:74-122`) including the sibling-prefix escape (C-12) with an assertion tightened to the guard's own `ValueError` (G-15).
- **Fail-loud identity resolution** (`ado/client.py:99-102`, the two-Alis rule) and fail-fast empty-PAT (`:40-46`, L-26) — both pinned (`test_ado_client.py:49-67`).
- **Circuit breaker in code, never prompt** (`guardian.py:35-67`) with the H-25 verdict-counting fix pinned (`test_services_guardian_responder.py:103-131`).
- **Graceful degradation everywhere**: push skipped-without-pywebpush (`push.py:56-73`), hydration `[]`/task fallbacks (`hydration.py:41-66`), session-store never-raises (`session_store.py:85-87,96-98`) — and each is test-pinned.
- **Seed is idempotent and secret-free**: bootstrap admin defaults empty, opt-in via env (`seed_users.py:115-132`, C-14); system user `pin_hash=None` (M-56) pinned (`test_auth_seed_users.py:10-23, 68-109`).


agent8:

All files and both injection sites read. Compiling the verdict.

## Agent 8: Knowledge Services

### Moat verdicts (code)

| Moat | Verdict | Evidence | Notes |
|---|---|---|---|
| 6 Caching | PASS | `knowledge.py:354-368` per-run LRU block cache (cap 256), version-stamped, invalidated on approve/reject (`:212,:228`); `thread_manager.py:84` appends block *after* persona (dynamic below cut), stored in `spawn_context` for replay-identical prompts; `playbooks.py:139-168` renders static DB content in stable `playbook_ids` order; `development.py:235` inserts playbooks between persona and fallback — static, above cut. No timestamps anywhere in rendered blocks | Episodic lines embed `(run {id})` (`knowledge.py:342`) — stable within the per-run cache, not poison |
| 4 Token intel | AT RISK | `knowledge.py:288-289` reranker told to "Omit irrelevant ids"; `:327` truncates to `top_k` | It's ranking, not a classifier, and the corpus is human-approved — but non-pinned items are unreachable *in this slice*; no evidence the agent can query the full corpus on demand |
| 5 Cost | PASS | top-k (`:316`), trigger[:300] (`:283`), task[:2000] (`:290`), episodic[:600] (`:342`), trajs limit 50 (`:253`); `ideas.py:244-254` body[:2000]/comments[:20]/voice[:400]; `guidebooks.py:116` MAX_LINES=200 enforced; `evidence.py:73-74` stdout[-2000:]; `distiller.py:65-77` summaries[:20], ≤5 candidates | Smallest-chunk discipline everywhere; Counsel transcript intentionally unbounded ("reads the ENTIRE thread", `ideas.py:151-159`) — a stated moat-4 choice |
| 1 Harness/durability | AT RISK | All state in DB (`KnowledgeItem`, `TrajectorySummary`, `IdeaThread`, `Playbook`); guidebooks live in fleet repos via PR; distiller never mutates sources | See finding 1: silent distill failure + mark-mined = permanent skip |
| 2 Concurrency | AT RISK | `knowledge.py:199-209` / `:221-228` — `session.get` + status check + commit, no row lock or version column | Two concurrent approves both pass the `status=="draft"` check; last-writer-wins on scope. Drafts are append-only (fine); block cache is module-global but single-loop (docstring `:39` acknowledges single-host) |
| 3 Swarm | PASS | `knowledge.py:347-350` — block cached per run_id precisely so swarm threads share it; first thread pays the rerank | Explicitly designed for fan-out |
| 7 Parallel | AT RISK | `evidence.py:274-280` verify_suite runs ruff/lint/build/dev-boot serially though independent (600s timeout each); `evidence.py:89` test_cmds serial | `guidebooks.py:191` serial seeding is a *documented* decision (ADO rate limits) — compliant; evidence serialization is unjustified |

### Test pins

| Moat | Verdict | Evidence |
|---|---|---|
| 6 | PARTIAL | Cached-per-run pinned (`test_services_knowledge.py:230-245`, asserts 1 rerank); block shape pinned (`:217-227`); guidebook determinism pinned (`test_services_guidebooks.py:91-97`); playbook order pinned (`test_services_playbooks.py:171-180`). UNPINNED: M-37 version invalidation on approve/reject, LRU bound, no-timestamp guard |
| 4 | PINNED | Search-space scoping exact-set (`:144-155`), own-trajectories-only (`:158-167`), lexical exact order — M-66 hardened (`:171-186`), top-k (`:189-197`), lexical fallback (`:200-209`). `llm_rerank` JSON parsing itself untested |
| 1 | PINNED | PHI checkpoint forced scope (`:33-54`), decisions final (`:132-140`), M-36 orphan card (`:68-77`), H-31 mined markers (`test_services_distiller.py:112-129`) |
| 5 | PARTIAL | MAX_LINES asserted (`test_services_guidebooks.py:97`); truncation caps (600/2000/300) never asserted |
| 2 | UNPINNED | No concurrency test for approve/reject race |
| 7 | UNPINNED | No test pins serial/parallel execution of verify_suite |

### Top findings (ranked)

1. **`distiller.py:78-80` + `:138-140`** — a gateway failure makes `distill()` return `[]`, then `run_nightly` marks every summarized run mined. One transient hiccup permanently skips that night's distillation — silent derived-knowledge loss. Source summaries survive (DB), so recoverable only by manual re-mining.
2. **`knowledge.py:199-209`** — approve/reject check-then-act without `SELECT FOR UPDATE`; concurrent decisions double-commit. Low rate (human-driven) but violates "conflict-free by construction."
3. **`evidence.py:274-280`** — independent gate checks serialized; worst case ~4×600s added latency per goal-mode round. Fan out with `asyncio.gather` (dev-boot can stay last if port-binding argues for it).
4. **`knowledge.py:316-327`** — top-k truncation is the only corpus path into prompts; if no agent-facing "search full corpus" tool exists outside this slice, retrieval is a de facto gate (moat-4 question for the orchestrator-slice agent to confirm).
5. **Missing test pins** — M-37 invalidation (`knowledge.py:49-53`) and LRU eviction have no regression test; a future refactor can silently un-invalidate the block cache.
6. **`knowledge.py:121-137` + `:160-161`** — `mark_mined` junk rows ("(distiller mined — no candidate)") surface in `corpus_for` own-items — UI noise, not corruption.

### Strengths

- M-37 version-stamped, bounded, corpus-invalidated block cache is textbook moat-6 hygiene; block placement after the persona respects static-above/dynamic-below.
- PHI checkpoint enforced at the service layer (`knowledge.py:80-101`), not the API — and pinned by tests.
- Retrieval fails open (lexical fallback, `:318-324`); evidence fails closed (`evidence.py:76-79`); per-repo guidebook failures recorded, never sink the batch (`guidebooks.py:182-184`).
- Generators are pure and deterministic; every LLM seam (`ranker`, `complete`, `scorer`) is injectable — zero sockets in tests.
- Bench-gated distillation (`distiller.py:91-104`) makes memory prove non-regression before a human even sees the card.

I have everything I need. Now compose the report.

Key analysis:

**Moat 6 (prompt caching):**
- Knowledge block: appended to `persona_prompt` at spawn (thread_manager.py:84), cached per run_id in `_block_cache` (knowledge.py:354-368), LRU-bounded (256), version-stamped, invalidated on approve/reject (`_bump_block_cache_version`, lines 212, 228). Content of block: pinned knowledge + episodic recall — no timestamps, no run IDs inside knowledge lines... wait, `render_block` line 342: `f"- (run {e['id'][1:]}) {e['content'][:600]}"` — this DOES bake the run id (trajectory id, e.g. `t9` -> "9") into the block. Is that cache poison? The block is per-run cached and stored in spawn_context for replay. Within a run, the block is identical across threads — good for caching across threads of the same run. But it is per-run dynamic content — the question is where it lands relative to the cache cut. Persona prompt is system-prompt-ish. The philosophy says system prompt is frozen; knowledge pinned per-run is "computable once per run and reusable" — caching per run means all threads in a run share the block. The block is appended at the END of persona_prompt (`persona_prompt += block`), i.e., after the frozen persona — so it sits at the bottom of the system prompt, below static content. That's consistent with "dynamic below the cut" IF the persona prompt itself is the static prefix. The per-run block is stable within a run, so cache hits across threads of the run work.
- Playbooks: `playbooks_prompt_for_mode` — content from DB (seeded SKILL.md), deterministic, no per-run data — fully static/reusable. Inserted between `base` (persona) and `fallback` in `_persona` (development.py:235) — static content, order stable (mode row's playbook_ids order — pinned by test line 171-180). Good.
- render_block headers: "--- Pinned knowledge (flywheel, human-approved) ---" — static header, dynamic content. No timestamps/dates anywhere. Trajectory run ids (episodic section) are stable within the cached block since it's cached per run — not cache poison within run; block is per-run anyway.
- Verdict: PASS (with a note that the block placement relative to the overall prompt prefix ordering can't be fully verified in this slice; within-slice evidence is strong: per-run cache, version invalidation, no timestamps).

**Moat 4 (token intelligence):**
- Is reranking a classifier/gate? `rerank` selects top-k via a cheap model (`llm_rerank`) — this IS a ranking layer that decides what the model sees in the pinned block. Is this a violation? The philosophy: no classifier deciding upstream what the model may see; but knowledge retrieval is ranked relevance with top-k. The module docstring says "~200 rows: no embeddings". The distinction: moat 4 is about the agent being free to read anything; knowledge items are all human-approved, and the agent could presumably read the full corpus via tools (not in this slice — can't verify). The rerank is a relevance ranker with top-k truncation — this is a form of upstream filtering, but only for what gets *pinned*; candidates are human-approved curated content. Hmm — the user's question: "does knowledge selection use a classifier/filter that decides what the model may see (violation), or relevance-ranking that the model can override (fine)?" Top-k truncation means items below rank k never reach the model — that's a gate-ish filter within this slice. But it's ranking, not classification; and corpus items are approved facts. Also full content availability: no evidence in-slice of a knowledge lookup tool for the agent. I'd call it AT RISK: top-k truncation with no in-slice evidence that the agent can fetch non-pinned items on demand. Also episodic: trajectories limited to 50 (line 253) and truncated to 600 chars in render (line 342).
- Actually, wait — is truncation of content a violation? `e['content'][:600]` truncates episodic summaries; `t.summary[:400]` for trigger. Knowledge content is NOT truncated (line 338 uses full content). Trajectory content truncated to 600 chars in the block — smallest relevant chunk discipline (moat 5). The full summary presumably available elsewhere.
- Verdict moat 4: AT RISK — rerank is a ranking layer (good taste), but `top_k` truncation with no demonstrated on-demand full-corpus path in-slice edges toward a gate. The reranker itself "omits irrelevant ids" (system prompt line 288-289) — a filter. But since corpus is curated/human-approved, arguably ranking of golden content. I'll mark AT RISK with note.

**Moat 5 (cost):**
- PASS: top-k pinning (`knowledge_top_k`), trigger capped at 300 chars in catalog (line 283), task text capped 2000 (line 290), episodic content 600 chars (line 342), trajectories limited to 50 (line 253). `plan_task_for` bounds: body[:2000], comments[:20], body[:400] (ideas.py:244-254). Guidebooks capped at 200 lines by construction (guidebooks.py:116-117). Evidence truncates stdout/stderr to last 2000 chars (evidence.py:73-74). Distiller corpus: summaries[:20], summary[:1200], lessons[:8] (distiller.py:65-69). Distill reply capped at 5 candidates (line 77). Strong smallest-chunk discipline everywhere.

**Moat 1 (durability):**
- Knowledge in DB (KnowledgeItem rows) — survives shredding. PASS. TrajectorySummary in DB. Ideas threads/comments in DB. Playbooks seeded from repo files (`playbooks_dir`) into DB — the source of truth is files in the repo (durable), DB is the runtime store. Guidebooks: AGENTS.md lives in the fleet repos via PR (durable), generated deterministically from fleet-config.
- Distiller: distills FROM TrajectorySummary rows; never mutates/deletes source — writes new KnowledgeItem drafts + markers. `mark_mined` creates status="rejected" rows. Distillation cannot lose/corrupt source — read-only on summaries. One wrinkle: mining marker pollutes the knowledge table with junk rows ("(distiller mined — no candidate)") — they have status="rejected" so excluded from search space (filter status=="approved") and from pending() (status=="draft"). But `corpus_for` includes ALL own items including these markers — minor noise, not corruption.
- distiller `distill` fails silently returning [] (line 78-80) — "the night shift fails silently" — moat 1 says never unrecoverable; failure just means no candidates that night, but wait: if distill returns [] due to a gateway error, `run_nightly` then marks all summaries as mined (lines 138-140) — so a transient gateway failure PERMANENTLY loses that night's distillation opportunity! Candidates=0 but mined=N. The summaries are never re-mined. Is that data loss? The source summaries survive (DB), but the distillation is skipped forever. That's a recoverability concern: silent failure + marking mined = lost knowledge candidates. H-31 fixed re-mining but created this: a gateway failure during distill → all runs marked mined → those runs never distilled. Finding! distiller.py:78-80 + 138-140. This is a moat-1 adjacent issue (silent loss of derived value, though source survives). I'd rank it high.
- Also: `unmined_since` order by id limit 100 — fine.

**Moat 2 (concurrency):**
- Two concurrent runs writing knowledge: `draft()` does session.add + flush + commit — plain inserts, no conflict (append-only). approve/reject: read-modify-write on a row with status check — two concurrent approves: both could pass `status != "draft"` check before commit? `session.get` + check + commit — no optimistic locking/version column. Race: double-approve → both commit, second overwrites scope. Decisions are supposed to be final (test at line 132-140) but concurrency race isn't handled — no SELECT FOR UPDATE. In single-host era with per-session sessions, last-writer-wins. Minor: the approval card resolution loop breaks after first match (line 188) — fine.
- Block cache: module-level OrderedDict mutated from async code — single event loop, fine on single host (docstring acknowledges "Single-host local era").
- `_block_cache_version` global increment — not thread-safe but single-loop asyncio OK.
- Verdict: AT RISK (approve/reject check-then-act race, acknowledged single-host) — but knowledge writes are rare/human-driven; append-only drafts conflict-free. I'll say PASS-with-note or AT RISK. Given moat 2 is "by construction", the check-then-act without locking is AT RISK.

**Moat 3 (swarm):** N-A mostly; per-run block cache explicitly supports swarm threads sharing (line 349-350) — PASS-ish, aligned. I'll say PASS (block shared across swarm threads, first thread pays).

**Moat 7 (parallel):** 
- evidence.run_test_commands runs commands SEQUENTIALLY (line 89: list comprehension with await in loop — actually `[await run_tests(...) for c in commands]` — sequential awaits). verify_suite also sequential: tests, then ruff, then lint, then build, then boot (lines 274-280). Could fan out with asyncio.gather — boot smoke must be after? Actually all checks are independent except maybe boot. This is serialization that could fan out — moat 7 says serialization is a decision, not a default. Also `seed_guidebooks` loops repos sequentially with comment "the batch is small (10) and ADO rate limits are real" (line 191) — deliberate, justified serialization. That's OK (a decision, documented).
- run_test_commands: commands must run in order? Docstring says "in order" — plausibly deliberate (e.g., lint before test), but could be justified. verify_suite sequential checks — ruff/lint/build/dev-boot are independent and could gather; each has a 600s timeout, so serial execution could add ~30+ min worst case. This is a legit AT RISK for moat 7.
- Verdict: AT RISK.

**Tests:**

Moat 6 pins:
- test_prompt_block_cached_per_run (test_services_knowledge.py:230-245) — PINNED: caching, single rerank per run.
- Version invalidation (M-37): NOT tested — no test asserts that approve/reject invalidates a cached block. PARTIAL. LRU bound (256) not tested.
- Deterministic render: test_render_block_sections (217-227) pins block shape. Guidebook determinism pinned (test_render_is_deterministic). Playbook order pinned (test_playbooks_prompt_preserves_mode_row_order).
- No test pins that block contains no per-run timestamps/IDs (episodic run ids are in the block — no test asserts absence of timestamps). PARTIAL overall.

Moat 4 pins:
- Search space scoping pinned (test_search_space_scopes, includes own trajectories only). Lexical rank exact order pinned (M-66). Top-k pinned (test_rerank_injected_ranker_and_top_k). Fallback pinned. Rerank "omit irrelevant" behavior not tested at llm_rerank level (no test for llm_rerank parsing — actually llm_rerank is untested directly; the id-filtering `valid` check untested). PARTIAL/PINNED — I'll say PINNED for selection behavior, but llm_rerank response parsing UNPINNED. Overall PARTIAL+.

Moat 1 pins: durability implicit (DB-backed tests). Distiller mining bookkeeping pinned (H-31 tests: drops-regressing still marks mined; unmined excludes mined; window). The distill-failure-marks-mined gap: test_distill_never_crashes_on_bad_reply pins [] return, but NO test pins that run_nightly with a failing complete does/doesn't mark runs mined — the silent-skip behavior is unpinned. PINNED for lifecycle, PARTIAL for failure modes.

Moat 5 pins: truncation behaviors partially pinned (episodic 600? not asserted; test_render_block uses short content). Guidebook MAX_LINES pinned (test_render_is_deterministic asserts ≤MAX). Evidence truncation to 2000 — not asserted in tests. PARTIAL.

Moat 2 pins: decisions final pinned (sequential), concurrent approve race untested — UNPINNED.

Moat 7 pins: N-A/UNPINNED — no test asserts parallel vs serial for verify_suite.

Top findings ranked:
1. distiller.py:78-80 + 138-140 — silent distill failure marks runs mined; that night's knowledge permanently skipped (recoverability/moat-1 gap). Gateway hiccup = lost distillation.
2. knowledge.py:199-209 / 221-228 — approve/reject check-then-act without row lock; two concurrent decisions can both commit (last-writer-wins on scope) — moat 2 race, though human-driven low rate.
3. evidence.py:274-280 — verify_suite serializes independent checks (ruff/lint/build/boot); each 600s timeout → worst-case serial latency; moat 7 serialization not justified (unlike guidebooks.py:191 which documents ADO rate limits).
4. knowledge.py:316/327 — top-k truncation with reranker instructed to "omit irrelevant ids" (line 289): no in-slice evidence the agent can reach non-pinned corpus items on demand — edges toward gate; mitigated by human-approved corpus. Moat 4 AT RISK.
5. knowledge.py:342 — episodic entries embed `(run {id})` in the rendered block: fine within per-run cache, but no test pins absence of per-run timestamps/IDs in otherwise-static content (cache-poison guard unpinned); also M-37 version-invalidation has no test.
6. Minor: mark_mined junk rows ("(distiller mined — no candidate)") surface in corpus_for own items (knowledge.py:160-161 includes all own items regardless of status) — noise in UI.
7. ideas.py `_thread_transcript` is UNBOUNDED — Counsel reads ENTIRE thread (by design, "reads the ENTIRE thread before speaking") — deliberate moat-4 choice (free reader) vs moat 5: no cap on transcript length. By design; note it.
8. guidebooks.py:157 — idempotency check reads AGENTS.md before checking repo_dir.exists() — fine (exists() check on file). Note: `agents_path.exists()` then `repo_dir.exists()` raise order: if repo dir missing, agents_path.exists() is False → raises GuidebookError — caught per-repo → recorded. OK.

Also moat 6 note: `persona_prompt += block` (thread_manager.py:84) appends dynamic content AFTER persona — correct ordering (static above, dynamic below), and block cached per run + stored in spawn_context for replay (lines 80-82, 109). Playbooks injected between base persona and fallback (development.py:235) — static DB content, stable order — above the cut. Good.

Strengths:
- M-37 version-stamped LRU block cache with invalidation on corpus change — exactly moat-6 hygiene.
- PHI checkpoint enforced at service layer, pinned by tests.
- Fail-closed evidence; fail-open retrieval (lexical fallback) — retrieval never fails a run (moat 1 aligned).
- Cost caps everywhere (top-k, 300/600/2000-char truncations, 50-trajectory cap, MAX_LINES=200).
- Deterministic pure generators (guidebooks, playbooks) — same input, same bytes; idempotent seeding.
- Distiller bench-gate before human sees candidate; sources never mutated.

Word budget: under 600. Need to be dense. Let me write it.


agent9:

All slice files read. Let me verify a few cross-cutting facts: the `Repo` model's uniqueness constraints (registration race), and where per-thread clone stamping lives (to confirm scope boundaries).

I have now read all files in my slice completely. Let me also check a couple of things to be thorough:

1. For moat 2 (per-repo write lock, golden repo + hardlinked clones): repos.py handles golden repo onboarding. The hardlinked per-thread clones would be in a different slice (workspace/stamping), but I should check whether repos.py references anything about clone stamping. The docstring says "cloned into golden, checked out at origin/<integrationBranch>, adopted by the fetcher — NEVER cloned into a task working directory." That's consistent with the philosophy. repos.py itself doesn't do per-thread clones — that's elsewhere (stamp_clone mentioned in philosophy §7). Let me quickly grep for "stamp_clone" or "hardlink" to see where that lives, to confirm it's not supposed to be in my slice.

2. For campaigns.py — fan-out: `launch` loops `for repo in repos: await run_manager.create_run(...)` — sequential await in a loop (moat 7: parallel by default — this serializes run creation!). Also no explicit cap of 100 on repo count — bounded only by the number of ready repos. The capacity semaphore is inside run_manager.create_run presumably (different slice). The campaign fan-out is bounded by fleet size, not by the 100 cap explicitly. Worth noting.

3. For team.py / identity.py — multi-tenant isolation: identity.resolve_descriptor is a lookup, not a leak. intents.load_run_for_user hard-scopes runs to created_by. stats.py cost_dashboard is team-wide metadata-only rollup (by design — "METADATA-ONLY team-wide rollups"). proposals are team-wide readable by design ("Team-wide readable: proposals cite code, not sessions"). bench list_cases is team-wide (report is team-visible per test). So the isolation story: sessions/runs are private (load_run_for_user), while proposals/bench/stats/deliveries are deliberately team-visible metadata. That's a design choice, documented.

4. Moat 1 concerns:
   - repos.py onboard: failure path sets ERROR status — recoverable. H-28 fix: relay publish outside try. Good.
   - repos.py archive_repo: shreds golden dir with shutil.rmtree(ignore_errors=True) — if threads still have hardlinked clones, shredding golden doesn't corrupt them (hardlinks). Actually wait — if per-thread clones hardlink packs from golden, then `rm -rf` on golden just decrements link counts; thread clones keep their own links. That's the point of hardlinks. OK.
   - One issue: `onboard` — if dest exists but is a partial/failed clone (e.g., previous clone crashed midway leaving a corrupt dir), `if not dest.exists()` skips re-cloning and proceeds to config/checkout on a possibly-corrupt dir. Partial failure state: clone fails → OnboardingError → status ERROR, but the partial dest directory remains; a retry would skip the clone because dest.exists() is True, then checkout -B might fail or succeed on a corrupt repo. That's a moat 1 partial-failure concern. Let me note it: repos.py:137 `if not dest.exists():` — no cleanup of partial clone on failure (clone.returncode != 0 → raise, but dest dir may exist partially). Actually `git clone` cleans up its own partial dest on failure in most cases (git removes the destination dir on failure). Yes, git clone removes the target directory if the clone fails. So that's mostly fine. But if the process is killed mid-clone (SIGKILL), the partial dir survives and retry skips cloning. Edge case worth flagging as AT RISK-ish, minor.
   - proposals.py accept: atomic claim (H-32), ceiling releases claim, then create_run — but if create_run raises AFTER the ceiling check passes and claim is held, the proposal is stranded in 'accepting' forever! Look: claim → ceiling check (releases claim on failure) → `run = await run_manager.create_run(...)` — if create_run raises, there's no rollback of the 'accepting' status. The H-32 comment says the ceiling path releases the claim, but the create_run exception path does NOT release it. That's a partial-failure state: proposal stuck in 'accepting', can't be retried (accept requires status='proposed'), and no run created. That's a moat 1 finding — proposals.py:152-154. Let me double check... yes:
     ```python
     run = await run_manager.create_run(...)  # if this raises, status stays 'accepting'
     ```
     No try/except around it. The exception propagates to the API layer (500), and the proposal is stranded in 'accepting' — neither proposed (retryable) nor accepted (has run). VIOLATION-ish / AT RISK for moat 1. This is a real finding.
   - campaigns.py launch: H-34 rollback deletes Delivery on fanout failure, but already-created runs "self-reconcile via reconcile_on_boot". OK — documented.
   - byo_pat.py: encrypted at rest (SHA-256 CTR keystream + HMAC — homegrown crypto, but documented as local-era stdlib-only with Key Vault planned; encrypt-then-MAC, random nonce per encrypt, constant-time compare). Write-only status. Fail-closed identity proof. pat_for_push treats expired as absent. Good. One nit: homegrown CTR is not AES, but it's documented and HMAC'd; flag as noted-but-accepted.
   - plans.py: approve/reject — single transaction, status guard, transition. If run is None (plan exists without run), approve still marks plan approved but no stage transition — fine.
   - intents.py: classify_text is a keyword router against the current legal move set — it classifies USER TEXT into UI intents, not a gate on what the MODEL may read. Moat 4 is about read classifiers upstream of the model. This is interaction routing — fine, not a violation. The docstring even says "tiny legal move set = near-perfect interpretation". It gates user ACTIONS (state-changing intents need confirmation) — that's HITL safety, not content censorship. PASS.
   - mentions.py: pure routing/extraction — no gating of model reads. PASS.
   - stats.py: cost dashboard — moat 5 visibility aggregation, team-wide metadata-only. PASS. Note: by_user bucket uses display names — team-visible cost per user. Deliberate ("cost visible").
   - bench.py: scoring pure function, verdict computed from stored outcomes, never agent self-report. start_eval creates run via run_manager with system_user_id. report cost math fixed (L-16). Good.

5. Moat 2 deep-dive for repos.py:
   - Golden repo clone: `git clone` into golden_dir/name, checkout integration branch. This is the golden repo. Per-thread clones (hardlinked) are NOT in this file — they live in workspace/stamping (another slice). repos.py's tier invariant docstring explicitly says "NEVER cloned into a task working directory" — consistent.
   - Write lock: not in this slice (capacity semaphore is elsewhere). 
   - Concurrency within repos.py: register_repo dedupe is check-then-insert without a uniqueness constraint visible here — two concurrent register_repo calls with the same URL could both pass the `existing` check and both insert → duplicate Repo rows for the same URL. Is there a DB unique constraint? Need to check the Repo model. Let me grep for the Repo model to see if name/remote_url have unique constraints. That's a moat 2-adjacent race (fleet registration). Let me check.
   - onboard: two concurrent onboard(repo_id) calls for the same repo — both validate, both clone? `if not dest.exists()` — race: both see not exists, both `git clone` to same dest → one fails with "destination path already exists". Sets ERROR status. Recoverable but noisy. Is there a guard against concurrent onboard? Not in this file. The API layer might guard (only allow onboard when status == REGISTERED?). Not visible in slice. Worth flagging as AT RISK with the caveat.
   - archive_repo vs onboard race: archive shreds golden dir while onboard clones into it. No locking. But these are admin ops, low frequency. Flag minor.

Let me check the Repo model for unique constraints, and check where stamp_clone/hardlink lives to confirm it's out of slice. Also check whether there's a per-repo write lock reference in this slice (there isn't, but let me confirm the semaphore lives elsewhere).

Also for campaigns: `launch` fan-out is sequential `await` in a for-loop — moat 7 says parallel by default. Each create_run may do capacity acquire etc. Sequential creation of N runs is a serialization that could fan out (asyncio.gather). But there's a counter-argument: capacity reservations need ordering? No — reservations close the check-then-act race precisely so concurrent spawns are safe. So sequential fan-out in campaigns is a moat 7 soft violation (AT RISK), though bounded by fleet size. Also no 100-cap in campaigns itself — the cap would come from capacity (create_run raising when capacity exhausted → H-34 rollback deletes the whole delivery — so a campaign larger than capacity fails entirely rather than queueing? Deterministic queueing (moat 2) says requests beyond capacity queue rather than corrupt. If create_run queues rather than raises, fine — depends on run_manager, out of slice. Note it.

Let me grep for the Repo model and stamp_clone.


agent10:

All migrations read — all reversible, dialect-aware. Now reading the core test files.

Critical confirmation: `semaphores.py` Capacity is **purely in-memory** (`asyncio.Lock`, `self._reserved`, `self._reserved_writable`) with a **module-level singleton** `capacity = Capacity()` (line 71). The DB is only used for *counting* active threads. This is exactly the multi-worker violation scenario: two backend processes (or a backend restart) lose reservations; the per-repo write lock is enforced by Python set membership + a SELECT count — a check-then-act race across processes.

However, note `base.py` line 4: "Single-writer rule: ALL writes flow through this one backend process — workers speak Redis/contracts only." So the *design* acknowledges single-process; but moat 2 says "enforced at the concurrency layer (DB constraints/locks), not by engineers being careful" and the philosophy explicitly says reservations "needs DB-level support: unique constraints, transactional insert" per the user's audit brief. So schema verdict for Moat 2/3: AT RISK / VIOLATION — no DB-level unique constraint on (repo_scope) for active writable threads, no reservation table, semaphore state is process-local memory. The `release == commit` alias at semaphores.py:67-68 is good for in-process, but a crash between try_acquire and insert leaks... actually no, in-memory state disappears on crash, so no leak on restart; but a restart *mid-run* loses nothing since DB rows persist and are counted. The race is: two backend replicas (HA deploy) → two Capacity singletons → both pass the write-lock check. Single-writer rule mitigates only if deployment truly is single-replica.

Also note: `try_acquire` holds `self._lock` (asyncio.Lock) across `await self.active_thread_count()` which does **sync DB I/O in an async function** — blocking the event loop. Minor.

Now, missing from models: retention columns on Run (no `retention_state`, no `expires_at`), Event has no TTL column — purge computed from ts + config. The two-step decay (30d replay-only / 12mo deleted) has `session_retention_days` and `events_ttl_months` config and `legal_hold` on Run — but no per-run timestamps recording when volume was purged or when events expire. The maintenance job presumably computes. No cascade deletes on Run relationships (events/threads) — good, deletion must be deliberate; but also note there's no ondelete behavior and no run deletion path visible in models. KnowledgeItem.source_run_id is a plain string with no FK — survives run deletion (good for survivors).

Issue: `Event.thread_id` has no FK to threads (just String) — acceptable since events outlive threads, actually arguably deliberate.

Issue: `Run.events` relationship default cascade — if someone calls `session.delete(run)`, SQLAlchemy will try to nullify events.run_id? No — default cascade is "save-update, merge"; on delete of parent with loaded children, without "delete" cascade, the children's FK is set to NULL → IntegrityError (run_id nullable=False). So deletion fails loudly — good for moat 1. But plans: `cascade="all, delete-orphan"` on Plan.steps — fine.

gateway_key stored plaintext in threads table (thread.py:35) — LiteLLM virtual key stored raw in DB. It's a scoped virtual key, not the master key, but still a credential at rest in plaintext while BYO PATs get encryption (byo_pat_encrypted). Worth flagging as inconsistency.

Now `Run.stage` default "queued", Thread.status default "queued" — deterministic queueing states exist. Queue ordering: is there a queue position / priority column? No `queue_position`, `priority`, or FIFO ordering column on Run/Thread. Ordering would rely on created_at — there's no index on (status, created_at) for dequeue ordering. Run has ix_runs_stage index though.

Nullable fields allowing half-created rows: Run.repo nullable (ask-mode runs may have no repo — deliberate), Thread.session_id nullable until SDK session starts (deliberate), spawn_context nullable — kill_replace respawns from spawn_context; nullable means old threads can't respawn (added later via migration b7e4c2f19a03 — nullable for backfill, fine). Approval.run_id nullable — deliberate (M-36). user.pin_hash nullable until forced PIN choice — deliberate. `Pin_hash` nullable is fine.

Server-side defaults: models use Python-side `default=` (client-side), not `server_default` — rows inserted via ORM get defaults, but raw SQL inserts don't. Not a violation, but "server-side defaults/onupdate" — mostly client-side defaults, and NO `onupdate` anywhere (e.g., last_active_at must be manually set; heartbeat_at manual). Only one server_default in migrations: trigger_event_verdicts.created_at (l7a8b9c0d1e2:35). This is a partial gap — nothing that breaks correctness though.

Migrations: all reversible (downgrade implemented everywhere). Destructive? The lane→thread rename preserves data. approvals.run_id nullable → downgrade makes it non-nullable, which would fail with NULL rows present, but that's inherent. No DROP COLUMN with data except spawn_context downgrade and modes.permissions downgrade (drops data on downgrade only — normal). Initial schema downgrade drops everything (normal). So migrations: PASS, reversible, SQLite+Postgres dialect-aware (batch mode, dialect checks, guarded index creation for dual lineages).

Config: secrets — jwt_secret no default, fail-fast validator (C-13); byo_pat_encryption_key no default, fail-fast (H-44); dev_insecure_defaults opt-in. bootstrap admin empty defaults. ado_webhook_secret empty = fail-closed. Differences dev/prod: `redis_url: "memory://0"` default silently falls back to fakeredis — a prod deploy that forgets COLLEGIUM_REDIS_URL runs on a **fake in-process Redis** silently (bus silently broken across processes). Comment says "Every real deployment sets COLLEGIUM_REDIS_URL explicitly" but no validator enforces it — silent dev/prod divergence. That's a moat-1-style risk (silent degradation), worth flagging as AT RISK for config. Similarly db_url defaults to SQLite; events docstring mentions 30GB guardrail pulls Postgres forward.

security.py: bcrypt PIN hashing, JWT HS256, token_version revocation, lockout with non-resetting counter (M-55). What's encrypted: BYO PAT (byo_pat_encrypted, key from config) — but encryption code itself isn't in security.py; it's elsewhere. PINs bcrypt-hashed. gateway_key plaintext (noted). SetupCode.code_hash hashed.

fleet.py: loads fleet-config loader.py dynamically (importlib from a config dir path — exec of a file from configured path; fine, it's own repo config), lru_cached, returns (repos, fleet_graph) Layer 0 in-memory graph.

redis_factory: single factory, scheme-based, shared FakeServer for memory mode. Real Redis: `redis.from_url` default — **no reconnect/health-check config** (redis-py does auto-reconnect with retry by default though: `retry_on_timeout` defaults vary; redis-py 5 auto-retries ConnectionError once? Actually redis-py auto-reconnects on next command). No socket keepalive, no health check interval — for a single-writer bus with BLPOP consumers, BLPOP blocking forever on a dropped connection without `socket_timeout`/`health_check_interval` is a real resilience gap. Worth flagging: no `health_check_interval`, no retry config.

Single shared client vs per-purpose: `make_redis()` creates a NEW client per call (not a shared singleton) — each caller gets its own connection pool. That's acceptable (per-purpose clients), but no pooling centralization. Fine.

Now tests. Read all 7 test files. Also test_smoke.



-----


10 backend & worker combine parallel agents diagnosis

agent1:

# Auditor A1 — StepEvent schema & durable event ingest (worker→Redis Stream→backend→DB)

All claims below were verified by reading both sides of the seam this session. No inference from names/docstrings.

---

## FINDINGS

### [SEVERITY: BLOCKER] [STATUS: CONFIRMED] Engine path never emits `session_id` → resume silently broken

- **Worker evidence:** `worker/worker/engine/events.py:163-172` — `EventEmitter.turn_boundary()` builds detail `{num_turns, duration_ms, is_error, usage}` — **no `session_id` key**. Grep for `session_id` across all of `worker/worker/engine/` returns **zero matches**. The default runtime is this engine: `worker/Dockerfile:36` `ENV ... ENGINE=custom`, dispatched in `worker/worker/main.py:264-277` (`engine != "sdk"` → `worker.engine.runner.main`), and `runner.py:339-344` publishes that boundary event.
- **Backend evidence:** `backend/app/events/bus.py:150-156` — the *only* writer of `thread.session_id` gates on `event.kind == STATUS and event.title == "turn complete" and event.detail.get("session_id")`. That column feeds resume: `backend/app/orchestrator/thread_manager.py:94-108` (`inherited_session_id = prior.session_id`) and `backend/app/sandbox/manager.py:154-155` (`env["RESUME_SESSION_ID"] = thread.session_id`).
- **What breaks at the seam:** The `session_id`-carrying `turn complete` event exists **only** in the legacy CAS-SDK path (`worker/worker/normalize.py:154-167`, `detail["session_id"] = msg.session_id`) — used only when `ENGINE=sdk`. Under the default `ENGINE=custom` engine, `detail.get("session_id")` is always `None`, so `bus.py:154` is falsy, `thread.session_id` stays `NULL` forever, and `RESUME_SESSION_ID` is never set. The backend's own comment (`bus.py:145-149`) says "Nothing else writes it … the thread is never resumable." Resume / kill-replace / mode-switch all silently start a stranger. The bus comment even misattributes the emission to `normalize.py` while the live engine never emits it.
- **Minimal fix direction:** Have the engine surface the LangGraph/SDK session identifier and include `session_id` in `EventEmitter.turn_boundary()`'s detail (or whatever turn-complete event the engine emits), matching the `detail["session_id"]` key the bus reads. Add a contract test: engine `turn complete` → `thread.session_id` populated.

---

### [SEVERITY: HIGH] [STATUS: CONFIRMED] No unique constraint on `(run_id, thread_id, seq)` → redelivered XADD double-inserts Event rows

- **Worker evidence:** `worker/worker/forwarder.py:33-37` XADDs `{thread_id, seq, payload}`; the consumer-group ack is the only dedupe, and ack happens *after* commit.
- **Backend evidence:** `backend/app/db/models/event.py:26-32` — `__table_args__` defines three *non-unique* indexes (`ix_events_thread_seq`, `ix_events_run_ts`, `ix_events_run_thread_seq`). **No `UniqueConstraint`/`unique=True`** anywhere. Ingest `_process` (`bus.py:137-141`) does a blind `session.add(Event(...))` then `session.commit()` — no `SELECT ... WHERE seq=` idempotency check, no upsert.
- **What breaks at the seam:** Crash window between `session.commit()` (`bus.py:161`) and `xack` (`bus.py:177`) → the message stays pending in the consumer group → redelivery on restart → `_process` re-runs → a **second Event row with the same (run_id, thread_id, seq)**. Nothing in the DB or the insert path rejects it. The `next_seq` guard (`bus.py:143-144`, `if event.seq >= thread.next_seq`) only gates the *counter bump*, not the row insert — the duplicate row is inserted regardless. Replay/audit (`ORDER BY thread_id, seq`) then sees the step twice.
- **Minimal fix direction:** Add `sa.UniqueConstraint("run_id", "thread_id", "seq", name="uq_events_run_thread_seq")` to `Event.__table_args__` (plus migration), and make `_process` treat `IntegrityError` on that key as already-ingested (ack without re-inserting). This is the ack-after-commit exactly-once backstop.

---

### [SEVERITY: MEDIUM] [STATUS: RISK] Worker `seq` resets to 0 on every activation; diverges from backend `next_seq`

- **Worker evidence:** `worker/worker/engine/events.py:33` — `self._seq = 0` in `EventEmitter.__init__`. The emitter is constructed fresh per container activation in `worker/worker/engine/runner.py:101`. On resume, only `RESUME_CONTEXT_ID` is read (`runner.py:87,99`); **`next_seq` is never read back** (grep `next_seq` in `worker/` → zero matches). The legacy `Normalizer` has the same reset (`normalize.py:83`).
- **Backend evidence:** `bus.py:142-144` advances `thread.next_seq = event.seq + 1` only when `event.seq >= thread.next_seq`. `next_seq` is the backend's monotonic cursor (`thread.py:36`), also written by `pin_finding` (`run_manager.py:345-349`) and `_stamp` (`development.py:194-209`).
- **What breaks at the seam:** A resumed/kill-replaced thread starts emitting at `seq=0` again while the DB already holds `seq=0..N` from the prior activation. The `next_seq` guard correctly refuses to rewind the counter (so `pin_finding`/`_stamp` don't collide), but the **Event rows for the new activation collide with the old seqs** — and once Finding 2's unique constraint lands, these redelivered-seq inserts would start raising `IntegrityError` on every genuinely-new post-resume event. The contract claims "seq is monotonic per thread" (`contracts/events.py:64`, `identifiers.py:54`) but the worker cannot honor that across restarts. Replay ordering by seq interleaves two activations.
- **Minimal fix direction:** Worker must seed its emitter from the thread's durable cursor on boot (e.g. read `next_seq` via a handshake, or include a per-activation epoch/generation in the event so seq is scoped `(epoch, seq)`). At minimum, document that seq is per-*activation* and add an activation/generation field so the DB unique key and replay ordering don't conflate restarts. This is a contract-level gap, not a one-line fix.

---

### [SEVERITY: MEDIUM] [STATUS: CONFIRMED] Dead-letter stream is write-only — a dead-lettered event is a permanent silent hole in replay

- **Worker evidence:** n/a (producer never dead-letters; only the consumer does).
- **Backend evidence:** `bus.py:36` `DEADLETTER_SUFFIX = ":deadletter"`. Written in two places — poison-pill validation failure (`bus.py:127-131`) and generic consumer error (`bus.py:110-115`) — both followed by `xack`. **Grep finds no reader of any `:deadletter` stream anywhere in the repo** (only the two writers and a test asserting the stream name, `test_events_bus.py:139`). The module docstring (`bus.py:8-10`) promises a "watchdog card" — no such card is created in either dead-letter path; only `log.error`.
- **What breaks at the seam:** A poison-pill or transient-DB-error event is acked (removed from the pending set) and moved to `events:{run}:deadletter`, which **nothing ever reads, alerts on, or re-injects**. The events table is the "PHI-grade system of record" and replay source — so a dead-lettered step is a permanent, silent gap in the replayable record with no surface. This directly contradicts the philosophy §1 "no silent loss, ever" and the bus's own "never acked-and-dropped" claim (it is acked-and-*sidetracked-to-nowhere*).
- **Minimal fix direction:** Either (a) implement the documented watchdog-card surfacing + a replayer that drains `:deadletter` back through `_process` after the underlying error clears, or (b) stop claiming the record is durable and add an operator-visible alarm. At minimum, emit the watchdog card the docstring already promises.

---

### [SEVERITY: LOW] [STATUS: RISK] Crash between xack and `relay.publish_step` → WS subscriber misses a committed event

- **Worker evidence:** n/a.
- **Backend evidence:** `bus.py:177-178` — `await self.redis.xack(...)` then `await self.relay.publish_step(run_id, event)`. Ack (durable) precedes relay (transient).
- **What breaks at the seam:** A crash in the microseconds between ack and publish drops the WS fan-out for an event that *is* durably committed. Live WS viewers see a gap. This is the **correct** priority order (durable before transient — matches the design's "events table is the system of record; deltas/relay are lossy-tolerable"), and the gap is recoverable on the next transcript/replay read (`transcript.read` / `ix_events_run_thread_seq`), so it is Low, not a correctness bug. Flagging only because the prompt asked to trace it; no fix needed beyond confirming replay is the recovery path (it is).

---

## (a) VERIFIED-OK list

- **Stream key agreement** — worker XADDs `events:{run_id}` (`forwarder.py:23,33`); consumer normalizes bare `run_id` → `events:{run_id}` in `register_run` (`bus.py:51-57`) and strips it back via `removeprefix` (`bus.py:98`). All three registration callers pass bare ids (`run_manager.py:109,140`; `thread_manager.py:166`). Converges correctly. **OK.**
- **XADD field names** — worker writes `thread_id`, `seq`, `payload` (`forwarder.py:33-37`); consumer reads only `fields["payload"]` (`bus.py:124`) and `fields.get("payload", "")` (`bus.py:113,129`). `thread_id`/`seq` stream fields are ignored by the consumer (it uses the parsed payload's own values) — so no field-name mismatch is possible. **OK.**
- **Schema JSON round-trip** — `model_dump(mode="json")` (`forwarder.py:36`) vs `StepEvent.model_validate(json.loads(...))` (`bus.py:124`). `ts: datetime` → ISO string → re-validated; `kind: StepKind` → `.value` str → enum; `seq: int`; `context_id/task_id/sdk_message_uuid: str|None`; `detail: dict`. All survive. **OK.**
- **Contracts version pin** — both `worker/pyproject.toml:20` and `backend/pyproject.toml:23` depend on bare `collegium-contracts`, resolved via the **same workspace source** (`pyproject.toml:16` `collegium-contracts = { workspace = true }`). Single shared package, so both sides pin the identical schema. **OK** (though see Finding 3 — the shared schema can't express per-activation seq).
- **Schema version guard** — `SCHEMA_VERSION = 1` (`contracts/events.py:34`), stored on every event; backend persists rows without reading `schema_version` but there is only v1, so no divergence yet. **OK** (fragile-looking but currently sound).

## (b) CORRECTED prior claims

- **`backend_diagnosis.md` (§1 strengths, line 64 & "What the backend already does well", line 130) claims: "event bus acks strictly after DB commit with poison-pill dead-lettering (H-43)."** — **Overstated / partially false.** Ack-after-commit is real (`bus.py:161`→`177`), but the dead-letter is a **write-only hole**: nothing reads `:deadletter` and the promised "watchdog card" (`bus.py:8-10`) is never created in either dead-letter path. "Poison-pill dead-lettering" as written implies recoverability that does not exist. (Finding 4.)
- **`backend_diagnosis.md` line 22 (§1 worst evidence) and the resume narrative (`run_manager.py:116-124`, `kill_replace_thread` line 366-369)** assert resume "now actually [works], because resume_from_thread_id mounts the old session volume and inherits the old session_id." — **False under the default engine.** `thread.session_id` is only populated from the legacy SDK path's `turn complete` event; the default `ENGINE=custom` engine never emits `session_id` (Finding 1). The session *volume* mount is real (`manager.py:190-192`), but `RESUME_SESSION_ID` (`manager.py:154-155`) is `NULL`-gated and stays unset. Resume inherits an empty session id.

## (c) Slice health summary

The durable-ingest *plumbing* is largely sound: stream keys, XADD field names, the JSON schema round-trip, and the shared contracts pin all verified clean on both sides. The seam's two real defects are at the edges — the **default engine never emits the `session_id` the backend's entire resume machinery depends on (BLOCKER)**, and there is **no unique constraint on `(run_id, thread_id, seq)`, so the ack-after-commit crash window double-inserts Event rows (HIGH)**. Compounding both: the worker resets `seq` to 0 each activation while the contract demands per-thread monotonicity, and the dead-letter stream is a write-only hole that silently violates the "no silent loss" moat the events table is supposed to guarantee.


agent2:

All verification complete. Compiling the final report.

Confirmed: `engine_runtime` defaults to `"custom"` (config.py:39). So on the default deployment the session_id capture at bus.py:150-156 never fires.

All my verification is done. Let me compile the findings.

**FINDINGS:**

1. **[HIGH] [CONFIRMED] `session_id` capture in bus ingest is dead on the default engine — kill_replace/mode-switch resume silently broken**
   - Worker: `worker/worker/engine/events.py:163-172` (turn_boundary has no session_id); zero `session_id` refs in `worker/worker/engine/` (grep, no matches); only the legacy SDK path emits it: `worker/worker/normalize.py:161`.
   - Backend: `backend/app/events/bus.py:150-156` (capture depends on `detail.session_id` in a "turn complete" event); `backend/app/orchestrator/thread_manager.py:94-108` (inherits prior.session_id → None); `backend/app/sandbox/manager.py:154-155` (RESUME_SESSION_ID only if session_id set); `backend/app/core/config.py:39` (engine_runtime default "custom"); no backend code sets RESUME_CONTEXT_ID (grep no hits) while the custom runner reads only RESUME_CONTEXT_ID (`worker/worker/engine/runner.py:87`).
   - Break: On ENGINE=custom (the default), thread.session_id is never populated, the replacement thread gets a new uuid (thread_manager.py:105) with no resume env, so context_id = new thread_id (runner.py:99) → the checkpointer lookup misses → "resume" starts a stranger. bus.py:145-149's comment claims replay banner + kill_replace resume depend on this field; on the default path the field is never written. (The sandbox-side half overlaps backend slice 4; my seam half: the event schema the backend depends on is not emitted by the default worker.)
   - Fix direction: pick one resume key and wire both sides — either custom engine emits a resumable checkpoint key (context_id) in the turn-boundary event and backend stores/passes RESUME_CONTEXT_ID, or thread_manager reuses the prior thread's id/checkpoint key for replacements. Add a contract test: custom-engine turn-boundary event must carry the resume token the backend persists.

2. **[MEDIUM] [CONFIRMED] TypingDeltas bypass the secret redaction applied to StepEvents**
   - Worker: `worker/worker/engine/graph.py:253-257` (delta_sink called with raw chunk content) and `:356-362` (`_delta` — no redact); redaction lives only in EventEmitter (`worker/worker/engine/events.py:19,105,116,142,156`). Backend: relay forwards deltas unvalidated (`backend/app/events/relay.py:112-119`) to WS clients.
   - Break: `redact()` strips Bearer tokens/private keys/etc. (security.py:31-52) from every durable StepEvent, but the same model output streamed seconds earlier as a TypingDelta reaches the browser unredacted. The live stream and the durable record disagree; any client-side capture of the WS feed holds the secret the DB never stored.
   - Prior-claim correction: harness_diagnosis.md line 93 states "redaction is egress-only (events/deltas/approvals)" — deltas are NOT redacted in the custom engine (nor in normalize.py, which redacts nothing at all: normalize.py has no redact import; but that path is legacy).
   - Fix direction: apply `redact()` inside `graph._delta` (or in the runner's `_delta_sink`, runner.py:159-160) so both egress legs share the same posture.

3. **[MEDIUM] [CONFIRMED] seq contract violated on worker container replacement; duplicate (thread_id, seq) rows**
   - Worker: seq allocator is per-process — `worker/worker/engine/events.py:33` (`self._seq = 0`), instantiated once per EngineRunner (`worker/worker/engine/runner.py:101`); SDK path same (normalize.py:83, main.py:73). Backend: ingest inserts without uniqueness enforcement (`backend/app/events/bus.py:137-144` — `next_seq` only ratchets forward) and the events table has only non-unique indexes (`backend/app/db/models/event.py:26-32`); replay reads `ORDER BY thread_id, seq` with no dedupe (`backend/app/services/sessions.py:69`).
   - Break: the contract (contracts/events.py:7) says "seq is monotonic per thread; WS relay and UI render strictly by seq." A replacement container restarts seq at 0, producing a second 0..M series for the same thread; replay interleaves two events per seq. The frontend's role-key workaround (apps/web/src/lib/runMachine.ts:165-171) only disambiguates user-vs-agent dupes (backend `_persist_user_message` uses a second allocator on thread.next_seq, runs.py:45-47), not agent-vs-agent dupes from a restart.
   - Fix direction: seed the emitter's seq from thread.next_seq at container start (or have ingest remap to next_seq), and/or add a unique constraint on (run_id, thread_id, seq) once allocation is single-sourced.

4. **[LOW] [CONFIRMED] `publish_global` fans repo names to every tenant's sockets**
   - Backend: `backend/app/events/relay.py:122-124` (fans to all run_ids) ← sole production caller `backend/app/services/repos.py:184` (`{"type":"repo_added","repo":name}`); sockets are owner-scoped per run (`backend/app/ws/events.py:48-51`), so the broadcast crosses tenants by construction. The test pins the broadcast (backend/tests/test_events_relay.py:95-102).
   - Break: user B's open run socket learns the existence and name of user A's newly onboarded repo. Name-only, no content — but it's a cross-tenant signal on a channel whose docstring (relay.py:6-7) claims fan-out is scoped to run.created_by. Frontend WsMessage union (apps/web/src/types/index.ts:60-73) doesn't even model repo_added, so it's unhandled there.
   - Fix direction: scope the invalidation (fan out only to runs owned by the onboarding user) or strip the payload to `{"type":"repo_added"}`.

5. **[LOW] [CONFIRMED] Broken test pins at the seam (push deep link + contracts lane_id)**
   - `backend/app/services/push.py:103-105` returns `/app?screen=approvals…` (matches the real console route, apps/web/src/lib/routes.ts:6) but `backend/tests/test_services_push_autonomy.py:33,49-50` assert `/?screen=…` — I ran it: `test_send_tallies_and_prunes_expired` FAILS (assertion `/app?…` != `/?…`).
   - `packages/contracts/tests/test_contracts.py:21,37` still pass `lane_id=` to StepEvent/TypingDelta though the contract renamed to `thread_id` (contracts/events.py:21,53) — I ran it: `test_step_event_minimal` FAILS (ValidationError: thread_id required).
   - Break: the schema's own test suite is red, so a real wire-breaking change would not be distinguishable from the existing noise.
   - Fix direction: update both test files to the shipped contract.

Also considered and VERIFIED-OK:
- Channel names: `deltas:{run_id}` forwarder.py:24,65 == relay.py:94,109; only producer is forwarder.py:65, only consumer relay.py:94 (tests aside). `events:{run_id}` forwarder.py:23,33 == bus.py:35,56-57. `approvals:{run_id}` forwarder.py:46 == approvals.py:62. `thread:heartbeats` forwarder.py:26,70 == heartbeats.py:25,54; payload keys thread_id/status match heartbeats.py:80-82.
- Schema round-trip: json.dumps(model_dump(mode="json")) (forwarder.py:36,65) → StepEvent.model_validate(json.loads(...)) (bus.py:124). All fields survive; StepKind str-enum → .value; datetime → ISO 8601; Optional fields → null ↔ None. Frontend StepKind union matches all 10 values (types/index.ts:43-45 vs contracts events.py:37-47).
- WS auth/scoping: cookie JWT + user status/token_version (ws/events.py:22-37); per-run ownership check before accept (ws/events.py:46-53); per-run queues (relay.py:34-39); _fanout only hits subscribers[run_id] (relay.py:68-69). No cross-run leak for steps/deltas.
- Relay delivery not assumed by worker: forwarder publishes are fire-and-forget XADD/publish (forwarder.py:28-66); no subscriber check anywhere; durable leg is the stream, acked by bus.py:177 after commit.
- Lossy-channel contents: only TypingDelta rides deltas:{run_id} (forwarder.py:60-66); approvals ride the durable approvals stream (forwarder.py:46, runner.py:229); session_id rides the durable events stream (SDK path); nothing non-lossy on pub/sub within my slice (kill/nudge on control channel is the prior audit's finding #8, outside this slice's channels).
- publish_step shape matches frontend WsMessage {type:"step", event} (relay.py:49; types/index.ts:61); delta envelope {type:"delta", delta} (relay.py:119; types/index.ts:65); note/run_stage/thread_status/approval_card/approval_resolved all present in the union.
- Push: trigger is approval-card creation from the approvals stream (approvals.py:167); payload = "Approval needed" + "{kind}: {run.title[:80]}" + deep link (approvals.py:188-190), scoped to run.created_by (approvals.py:182-183), send_to_user filters subscriptions by user_id (push.py:83) — no cross-user content leak.
- Relay parses but doesn't validate deltas (relay.py:115-119) — acceptable by design (transient), and it routes by subscription run_id, not payload run_id, so a mis-published payload can't be misrouted.

One more nuance for finding on normalize.py Q4: unhandled SDK message types fall through silently (normalize.py:99-169 has no else clause). Blocks unknown within messages are also silently skipped. The SDK path is legacy (main.py:264-277 default custom), so LOW. Also `_NOISY_SYSTEM_SUBTYPES` intentionally drops init/thinking_tokens (documented, deliberate — fine). Also normalize.py deltas are emitted per complete block, not streaming partials — "typing" effect on SDK path is block-granular, fine.

Also: Normalizer seq vs backend `_persist_user_message` — the runMachine comment confirms worker/backend each run own allocator, which is my finding #3's user-message half.

Should finding on seq be HIGH or MEDIUM? Impact: replay/audit ordering corruption after container replacement — durable record corrupted for a designed-for event (kill_replace/crash resume). Given harness-is-the-product moat, I'd say HIGH. But it's interleaved ordering, not data loss. I'll say HIGH borderline — actually MEDIUM+ ... Let me settle: replay is a survivor (§1), and the corruption is permanent in the DB. I'll mark it HIGH.

And finding #1 is HIGH (resume silently broken on default engine = harness-level broken state for a documented feature).

Push finding — the test failure is real but code is right. LOW.

Let me write up the final report. I need to be careful about line numbers I cite — all were read this session:

- forwarder.py:23,24,26,33-37,46,60-66,70-72
- relay.py:34-39,48-49,68-79,92-119,122-124
- ws/events.py:22-37,40-53,55-70
- bus.py:56-57,124,137-144,150-156,177-178
- normalize.py:83-97,99-169,161 (session_id),193-194
- graph.py:253-257,356-362
- events.py (worker engine): 33,163-172,19,105,116,142,156
- runner.py:87,99,101,159-160,229
- main.py:73,151-155,264-277
- thread_manager.py:94-108 (104-105 new uuid)
- manager.py:154-155 (sandbox)
- config.py:39
- approvals.py:62,167-190
- push.py:76-100,103-105
- heartbeats.py:25,54,80-82
- event.py:26-32
- sessions.py:65-71
- runs.py:27-60,339-344
- repos.py:184
- contracts events.py:37-47,50-72,79-89
- types/index.ts:43-45,47-58,60-73
- runMachine.ts:165-171
- tests: test_services_push_autonomy.py:33,49-50 (ran: FAIL); test_contracts.py:21,37 (ran: FAIL)
- test_events_relay.py:95-102
- security.py:31-69

Done. Write the report.


agent3:

Now I have the full picture of the runner. Let me verify a few remaining items:

1. Backend consumer of `approvals:` stream — found: `backend/app/services/approvals.py` `_loop` reads `approvals:{rid}` via xreadgroup. Let me grep for other consumers of `approvals:` across backend/app to be thorough.

2. Who calls `register_run` — need to check where the backend registers run streams. If a run is never registered, the approval request is never consumed. Let me grep.

3. `approval_timeout_seconds` in backend config vs worker's `APPROVAL_TIMEOUT_S` (900) — check backend config value.

4. Check `needs_approval` in worker/engine/tools to understand which tools reach the gate.

5. Check where `ApprovalGate` (the non-interrupt one) is used vs `ApprovalBroker`. The runner uses ApprovalBroker. Where is ApprovalGate used? Maybe legacy. Let me grep.

6. Check the emitter's `approval_card` and `approval_decision` to see event kinds.

7. Check backend `relay.publish_run_stage` with "awaiting_user" and actions ["allow_once", "always_allow", "deny_tool"] — the console gets buttons allow_once/always_allow/deny_tool. Then DecideBody validates against _VALID_DECISIONS which includes "deny_tool" but NOT... wait, worker's `wait_decision` accepts ("allow", "allow_once", "always_allow", "edited_allow", "deny"). The API allows "deny_tool" — which is NOT in the worker's accepted set! So if the console sends "deny_tool" (which is the button the backend itself advertises via publish_run_stage actions!), the worker's wait_decision guard at engine/approvals.py:205-207 would return {"decision": "deny", "reason": "unknown decision — denied"}. Actually wait — that's fail-closed, so "deny_tool" becomes a deny. That's semantically fine (deny_tool ≈ deny), though the audit row records "deny_tool" while the worker records "deny". Hmm, but actually let me check: does the console map deny_tool → deny before calling the API? The API's _VALID_DECISIONS includes both "deny" and "deny_tool". If "deny_tool" is RPUSHed to the worker, the worker treats it as unknown → deny. Fail-closed, so not a security hole, but a semantic mismatch: reason is lost ("unknown decision — denied" instead of the user's reason). Actually wait — the reason: `{"decision": "deny", "reason": "unknown decision — denied"}` — the user's reason is dropped. Minor.

Also "approved"/"rejected" (plan-approval verdicts) are in _VALID_DECISIONS but NOT in the worker's accepted set ("allow", "allow_once", "always_allow", "edited_allow", "deny"). If a plan approval decision "approved" is RPUSHed to a worker BLPOP... the worker would deny it as "unknown decision"! But wait — are plan approvals consumed by a different path? The H-24 comment in api/approvals.py says "plus the plan-approval verdicts (approved/rejected)". Where do plan approvals go? Let me check if there's a plan-approval path that uses the same decide endpoint and the same resolve_approval RPUSH. If a plan-approval card (kind="plan") is decided with "approved", resolve_approval RPUSHes {"decision": "approved"} to approval:{id}:decision. If a worker is BLPOPing on that key, it would deny. But maybe plan approvals don't come from the worker's BLPOP flow — maybe they come from a different flow (e.g., backend-created plan approval cards where no worker is listening). Let me check where kind="plan" approvals are created.

8. Worker bridge (worker/approvals.py — the SDK bridge) XADDs payload `{"tool": tool_name, "input": tool_input}` — note the inner key is "input", NOT "args"! While the forwarder (engine path) XADDs `{"tool", "args", "preview", "destructive", "always_allowable"}`. The backend `_create_card` just stores `payload` as JSON — it doesn't read inner keys, so both shapes are stored fine. But the console presumably renders based on payload keys. The task says "verify ... the payload's inner JSON keys (tool, args, preview, destructive, always_allowable) are read with matching names". The backend doesn't read inner keys at all — it stores payload opaquely and fans it out. So the mismatch between bridge ("input") and engine ("args") payloads matters only for the console rendering. Let me check if there's console code... that's outside my slice (console is frontend). But I should note the two payload shapes differ: bridge sends {"tool", "input"}, engine sends {"tool", "args", "preview", "destructive", "always_allowable"}. The backend is agnostic. UNVERIFIED for console.

Wait — is the SDK bridge (worker/approvals.py) actually used in production? The runner uses the engine graph with ApprovalBroker. Where is ApprovalBridge used? Let me grep for ApprovalBridge usage. It's the claude_agent_sdk can_use_tool callback — probably used by worker/main.py (the SDK-based worker) rather than the engine runner. Let me check worker/main.py.

9. Decision key name: backend RPUSH `approval:{approval_id}:decision` (control.py:33-35). Worker BLPOP `approval:{approval_id}:decision` (approvals.py bridge:70, engine broker:194, engine gate:74). Match confirmed.

10. Decision JSON keys: backend sends {"decision", "reason"} (control.py:35). Worker reads decision.get("decision") and decision.get("reason") (bridge:89-95, broker:205-208, gate:95-111). Match confirmed.

11. Decision value strings:
- Backend API accepts: {"allow", "allow_once", "always_allow", "deny", "deny_tool", "approved", "rejected"} (api/approvals.py:26-27).
- Worker broker accepts: ("allow", "allow_once", "always_allow", "edited_allow", "deny") (engine/approvals.py:205-206). Note: "edited_allow" is accepted by the worker but the API does NOT accept it — so edit-and-resend can never come through the API! The graph gate handles "edited_allow" with edited_args (graph.py:440-449), and the broker accepts it, but the backend API validator rejects "edited_allow" with 422. So the edit-and-resend feature is unreachable through the backend API. That's a seam mismatch — CONFIRMED. Unless there's another path (console calling a different endpoint?). Let me grep for "edited_allow" and "edited_args" across the backend.

- "deny_tool" accepted by API, not by worker → silently becomes deny with reason dropped. Fail-closed but lossy.
- "approved"/"rejected" accepted by API, not by worker broker → would become deny if a worker were listening. Need to check the plan-approval path.

12. approval_id generation: worker-side, uuid4 in three places: bridge (approvals.py:57), gate (engine/approvals.py:70), broker card_payload (engine/approvals.py:167). Backend `_create_card` falls back to uuid4 if missing (services/approvals.py:125) but worker always sends it. Uniqueness: uuid4 — collision probability negligible. Restart: on container restart, the runner services PENDING interrupts from the checkpoint BEFORE invoking (runner.py:192-207), reusing the persisted payload with the SAME approval_id — good, no orphaning. That's the re-drive path and it's explicitly handled. Backend re-drive: `_create_card` is idempotent on approval_id (M-34, services/approvals.py:142-152) — a duplicate XADD (re-emitted card on restart) skips re-insert. Good.

But wait — there's a subtlety: on restart, the runner re-emits the card via `_emit_approval_card(payload)` → `publish_approval_request` XADDs again with the same approval_id. Backend dedupes via `session.get(Approval, approval_id)`. Good. But what if the approval was already decided before the crash? Then the decision is in the Redis list `approval:{approval_id}:decision` — wait, no. If the worker crashed while BLPOPing, and the human decided, the decision sits in the Redis list. On restart, the runner re-emits the card (duplicate, deduped by backend), then BLPOPs on the same key and gets the decision immediately. 

But what if the approval was already decided AND consumed? Can't be — if the worker consumed the decision, it would have resumed the graph and checkpointed... actually, a crash between BLPOP-return and checkpoint-write would re-BLPOP on restart and find the list EMPTY (the decision was popped). Then the worker waits the full timeout and denies, while the backend row says "allow". State-machine disagreement! Let me think about this carefully:

- Worker BLPOP pops the decision from the list (destructive read).
- Worker publishes decision event, then `graph.ainvoke(Command(resume=decision))` — the checkpoint is written when the graph node completes... actually with LangGraph, the checkpoint is written at node boundaries. The resume value is persisted in the checkpoint when the graph processes it. A crash between BLPOP return and the next checkpoint write loses the decision: on restart, the pending interrupt is still in the checkpoint (good), the runner re-BLPOPs, but the list is now empty (popped). The human already clicked. The backend row says decided. The worker waits 900s and denies. The backend row says "allow" (or whatever), the engine denies. State-machine disagreement confirmed — but is it likely? The window is small (between BLPOP return and checkpoint write). It's a RISK, not a common path. Also note: the decision event is published to the events stream BEFORE the resume (runner.py:216-217), so the event log says "decision: allow" while the engine then denies on timeout after restart. Actually wait — if it crashes before ainvoke, the decision event WAS published (durable stream). So the events table records an "allow" decision, the approval row records "allow", and the engine denies after restart+timeout. Triple disagreement. This is a genuine at-least-once/exactly-once seam issue. Severity: MEDIUM (small window, fail-closed outcome — denies, so safe direction, but confusing audit trail).

Actually — is it fail-closed? Yes, timeout → deny. So the safety property holds. The disagreement is audit-trail-level. MEDIUM/LOW.

13. Timeout semantics:
- Worker: BLPOP timeout 900s (APPROVAL_TIMEOUT_S default 900, runner.py:90) → deny.
- Backend: `expires_at = now + approval_timeout_seconds` (services/approvals.py:124, 150). What's the backend's `approval_timeout_seconds` default? Need to check config. If the two disagree (e.g., backend 900 vs worker 900 — same default?), the sweep stamps decision=timeout at expiry. Let me check config.py.
- The sweep `_expire_stale` stamps decision="timeout" and fans out approval_resolved — but does NOT RPUSH to the decision list. So the worker isn't woken (it's already timed out anyway). Fine.
- If worker times out (deny) but human clicks Approve 5 min later: the sweep should have stamped the row "timeout" at expires_at, so `decide` raises ValueError("approval not found or already decided") → 409. Good — IF the sweep ran. The sweep runs every 30s (SWEEP_INTERVAL_SECONDS) in the service loop. But note: the sweep only runs when `_loop` iterates — and the loop blocks on xreadgroup with block=1000ms. So the sweep runs at most ~1s late plus 30s cadence. But there's a subtle gap: between expires_at and the sweep's stamp, the pending API filters expired rows out of the LIST (api/approvals.py:60-65 `_expired`), but the DECIDE endpoint does NOT check expiry! `decide` (api/approvals.py:79-97) only checks existence and ownership, then calls service.decide which checks `approval.decision is not None`. If the sweep hasn't run yet (up to ~31s after expiry), a decide on an expired-but-unstamped approval succeeds: row stamped with the human's decision, RPUSH to `approval:{id}:decision` — where no worker is listening (it already denied on timeout). The decision sits in a Redis list forever (no TTL on the key — RPUSH creates a list with no expiry; it leaks). The engine already denied; the row says "allow". State-machine disagreement: audit says allowed, engine denied. The Redis list key leaks forever (no TTL, no cleanup). CONFIRMED BUG (small window, fail-closed for safety but corrupts audit + leaks Redis keys).

Actually wait — is it really true the worker already denied? Worker timeout is 900s from BLPOP start; backend expiry is `requested_at`... no — backend `expires_at = card-creation-time + ttl`. The card is created when the backend consumes the XADD, which is AFTER the worker started BLPOPing (XADD → consume → create). So backend expires_at ≈ worker_timeout + small_delta (stream latency). So backend expiry is slightly LATER than worker timeout. The worker denies at T+900; the backend row expires at T+900+δ. Between worker-timeout and backend-expiry+sweep, the card is still decidable. So the window is: [T+900, T+900+δ+sweep≤31s] plus actually the whole window where the row is unexpired-but-worker-timed-out... no wait, the row expires at T+900+δ which is AFTER the worker's T+900 deny. So in [T+900, T+900+δ] the row is unexpired and decidable, and the worker is gone. Plus [expiry, sweep] it's expired-but-unstamped and STILL decidable via the API (no expiry check in decide). So the total window is δ + up-to-31s. Confirmed.

Also: what are the actual defaults? Worker: APPROVAL_TIMEOUT_S default 900 (runner.py:90). Backend: need to check `approval_timeout_seconds` in config. Let me grep.

14. Double-decide race: `services/approvals.py:194-211` decide: `session.get` → check `decision is not None` → set → commit. Check-then-act, no `FOR UPDATE`. Two concurrent clicks: both sessions read decision=None, both commit... second commit overwrites first (last-writer-wins on the row), and BOTH RPUSH to the decision list. The worker's BLPOP gets the FIRST pushed decision; the second stays in the list (leaked). If the two decisions differ (allow vs deny), the row shows the second, the worker got the first. Audit-vs-engine disagreement. CONFIRMED (matches backend diagnosis #5). Also the API returns 200 to both clickers (both think they decided). Actually wait — would the second commit fail? No — SQLAlchemy would just UPDATE the row again; no version column, no optimistic lock. Both succeed. Both RPUSH. Confirmed.

What does the worker do with a second decision? Nothing — BLPOP returns after the first; the second decision sits in the list forever (no TTL). Leaked key + if the SAME approval_id were ever reused (uuid4, won't happen), it'd be stale. So: leaked Redis keys accumulate for every double-decide and every decide-after-timeout. No cleanup anywhere. Let me verify there's no TTL set on the decision key... control.py resolve_approval only RPUSHes, no EXPIRE. Confirmed leak.

15. always_allow: worker bridge whitelists by tool NAME (approvals.py:50-55) — the SDK bridge path. The engine gate re-verifies is_destructive (engine/approvals.py:64-67, 178-180). Backend: does it persist always_allow anywhere? The backend just passes the decision string through resolve_approval → RPUSH. The worker (graph.py:452-453) calls broker.persist_always_allow(name) → SADD always_allow:{run_id}. So always_allow persistence is worker-side Redis only; the backend DB records only the decision string "always_allow" in the row. The backend does NOT persist an always-allow rule anywhere (no table). So on run end, the Redis set... does always_allow:{run_id} have a TTL? SADD with no EXPIRE — leaks forever too. Let me check if anything expires it. grep always_allow across worker/backend.

Also the prior-audit claim (worker #11): "worker/approvals.py:50-55 whitelists by tool name unconditionally; the engine gate re-verifies is_destructive on every always-allow hit (engine/approvals.py:64-67). Always-allowing Bash once permanently whitelists git push --force."

Let me verify: worker/approvals.py:50-55 — the SDK bridge: `if tool_name in AUTO_ALLOW_TOOLS or tool_name in self.always_allowed: allow`. Then checks Redis set. NO is_destructive check in the bridge! So in the SDK-bridge path, always-allowing "Bash" whitelists ALL Bash commands including `git push --force`. The engine gate (engine/approvals.py:64-67) DOES re-verify is_destructive: `if tool_name in self._always_allowed and not is_destructive`. And the broker's is_always_allowed (176-182) also checks is_destructive first. So the claim is CONFIRMED for the SDK bridge path (worker/approvals.py) and FIXED for the engine path. But which path is live? The engine runner (runner.py) uses ApprovalBroker with is_always_allowed → destructive-safe. The SDK bridge (worker/approvals.py) is used by... let me grep for ApprovalBridge instantiation. worker/main.py probably. Let me check.

Also note: the bridge's always-allow set and the engine's set share the SAME Redis key `always_allow:{run_id}`. So an always_allow persisted via the engine (class-scoped, destructive-safe) is ALSO read by the bridge (name-based, destructive-unsafe) if both run in the same run. And vice versa: the bridge SADDs tool names (e.g. "Bash") which the engine would read as tool classes... the engine's tool names are "terminal_exec" etc., while SDK tool names are "Bash", "Read" etc. Different namespaces — "Bash" in the set never matches engine's "terminal_exec", and "terminal_exec" never matches SDK's "Bash". So cross-contamination is unlikely but the key is shared. Minor.

16. Approval after thread death: thread killed while approval pending. The worker's kill path: control pump sets _stop (runner.py:426-435) — but the runner is blocked in `wait_decision` BLPOP inside `_invoke_with_approvals`, which is awaited from `_run_turn` in the main task. The kill sets _stop but the main loop is stuck in BLPOP for up to 900s! The `while not self._stop.is_set()` idle loop isn't running during a turn. So kill during a pending approval does NOT wake the BLPOP — the thread lingers until the approval times out (up to 15 min) or the human decides. The harness diagnosis #8 says "kill not honored mid-turn... a kill during a turn (or 900s approval wait) burns budget until turn end". Confirmed for the approval wait. Does the backend clean up the pending row on thread death? Need to check thread stop path — grep for Approval cleanup in thread_manager/run_manager. Probably not — the row stays pending until the sweep stamps it timeout at expiry. And the worker's BLPOP: on SIGKILL (container replacement), the BLPOP dies with the process; on restart the pending interrupt is re-serviced (re-publish + re-BLPOP) — but wait, if the thread was KILLED (not crashed), does the backend restart the container? kill_replace re-stamps... that's the replace flow. For plain kill, thread stops; on resume (new container), the checkpoint's pending interrupt is re-serviced — the runner re-publishes the card (deduped by backend if row exists... but the row may have been stamped "timeout" by then — dedupe still skips re-insert, so the card is NOT re-surfaced to the UI! The worker BLPOPs for 900s on a decision key that will never get a push because the row says "timeout" and the decide endpoint 409s. So a resumed thread with a previously-timed-out approval burns 900s then denies. Hmm — actually is that right? On resume, `_emit_approval_card` re-XADDs with the same approval_id; backend `_create_card` sees existing row (decision="timeout") and skips re-insert — but STILL fans out the card and publishes run_stage awaiting_user! Look at services/approvals.py:142-167: if existing is not None, skip insert — but the fanout and push still happen unconditionally. So the console gets an "approval_card" WS message for a row that's already decided (timeout). The user clicks, decide 409s. Minor UI confusion. And the worker waits 900s → deny. Actually that's the correct fail-closed outcome for a re-driven old approval, just slow.

Hmm wait, actually there's a subtler issue: on resume after the row was stamped "timeout", the worker re-BLPOPs and waits the FULL 900s again even though the backend already knows it's timed out. The backend has no way to tell the worker "already timed out, deny immediately". Not a correctness bug (fail-closed) but a 15-min stall per re-driven timed-out approval. RISK/LOW.

17. Backend consumer registration: `register_run` — who calls it? If the run isn't registered, approvals:{run_id} is never consumed and every approval times out into deny. Let me grep.

18. `xreadgroup` with `block=None if in_memory() else 1000` — fine.

19. One more: the consumer group "approvals" with consumer name "backend-1" — if two backend replicas run, both use consumer "backend-1"... same consumer name in one group = they share pending entries; XAUTOCLAIM absent. A crash with unacked messages: pending entries are never reclaimed (no XAUTOCLAIM, no claim-on-startup). But messages are acked per-message after processing (services/approvals.py:87), and the group is created with id="0" so history is replayed on first create... but BUSYGROUP on restart means the group exists with its last-delivered pointer; unacked pending messages from a crashed consumer are NOT redelivered (XREADGROUP with ">" only delivers new messages). So a backend crash between card-create and XACK... wait, the ack happens after _create_card; if crash between DB commit and XACK, the message is pending forever, never redelivered → but the row exists (durable), so the card is in the DB and the pending API shows it. The fanout may have been missed but the row is durable. OK-ish. But if crash between XADD-read and card-create, message pending forever → card never created → worker times out → deny. Fail-closed but the human never sees the card. No XAUTOCLAIM anywhere? Let me grep. This is a real consumer-group gap: no pending-entry reclamation. RISK.

Also `xgroup_create ... id="0"` with mkstream — on first run reads from beginning. Good.

20. Check `needs_approval` to see which tools are gated — quick look at tools/__init__.

21. Check config approval_timeout_seconds default.

22. Check where decide endpoint's "deny_tool" comes from — publish_run_stage actions ["allow_once", "always_allow", "deny_tool"] (services/approvals.py:157-158). So the console's buttons are allow_once/always_allow/deny_tool. If the console sends the literal action string "deny_tool" to the decide API, it passes validation (_VALID_DECISIONS includes "deny_tool"), gets RPUSHed, and the worker broker rejects it as unknown → deny with reason "unknown decision — denied". The user's reason is dropped, and the audit row says "deny_tool" while the worker logs "unknown decision". Fail-closed, but the reason loss and audit mismatch is a seam defect. Also "allow_once" → worker accepts ("allow","allow_once") → allow. "always_allow" → accepted. So only "deny_tool" mismatches. And "approved"/"rejected" are for plan approvals — need to find that path. Let me grep for kind="plan" and where plan approvals are created/decided.

Let me also check `edited_allow` — the worker supports it (broker:205, graph:440-449) but the API validator rejects it. So edit-and-resend is impossible via the API. Unless the console sends "allow" with edited args in `reason`? No. Let me grep backend for edited_allow/edited_args.

Let me run these greps now.


agent4:

# A4 — Control-Plane Seam Audit (Redis `thread:{id}:control`)

Runtime dispatch fact that frames everything: the backend sets `ENGINE` from `engine_runtime`, default `"custom"` (`backend/app/core/config.py:39` → `backend/app/sandbox/manager.py:144`), and `worker/worker/main.py:260-277` dispatches `custom` → `worker/engine/runner.py`. So `runner.py`'s `_control_pump` — **not** `main.py`'s `_control_loop` — is the production control-plane consumer. The two runtimes implement **different contracts**.

---

## Findings

**[SEVERITY: BLOCKER] [STATUS: CONFIRMED] `interrupt` is silently dropped by the default engine — every "stop" path is a no-op at the worker**
- Worker evidence: `worker/worker/engine/runner.py:426-458` — the pump branches on `kill` (426), `nudge` (436), `spawn_done` (438), `mode` (445). There is **no `interrupt` branch**; the message falls through to a heartbeat (458) and the loop continues. Only the legacy SDK runtime handles it (`worker/worker/main.py:171-173` → `client.interrupt()`).
- Backend evidence: interrupt is the payload of all three stop paths — `POST /threads/{id}/stop` (`backend/app/api/threads.py:50`), `stop_run` (`backend/app/orchestrator/run_manager.py:240`), `stop_thread` (`run_manager.py:320`). All three treat it as an immediate stop: `stop_run` stamps rows `stopped`, frees capacity slots, and **deletes the gateway key** (`run_manager.py:232-243`, `thread_manager.py:222-233`); `stop_thread` same (`run_manager.py:325-332`); the API returns `{"ok": True}` (`threads.py:51`).
- What breaks: the user clicks Stop, the UI says "Stopped by you — all work preserved" (`run_manager.py:215-216`), and the worker keeps its turn running, burning budget. Its subsequent "running" beats are then **ignored** by the persister because the row is terminal (`backend/app/services/heartbeats.py:115` — status only written while `ACTIVE_STATUSES`), so the divergence is permanent and invisible. The thread only dies indirectly when its next LLM call fails on the deleted gateway key — surfacing as "failed", contradicting the banner. Verified: no other worker consumer of `control.queue` exists in the custom engine (grep: only `runner.py:424` and `main.py:169`).
- Fix direction: implement `interrupt` in `_control_pump` (cancel the in-flight `_run_turn` task, set `_stop`, heartbeat `stopped`), or make the backend stop paths send `kill` + `stop_container`. Add a worker→backend ack and gate the DB stamp/key release on it.

**[SEVERITY: HIGH] [STATUS: CONFIRMED] `kill_replace_thread` trusts a lossy, un-acked kill that the worker cannot honor mid-turn — 15s wait is log-only**
- Worker evidence: kill sets `_stop` in the pump (`runner.py:426-435`) but `_stop` is only polled at the idle-loop top (`runner.py:283-288`). Mid-turn the main loop is inside `_invoke_with_approvals` (`runner.py:334`, `203-218`) or a 900s approval BLPOP (`runner.py:211` → `worker/worker/engine/approvals.py:194`, timeout 900 at `:32`). Nothing cancels the in-flight turn. A kill published during a reconnect window (`worker/worker/control.py:40-63`, backoff 0.5→5s) is gone forever.
- Backend evidence: `run_manager.py:408` publishes kill, then `wait_for_container_exit` polls docker for 15s and **logs and proceeds on timeout** (`backend/app/sandbox/manager.py:248-259`), then spawns the replacement mounting the **same session volume** (`run_manager.py:443-449`, `manager.py:190-192`). No `stop_container` fallback exists on this path.
- What breaks: even a *delivered* kill blows the 15s window whenever the thread is mid-turn or parked on an approval — two live containers writing one session volume, the §1 corruption the philosophy calls unforgivable. A *lost* kill guarantees it.
- Fix direction: after kill publish, escalate to `sandbox_manager.stop_container` on timeout (the pattern `abandon_run` already uses at `run_manager.py:267-268`) and fail the replace instead of proceeding.

**[SEVERITY: HIGH] [STATUS: CONFIRMED] `spawn_done` is a one-sided contract — worker consumes it, nobody publishes it**
- Worker evidence: `runner.py:438-444` — the pump calls `self._spawn_registry.finish(msg.text)` on `spawn_done`; the comment states this is "the production path that moves a spawn out of 'running'… without it the registry saturated permanently after SWARM_MAX_SLICES spawns and every subsequent fan-out was vetoed."
- Backend evidence: `LaneControl` has no spawn_done publisher (`backend/app/events/control.py:13-39` — only interrupt/nudge/set_mode/kill/resolve_approval). Repo-wide grep for `spawn_done` finds **only** the two runner.py lines. The worker never publishes it either.
- What breaks: the control-plane half of the spawn-liveness protocol is dead code. Spawn registry entries clear only via the 2h watchdog, so fan-out vetoes after 8 spawns until the watchdog fires — exactly the failure the comment claims this message prevents.
- Fix direction: publish `spawn_done` from the backend heartbeat-ingest terminal-stamp path (or from the child worker to its parent's control channel on completion), or delete the handler and honest-up the comment.

**[SEVERITY: MEDIUM] [STATUS: CONFIRMED] No ack/confirmation path exists for any control message; exactly-once-critical flows ride fire-and-forget pub/sub with no retry**
- Worker evidence: pub/sub subscribe only (`worker/worker/control.py:44`); reconnect backoff drops everything published while unsubscribed (`control.py:60-63`). The only reverse signal is the heartbeat status (`worker/worker/forwarder.py:68-72`).
- Backend evidence: all four methods are bare `publish` (`control.py:21,24,27,30`). No call site polls for worker confirmation: kill_replace polls **docker**, not the worker (`run_manager.py:416-419`); `_wait_for_heartbeat` gates only readiness and explicitly proceeds on timeout (`run_manager.py:471-491`). The trigger flow records `status: "nudged"` immediately after the publish (`backend/app/services/triggers.py:364-366`) — a lost nudge there means the webhook event was dedupe-committed yet never acted on.
- What breaks: (a) kill before replace → finding 2; (b) stop interrupts → finding 1; (c) trigger/responder nudges (`triggers.py:364`, `responder.py:85`, `runs.py:390-392`) silently vanish in a reconnect window; (d) nudge-after-replace can still race (finding 8). Note the design contrast: the *approval* channel is durable (backend `rpush` at `control.py:32-36`, worker `blpop` at `approvals.py:194`) — decisions survive reconnects; control messages don't.
- Fix direction: worker publishes a control-ack (per-message nonce echoed to an ack list/stream); backend retries exactly-once-critical messages until ack or escalates to docker force-stop.

**[SEVERITY: MEDIUM] [STATUS: CONFIRMED] Mode payload contract mismatch — backend sends permission-mode strings, custom engine parses blueprint-mode names**
- Worker evidence: `runner.py:447` `Mode(msg.mode)` against enum `ask|plan|development|debug|goal` (`worker/worker/engine/state.py:30-35`); `ValueError` swallowed silently (`runner.py:456-457`). The SDK runtime instead calls `client.set_permission_mode(msg.mode)` (`main.py:208-209`).
- Backend evidence: `LaneControl.set_mode` sends `{"type":"mode","mode": permission_mode}` (`control.py:26-27`) where the values are `default|acceptEdits|bypassPermissions` (`backend/app/orchestrator/autonomy.py:16-24`). The test pins the wrong-side contract: `set_mode("thread-1", "acceptEdits")` (`backend/tests/test_events_control.py:26-28`).
- What breaks: if `set_mode` ever gains a caller, the custom engine silently ignores every message while the SDK runtime honors it — behavior depends on `ENGINE`. Latent today: grep confirms **zero production callers** of `set_mode` (only the test and the conftest fake at `backend/tests/conftest.py:386`); `switch_mode` deliberately avoids the channel (`run_manager.py:546-563`). So: confirmed contract drift, currently no live blast radius.
- Fix direction: split the contract — either send blueprint-mode names the custom engine accepts, or rename the backend method to reflect SDK-only semantics and delete the dead worker branch.

**[SEVERITY: MEDIUM] [STATUS: CONFIRMED] Backend's "nudge = graceful interrupt+inject+resume" is false on the default engine; nudge during a 900s approval BLPOP is delayed, not injected**
- Worker evidence: custom engine queues the nudge (`runner.py:436-437`) and injects it only at the **turn boundary** as a NUDGE-tagged `HumanMessage` (`runner.py:364-387`) — its own docstring admits this (`runner.py:24-28`). No running turn is ever interrupted. During an approval wait the main loop is parked in `broker.wait_decision` (`runner.py:211`, `approvals.py:193-199`); the nudge sits in `_pending_nudges` until the decision arrives and the turn completes. The SDK runtime does implement interrupt+inject+resume (`main.py:174-207`).
- Backend evidence: docstring claims "nudge is graceful interrupt+inject+resume on the worker side" (`control.py:1-3`; echoed at `run_manager.py:280`).
- What breaks: the "nudge a drifting lane" steering promise (the reason the spike's check (g) exists) doesn't hold mid-turn on the default engine — a runaway turn runs to completion first. Inject+resume itself **is** implemented and the text does reach the next turn; queued delivery while idle works (picked up within 5s, `runner.py:283-288`); nothing is lost, only delayed. Edge: the intent path can send an empty-text nudge (`runs.py:390-392` passes `intent.text or ""`), which injects an empty `HumanMessage` and burns a turn.
- Fix direction: correct the backend docstring to turn-boundary semantics, or implement mid-turn cancel in the custom engine; reject empty nudge text at the API.

**[SEVERITY: MEDIUM] [STATUS: CONFIRMED] Two divergent stop paths; API `/stop` does no bookkeeping and both report success for unguaranteed delivery**
- Backend evidence: `POST /threads/{id}/stop` calls `control.interrupt` **directly** (`threads.py:50`) — no DB stamp, no key release, no slot release — while the intent path routes to `run_manager.stop_thread` (`backend/app/api/runs.py:429`) which does all three (`run_manager.py:320-332`). Both return `{"ok": True}` (`threads.py:38,51`) where "ok" means "published to Redis", not delivered or applied — with finding 1, it means "published a message the default worker ignores".
- Worker evidence: `runner.py:426-458` (interrupt dropped).
- What breaks: same verb, two different side-effect profiles depending on which UI surface invoked it; and the status code asserts success for a fire-and-forget publish the worker may not even receive. IDOR guards themselves are correct on all three endpoints (verified-OK below).
- Fix direction: route `/threads/{id}/stop` through `run_manager.stop_thread`; return 202-style semantics ("stop requested") or gate 200 on a worker ack.

**[SEVERITY: MEDIUM] [STATUS: CONFIRMED] Spawn-time `MODE` is a static backend default, not `run.mode` — goal-mode graph branches never fire**
- Backend evidence: `thread_env` sets `"MODE": self.settings.engine_default_mode` (`backend/app/sandbox/manager.py:145`), default `"development"` (`backend/app/core/config.py:43`) — for **every** run, regardless of `run.mode` (the real mode is only stored in `spawn_context`, `backend/app/orchestrator/thread_manager.py:111`, and used to pick the *backend* blueprint, `run_manager.py:162`).
- Worker evidence: `runner.py:81` `Mode(os.environ.get("MODE", "ask"))` seeds graph state (`runner.py:139`); the graph branches on `Mode.GOAL` (`worker/worker/engine/graph.py:98,153,1033,1068`).
- What breaks: a goal-mode run's worker boots as `development` and never takes the GOAL branches. Not a control-channel message, but it is the mode-delivery seam: mode crosses the boundary once at spawn (wrong value) and the mid-session channel that could correct it is unwired (finding 5).
- Fix direction: pass `run.mode` (validated against the worker enum) as `MODE` in `thread_env`.

**[SEVERITY: LOW] [STATUS: CONFIRMED] `_wait_for_heartbeat` races the worker's subscribe; timeout path nudges anyway**
- Worker evidence: `runner.py:244` publishes the first heartbeat **before** the control listener task is created at `runner.py:248` (subscribe inside `control.py:44`). The readiness signal therefore predates readiness.
- Backend evidence: `_wait_for_heartbeat` returns True on the key and the caller "nudges either way" on timeout (`run_manager.py:467-491`).
- What breaks: the nudge-after-replace mitigation has a residual lose-the-nudge window on boot, plus the full 0.5–5s window on every worker reconnect. A lost nudge here strands the replacement idle on the resumed volume with no user-visible failure.
- Fix direction: gate on an explicit "control-subscribed" heartbeat payload/status, and retry the nudge on the worker's next heartbeat.

---

## (a) VERIFIED-OK

- **Channel string identical both sides**: `f"thread:{thread_id}:control"` — backend `backend/app/events/control.py:17-18`, worker `worker/worker/control.py:28`. All call sites pass bare `Thread.id`: `threads.py:50` (IDOR-guarded path param), `run_manager.py:240, 266, 313, 320, 408`; worker side from `THREAD_ID` env (`runner.py:79` → `:106`), set from `thread.id` (`sandbox/manager.py:131`). No prefixed/wrong-id caller found.
- **Malformed-message robustness**: worker skips bad JSON (`control.py:56-57`), defaults missing fields to `""` (`control.py:50-54`), and unknown types fall through the pump without crashing the loop (`runner.py:458-468`). Backend only emits the four known types (`control.py:20-30`), so no live type the worker can't tolerate.
- **IDOR/tenant guards**: `/threads` nudge/stop/pin all check run ownership + thread-in-run (`threads.py:30-36, 44-49, 59-65`); intent path re-checks (`runs.py:291-292`).
- **Approval decisions are durable, not pub/sub**: backend `rpush` (`control.py:32-36`) vs worker `blpop` (`approvals.py:194`) — decisions don't fall in reconnect windows; deny-on-timeout and deny-on-malformed are deterministic (`approvals.py:198-207`).
- **`abandon_run` does not depend on the kill message**: force docker stop+remove then shred (`run_manager.py:267-272`, `manager.py:230-237`). The one safe kill flow.
- **Reconnect loop mechanics**: cancel-safe, cleanup guarded, backoff capped (`worker/worker/control.py:58-74`).
- **Nudge liveness guard**: backend refuses to nudge terminal/missing threads instead of resurrecting them (`run_manager.py:294-308`).

## (b) CORRECTED prior claims

- **Worker #8 (kill not honored mid-turn; lossy kill channel)** — CONFIRMED and sharpened: refs are `runner.py:283-288` + `:426-435` (poll-only) and `worker/control.py:40-63` (lossy window). New: even a *delivered* kill defeats `kill_replace`'s 15s wait whenever the thread is mid-turn or in the 900s approval BLPOP; "SIGKILL skips the cascade drain" confirmed (`runner.py:302-318` drain lives in `finally`; `manager.py:234-235` force-removes after 5s).
- **Backend #3 (kill rides lossy pub/sub, no force-stop fallback)** — CONFIRMED for `kill_replace_thread` (`run_manager.py:408-419`); partially corrected: `abandon_run` **does** force-stop containers (`run_manager.py:267-268`), so the gap is specific to kill_replace; and `stop_run`/`stop_thread` are worse than "lossy kill" — they send `interrupt`, which the default engine never acts on (finding 1).
- **Both control-plane docstrings over-promise**: `worker/worker/control.py:1-7` ("interrupt (stop, immediate)") and `backend/app/events/control.py:1-3` ("Interrupt is immediate; nudge is graceful interrupt+inject+resume") describe only the legacy SDK runtime (`main.py:165-213`), not the default custom engine.
- **Worker #8's "kill/nudge ride lossy pub/sub"** — extend the list: `interrupt`, `mode`, and `spawn_done` ride the same channel; `spawn_done` additionally has no publisher at all.

## (c) Slice summary

The control plane is a well-built listener paired with a fire-and-forget publisher and no acknowledgment protocol, and its message contract has silently forked between the two worker runtimes: the default custom engine ignores `interrupt` entirely, so every backend stop path reports success while the thread runs on with a released slot and a deleted gateway key. The one flow that does force-stop containers (`abandon_run`) is safe, but `kill_replace` combines a lossy kill, a worker that can't honor kill mid-turn, and a log-only 15s wait into a live session-volume corruption window. `spawn_done` is dead on arrival (no publisher), mode messages are a latent contract mismatch, and nudges are delayed-not-lost on the default engine — the seam's durable, acked approval channel proves the right pattern exists one file away.


agent5:

# A5 — Run Lifecycle State Machine at the Worker↔Backend Seam

Files read in full this session: `worker/worker/engine/runner.py`, `worker/worker/engine/state.py`, `backend/app/orchestrator/run_manager.py`, `backend/app/db/models/run.py` — plus seam dependencies verified directly: `packages/contracts/.../intents.py`, `backend/app/services/runs.py`, `backend/app/events/bus.py`, `backend/app/events/control.py`, `backend/app/services/heartbeats.py`, `backend/app/services/approvals.py`, `backend/app/api/approvals.py`, `backend/app/api/runs.py`, `backend/app/services/intents.py`, `backend/app/orchestrator/semaphores.py`, `backend/app/orchestrator/thread_manager.py`, `backend/app/sandbox/manager.py`, `backend/app/db/models/thread.py`, `backend/app/main.py`, `backend/app/core/config.py`, `backend/app/orchestrator/blueprints/{ask,goal,development,plan,debug,swarm}.py`, `worker/worker/{main,control,forwarder}.py`, `worker/worker/engine/approvals.py`.

---

## FINDINGS

### [SEVERITY: BLOCKER] [STATUS: CONFIRMED] `input_required` permanently freezes the thread row — every blueprint await wedges, and the capacity slot + per-repo write lock release while a live writable worker still holds the repo

- Worker evidence: `worker/worker/engine/runner.py:209-213` (heartbeats `"input_required"` while BLPOP-waiting on an approval, then heartbeats `"running"` on resume) and `runner.py:358-362` (blocked-escalation sets `"input_required"`, then heartbeats it).
- Backend evidence: `backend/app/services/heartbeats.py:115-116` — the persister writes the worker's status only `if status and thread.status in ACTIVE_STATUSES`; `backend/app/orchestrator/semaphores.py:15` — `ACTIVE_STATUSES = ("queued", "running", "idle", "interrupted")`, deliberately excluding `"input_required"` (the exclusion is intentional per `run_manager.py:288-294` and pinned by `tests/test_orchestrator_run_manager.py:294`).

What breaks, step by step, all in code read this session:
1. First approval wait: row is `"running"` (∈ ACTIVE) → persister overwrites it with `"input_required"` (heartbeats.py:115).
2. From that moment the row is frozen: every subsequent beat (`"running"` after the human decides, `"idle"` at turn end, even `"failed"`) is rejected because `"input_required" ∉ ACTIVE_STATUSES`. `heartbeat_at` keeps updating (heartbeats.py:108 is unconditional), so the row shows a *fresh heartbeat* with a *stale, wrong status* forever.
3. Blueprint awaits never return: every `_await_thread` terminal set is `("idle","completed","failed","stopped","interrupted","replaced")` — no `"input_required"` (`ask.py:108-113`, `development.py:391-393`, `plan.py:254-256`, `debug.py:256-258`, `swarm.py:280-282`, `goal.py:597-598`). For ask/development/plan/debug/swarm there is **no timeout** (`ask.py:100-115`, `development.py:382-394`), so the run hangs in INVESTIGATING/DEVELOPING/PLANNING forever. Goal's wedge guard fires at 2700s (`goal.py:584,600-602`) and fails a run whose human may have approved minutes earlier.
4. Concurrency moat inversion while frozen: capacity counting (`semaphores.py:31`) and the per-repo write lock (`semaphores.py:46-55`) both filter on `ACTIVE_STATUSES`. A supervised **writable** developer thread parked on an approval stops counting as active — a second writable thread on the *same repo* can spawn; when the human approves, the first thread resumes and both write the same repo. §2's core invariant inverts exactly at the seam.

The only in-code recoveries are a user nudge (`run_manager.py:294,309` sets `"running"`) or a terminal stamp. Minimal fix direction: make the persister treat `"input_required"` as active for *status* purposes (add it to the persister's writable set, or to `ACTIVE_STATUSES` and explicitly exclude it in capacity accounting instead), so `running`/`idle`/`completed` beats land after the approval resolves.

---

### [SEVERITY: BLOCKER] [STATUS: CONFIRMED] "Stop" never reaches the default engine — the backend publishes `interrupt`, the custom runner has no handler for it

- Worker evidence: `worker/worker/engine/runner.py:419-458` — `_control_pump` handles exactly `"kill"` (426), `"nudge"` (436), `"spawn_done"` (438), `"mode"` (445); an `"interrupt"` message falls through to a heartbeat at :458. Worker `main.py:264` defaults `ENGINE` to `"custom"` and dispatches to `engine_main()` at :276-277; only the legacy SDK runtime honors interrupt (`worker/main.py:171-173`).
- Backend evidence: `backend/app/events/control.py:20-21` — `LaneControl.interrupt` publishes `{"type": "interrupt"}`; `run_manager.py:239-243` (`stop_run`) and `:320` (`stop_thread`) send *only* `control.interrupt` — no `kill`, no `stop_container` (contrast `abandon_run` at :265-268, `finish_thread` at `thread_manager.py:257-258`). `config.py:39` — `engine_runtime: str = "custom"` is the backend's default, injected as container env `ENGINE` at `manager.py:144`.

What breaks: POST intent STOP_RUN (`api/runs.py:280-281`) → run stamped INTERRUPTED, threads stamped `"stopped"`, gateway keys deleted (`run_manager.py:232-243`) — and the worker container keeps running the turn untouched. Mid-approval the worker sits in BLPOP for up to 900s (`worker/engine/approvals.py:194`, timeout default 900 at `runner.py:90`); mid-tool it runs to turn end. The UI banner says "Stopped — all work preserved" while the workspace is still being mutated. The actual death mechanism is a side effect: the deleted gateway key makes the worker's *next LLM call* fail auth, producing a `"failed"` engine-error event (runner.py:289-293) that ingest happily appends *after* the stop banner (bus.py:137-141 has no stage check). Nothing ever waits for worker confirmation — fire-and-forget pub/sub. Also note `stop_run`'s thread filter `("running", "idle", "queued")` (`run_manager.py:226`) omits `"input_required"` threads entirely — an approval-parked thread isn't even stamped or key-released.

Minimal fix direction: teach the custom runner an `interrupt` handler (set `_stop` + heartbeat `"stopped"`, mirroring kill at runner.py:426-435), and/or make `stop_run` fall back to `control.kill` + `stop_container` like `abandon_run` does; include `"input_required"` in the stopped-thread filter.

---

### [SEVERITY: HIGH] [STATUS: CONFIRMED] `reconcile_on_boot` falsely kills healthy runs — no liveness check, no container stop, no worker notification; zombies keep writing events into "stopped" runs; resume then mounts a live session volume

- Worker evidence: the worker never checks whether the backend declared it dead — its only inbound channels are the control pub/sub (`worker/control.py:31-74`) and approval BLPOP; reconcile sends nothing on either (verified: `run_manager.py:616-654` contains no `control.*` call). The reconciled-away worker keeps heartbeating (`runner.py:470-481`) and keeps XADDing events (`forwarder.py:28-38`).
- Backend evidence: `run_manager.py:622-643` — the sweep filters purely on `run.stage`; it never calls `sandbox_manager.container_running` (exists at `manager.py:239-246`, unused here), never reads the Redis heartbeat TTL key (`thread:{id}:heartbeat`, 90s TTL, `forwarder.py:69`), never checks `Thread.heartbeat_at` freshness. It stamps threads `"stopped"` (:638-640), transitions the run INTERRUPTED (:642), releases gateway keys (:649-651) — but **never stops the containers** (no `stop_container` call anywhere in :616-654). Ingest has no stage guard: `bus.py:137-161` stores every valid event and bumps `run.last_active_at` (:157-160) against the INTERRUPTED run.

What breaks: any backend restart (deploy, crash, rolling update) with live workers declares every active run dead while the containers keep running. The key release is an *indirect, delayed* kill — the zombie dies only when its next LLM call hits the deleted key. Worse, INTERRUPTED offers `RESUME_RUN` (`services/runs.py:24`), and `resume_run` (`run_manager.py:116-148`) re-executes with `resume_from_thread_id`, which mounts the **prior thread's session volume** (`manager.py:190-192`) with **no wait-for-exit at all** — unlike `kill_replace_thread`, which at least polls 15s (`run_manager.py:416-419`). If the old container is still alive (falsely reconciled, mid-BLPOP), two containers write one session volume — the exact §1 corruption the philosophy calls unforgivable.

Minimal fix direction: reconcile must check liveness (heartbeat TTL key and/or `container_running`) before stamping; send `control.kill` + `stop_container` for genuinely dead-but-running containers; add a wait-for-exit before any session-volume remount on the resume path.

---

### [SEVERITY: HIGH] [STATUS: CONFIRMED] Hard worker death is invisible forever — no exit-code reader, no heartbeat-timeout enforcement, and 5 of 6 blueprint await loops have no timeout

- Worker evidence: exit codes 0/1 are produced at `runner.py:326` and `main.py:510-523`; a *soft* failure emits a `"failed"` heartbeat first (`runner.py:289-293`), but a hard death (OOM-kill, SIGKILL from `stop_container`'s 5s timeout at `manager.py:234-235`, or an exception in `EngineRunner.__init__` env reads at `runner.py:78-90` — before `run()`'s try block) emits nothing.
- Backend evidence: the container is started `detach=True, remove=False` (`manager.py:214-226`); nothing anywhere calls `container.wait()` or reads an exit code — `container_running` (`manager.py:239-246`) is called only by `wait_for_container_exit` (:255), used only by kill_replace (`run_manager.py:418`). No backend component consumes `heartbeat_at` or `last_active_at` for liveness: `heartbeat_at` is written by `heartbeats.py:108` and read only for API display (`api/runs.py:177`); `last_active_at` is written by `bus.py:157-160` and used only for session-list sorting (index comment, `models/run.py:26-27`). `_await_thread` without timeout: `ask.py:100-115`, `development.py:382-394`, `plan.py:244-257`, `debug.py:247-259`, `swarm.py:272-283`; only goal has `THREAD_MAX_WAIT_S = 2700` (`goal.py:584-604`).

What breaks: a crashed worker leaves its thread row `"running"` (∈ `ACTIVE_STATUSES`, `semaphores.py:15`) **forever**: the blueprint's await loop hangs → the run hangs in its active stage → the capacity slot leaks (counted by `semaphores.py:31` against the global cap of 12, `config.py:100`) → the minted gateway key leaks (`release_key` is only called from stop/finish/spawn-failure/reconcile paths — `thread_manager.py:270,285-286`, `run_manager.py:243,332,651`). The *only* recovery is the next backend restart's reconcile sweep (`main.py:61`). There is no heartbeat-timeout value to name because no enforcement exists — the 90s Redis TTL key is read solely as a nudge-readiness signal (`run_manager.py:467-491`).

Minimal fix direction: a periodic reconciler that fails threads whose heartbeat is stale beyond N×15s (and whose container is not running), releasing slots/keys and failing the owning run via the `_guarded_execute` path.

---

### [SEVERITY: HIGH] [STATUS: CONFIRMED] Reconcile sweeps VERIFYING — a legitimate human-parked stage — and resume from it re-stamps the workspace, destroying un-PR'd work

- Backend evidence: `run_manager.py:622-626` includes `RunStage.VERIFYING.value` in the zombie sweep, while `AWAITING_USER` and `PR_READY` are excluded. VERIFYING is parked-human: its actions are `review_evidence` + `create_pr` (`services/runs.py:21`), and the development blueprint's last stage-write is VERIFYING (`development.py:54`; the `evaluate` node has `stage=None`, :57). On sweep, the run flips to INTERRUPTED and its actions are recomputed to `[edit_and_resend, resume_run]` (`services/runs.py:24`, via `transition` at :47) — the `create_pr` path disappears. RESUME_RUN then re-executes the development blueprint from `hydrate` (`run_manager.py:144-147` → `_execute` → blueprint node 1), and `_develop` re-stamps the workspace with `fresh=True` — `stamp_clone` does `shutil.rmtree(dest)` (`manager.py:64-68`; `development.py:124-128` passes no `preserve_workspace`), wiping the implementation that was awaiting PR.

What breaks: every backend restart while a human reviews evidence (a run can sit in VERIFYING for days) destroys the run's terminal trajectory *and* arms a resume path that razes the un-shipped workspace. This is a false-zombie class the sweep's design (parked stages excluded) already recognizes for AWAITING_USER/PR_READY but missed for VERIFYING.

Minimal fix direction: exclude VERIFYING from the sweep (its threads are already finished/idle by construction), or distinguish "parked awaiting human" stages from in-flight stages explicitly in the reconciler.

---

### [SEVERITY: MEDIUM] [STATUS: CONFIRMED] Cost settlement and key release are missing on most terminal paths

- Worker exit modes that bypass settlement: crash/OOM/SIGKILL (never detected — finding 4); kill via stop_run/stop_thread (keys released but cost never read — `run_manager.py:243,332`); abandon (:265-276); idle-TTL completion after the blueprint already settled (post-completion nudge spend is never re-settled — `ask.py:148` runs once).
- Backend evidence: `run_manager.py` contains **zero** `settle_cost` calls (verified by grep). Blueprint coverage: `ask.py:148` — success only; the failed branch `ask.py:125-137` returns early with **no settle, no finish_thread, no key release**. `swarm.py:258-268` settles all threads — except the all-failed branch returns early at :243. `goal.py:451-452` settles only on the COMPLETED (ship) path; a verify failure raises at :410 and `_guarded_execute` marks FAILED (`run_manager.py:203-206`) with no thread cleanup of any kind. `plan.py`, `debug.py`, `development.py` never call `settle_cost` or `finish_thread` at all (verified by grep across blueprints) — development-mode runs therefore keep `run.cost_usd = 0` forever and leak both threads' gateway keys.

Minimal fix direction: settle + release in one place — e.g. a terminal-thread hook in the persister or a sweep — rather than per-blueprint happy paths.

---

### [SEVERITY: MEDIUM] [STATUS: CONFIRMED] The approval "awaiting_user" run stage is relay-only paint — the DB row never changes, and nothing re-publishes the true stage after the decision

- Worker evidence: `runner.py:209-213` (status `input_required` around the BLPOP).
- Backend evidence: `services/approvals.py:157-158` — `_create_card` calls `relay.publish_run_stage(run_id, "awaiting_user", [...])` with **no DB write** to `run.stage`; `decide()` (:194-211) fans out only `approval_resolved`, never re-publishing the run's actual stage. `RunStage.AWAITING_USER` as a *persisted* stage is used solely for plan HITL (`plan.py:224`, `debug.py:185`).

What breaks: the UI and DB diverge for the entire approval wait and everything after it (no further stage publish arrives until the blueprint's next node boundary — for an ask run, not until `_complete`). A page reload reads `run.stage` from the DB (e.g. `developing`) while the WS-painted card said `awaiting_user`. It also means reconcile sees an approval-parked run as in-flight DEVELOPING and sweeps it (finding 3). No watchdog kills a legitimately waiting run — the backend has none — but goal's 2700s wedge guard (`goal.py:600-602`) will kill one, compounded by finding 1's freeze.

Minimal fix direction: persist the awaiting state on the run row (or a thread-level state the UI reads), and re-publish the true stage in `decide()`.

---

### [SEVERITY: MEDIUM] [STATUS: RISK] Abandon with a lost kill message → unbounded event-stream growth

- Worker evidence: kill rides lossy pub/sub (`worker/control.py:40-63` — during reconnect backoff, publishes are silently missed); a worker that never receives the kill keeps XADDing to `events:{run_id}` with no `maxlen` cap (`worker/forwarder.py:31-38`).
- Backend evidence: `abandon_run` unregisters the run from ingest (`run_manager.py:273`), so no consumer group ever drains that stream again; it then stops containers (:267-268) — but `stop_container` swallows Docker failures with a log (`manager.py:236-237`).

What breaks: if the pub/sub kill is lost *and* the docker stop fails (or the 5s SIGTERM→SIGKILL window is survived by a wedged but alive process), the orphaned worker's events pile into an unread stream indefinitely — Redis memory growth with no reaper. Requires two failures stacked, hence RISK not CONFIRMED-bug.

Minimal fix direction: cap the events stream (`maxlen` approximate) or keep a dead-letter drain registered for abandoned runs until the container is confirmed gone.

---

### [SEVERITY: LOW] [STATUS: RISK] Stop/spawn race: a mid-spawn blueprint can land a live thread on an INTERRUPTED run; a late blueprint error overwrites INTERRUPTED with FAILED

- Backend evidence: `stop_run` reads stage, stamps INTERRUPTED, *then* cancels the blueprint task (`run_manager.py:234-246`) — cancellation is delivered at the next await, so a spawn already in flight (`thread_manager.spawn`, :86-168) can complete afterwards: thread row `"running"`, container live, capacity counted, on a run the user stopped. Separately, `_guarded_execute`'s guard excludes only TERMINAL_STAGES (`run_manager.py:203`); INTERRUPTED is not terminal (`services/runs.py:30`), so a blueprint raising after a stop (e.g. the next `capacity.try_acquire` failing, `thread_manager.py:86-88`) flips the user's INTERRUPTED run to FAILED, swapping `[edit_and_resend, resume_run]` for `[resume_run]` (`services/runs.py:24,26`).

Minimal fix direction: re-check run stage inside spawn after `try_acquire`, and treat INTERRUPTED as non-overwritable by the failure path.

---

### [SEVERITY: LOW] [STATUS: CONFIRMED] `last_active_at` and `heartbeat_at` are display-only — both liveness illusions are real, and neither has any consequence

- Worker evidence: heartbeats continue every 15s regardless of turn progress (`runner.py:470-481`) — a healthy multi-minute tool call never looks dead.
- Backend evidence: `bus.py:157-160` bumps `run.last_active_at` on **every** ingested event with no stage check — a chatty-but-stuck run (events flowing, turn never completing) looks alive forever, and a lingering/zombie worker reorders the session list (sorted by `last_active_at`, index comment `models/run.py:26-27`). No run_manager liveness decision reads either timestamp (verified: only `api/runs.py:177` display and `run_manager.py:467-491` readiness).

This answers brief Q5 both ways: yes to chatty-but-stuck looking alive; no false-dead risk for long turns — but only because *nothing enforces anything* (which is finding 4).

---

### [SEVERITY: LOW] [STATUS: RISK] Approval-timeout contract is configured on one side only

- Worker evidence: `APPROVAL_TIMEOUT_S` env, default 900 (`runner.py:90`) — but the container env built by the backend never sets it (`manager.py:127-169` — verified: no `APPROVAL_TIMEOUT_S`, no `IDLE_TTL_SECONDS`).
- Backend evidence: `approval_timeout_seconds: int = 900` (`config.py:98`) is operator-configurable and drives card expiry (`services/approvals.py:124,150`) and the sweep (:89-121).

What breaks: defaults match (900 == 900 — verified), but an operator raising the backend timeout creates cards that outlive the worker's BLPOP: the worker denies at 900s, the card looks live until the backend's longer expiry, and a late human decision is RPUSHed (`events/control.py:32-36`) to a key nobody is BLPOPing — silently lost, with the audit row claiming "decided."

Minimal fix direction: pass the backend's value into the container env so one setting drives both sides.

---

## (a) VERIFIED-OK

- **Terminal-stage guard on runs**: `services/runs.py:37-48` — `transition()` raises on terminal re-transition; the only legitimate exit is resume (`run_manager.py:135`, `allow_terminal_exit=True`). `TERMINAL_STAGES` = completed/failed/abandoned (`services/runs.py:30`).
- **Failure path never overwrites terminal runs**: `run_manager.py:203-206` (H-42) — checked in code, not just the comment.
- **Terminal guards on stop/abandon/kill-replace**: `run_manager.py:221-222`, `:258-259`, `:385-387`.
- **Dying-container beats can't resurrect terminal thread rows**: `heartbeats.py:115` guard + `ACTIVE_STATUSES` (`semaphores.py:15`); status-change bypass of the write throttle (:97-99); `heartbeat_at` written unconditionally (:108).
- **kill_replace waits for the old container before remounting the session volume**: `run_manager.py:416-419` → `manager.py:248-259` (best-effort 15s — see corrections).
- **finish_thread ordering**: container stop *before* terminal stamp, terminal threads skipped, key released (`thread_manager.py:249-270`).
- **abandon_run full teardown**: kill + stop_container + task cancel + shred + unregister + relay (`run_manager.py:265-276`).
- **Approval wire contract matches exactly**: backend RPUSH `approval:{id}:decision` (`events/control.py:32-36`) == worker BLPOP same key (`worker/engine/approvals.py:194`); decision strings validated at the API (`api/approvals.py:26-27`) against the worker's accepted set (`worker/engine/approvals.py:205-207`) — `deny_tool` falls through to a fail-closed deny, never an approve.
- **Approval timeout defaults agree**: 900 == 900 (`config.py:98`, `runner.py:90`), and the backend sweep stamps `decision="timeout"` for cards the worker already abandoned (`services/approvals.py:89-121`).
- **Soft worker failure propagates**: exception → `"failed"` heartbeat + exit 1 (`runner.py:289-293,326`) → persister stamps row (`heartbeats.py:115`) → blueprint await returns (`ask.py:108`) → run FAILED (`ask.py:125-136`).
- **Nudge-turn failure keeps the thread alive** (M-04): `runner.py:388-399`.
- **Worker-side vocabulary coverage**: every custom-engine status (`running`, `idle`, `input_required`, `failed`, `stopped`, `completed` — `runner.py:122,209,279,290,332,355,359,361,427,489`) is storable in `Thread.status` (`models/thread.py:31`, String(16)); nothing the worker emits is unparseable backend-side (the failures are semantic — findings 1–4 — not schema).

## (b) CORRECTED prior claims

- **Backend #3 (wait-for-exit / orphan reaper) — CONFIRMED and sharpened.** As claimed: 15s poll logs and proceeds (`manager.py:253-259`), kill rides lossy pub/sub with no `stop_container` fallback on the kill path (`events/control.py:29-30`; kill_replace does wait), reconcile never stops containers (`run_manager.py:616-654`), and no orphan reaper exists anywhere (only caller of `container_running` is the kill_replace wait). Sharpened: reconcile also performs **no liveness check** before stamping (heartbeat key and container state both ignored), and the *resume* path — not just kill_replace — mounts a prior session volume with **zero** wait-for-exit (`run_manager.py:116-148` vs :416-419).
- **Worker #8 (kill not honored mid-turn; lossy channel) — CONFIRMED as written.** `_stop` is polled only in the idle loop (`runner.py:283-288`); a kill during a turn or the 900s BLPOP (`worker/engine/approvals.py:194`) burns budget until turn end; SIGKILL (docker stop 5s → SIGKILL, `manager.py:234-235`) bypasses the cascade drain (`runner.py:294-318`); pub/sub kills during reconnect backoff are silently dropped (`worker/control.py:60-63`). One amplification the prior claim missed: after a mid-turn kill, `_run_turn`'s tail **overwrites** the `"stopped"` status with `"idle"`/`"input_required"` and heartbeats it (`runner.py:358-362`) — harmless only because the persister's terminal guard blocks the DB write.
- **Backend #11 (plan/debug never settle — M-47) — CONFIRMED and EXTENDED.** The leak is larger than claimed: `development.py` also never calls `settle_cost` or `finish_thread`, `ask.py:125-137`'s failed branch skips settle *and* key release, and `swarm.py:233-243`'s all-failed branch early-returns before its own settle loop.
- **Backend strengths list ("boot reconciliation interrupts zombie runs incl. QUEUED/PLANNING") — partially corrected.** Including QUEUED is right; including VERIFYING is a bug (finding 5) — it's a parked-human stage, and sweeping it arms a workspace-destroying resume.
- **Runner docstring ("Kill is immediate (process exit)", `runner.py:26-27`) — overclaims.** Kill sets `_stop`; the in-flight turn and any BLPOP run to completion first.

## (c) Slice summary

The lifecycle seam has a well-guarded *run-row* state machine (terminal guards, H-41/H-42, ordered finish/abandon paths all verified real) sitting on top of a nearly unguarded *worker-truth* channel: the backend learns worker state only through a heartbeat persister that freezes any thread which ever waits on an approval, never reads container exit codes, and has no heartbeat-timeout enforcement — so hard deaths hang runs and leak capacity/keys until the next restart, while restarts themselves falsely "reconcile" healthy, parked, and even VERIFYING-stage runs. The two control verbs are asymmetric to the point of inversion: abandon tears down containers, but stop publishes an `interrupt` message the default custom engine never handles, making the product's safest-looking button a DB-only fiction enforced eventually by gateway-key deletion. The flagship fix order: (1) unfreeze `input_required` in the persister, (2) wire `interrupt` into the runner or downgrade stop to kill+stop_container, (3) make reconcile liveness-aware and exclude parked-human stages, (4) add a stale-heartbeat reaper so no terminal state depends on a backend reboot to be discovered.

Now write the full report as the final response.


agent6:

All verification complete. Compiling the audit report.

`_create_card` is payload-shape agnostic (stores the payload dict verbatim, approvals.py:132,149) — tolerates both SDK and engine shapes. VERIFIED-OK.

I now have everything. Let me also double-check the one remaining question on my list: Q3 tar-slip. fetcher.py uses git clone/fetch only — no tarball extraction in my slice (the backend audit's tar-slip claim concerns onboarding/extract elsewhere — repos.py onboarding uses git clone too (repos.py:139-144); no tar in my files). I'll state: no tarball path in the spawn seam; tar-slip claim not applicable to fetcher/manager (verified: only git subprocess in both files).

Also worker trust of payload paths: the engine resolves tool paths against WORKSPACE_DIR (readonly.py:32, mutating.py:34) — env-set, not payload-driven. The worker doesn't receive path payloads from the backend. OK.

Also containers.run is missing a `working_dir` — container WORKDIR /app (worker/Dockerfile:19). WORKSPACE_DIR=/workspace is conveyed via env, engine passes it to tools. Consistent.

Also note: worker image has no HEALTHCHECK and backend doesn't wait for readiness after spawn — _wait_for_heartbeat exists and is used only in the remount/nudge path (run_manager.py:467-489). OK.

One more env check: worker SDK runtime IDLE_TTL default 600 vs engine 900. Backend's finish_thread comment assumes 900s engine default. LOW, noted.

Summary of findings:

**F1 [BLOCKER][CONFIRMED] Persona/knowledge prompt is dropped by the default engine.**
- Backend: thread_env sets PERSONA_PROMPT (manager.py:133), composed with knowledge (thread_manager.py:83-85) and stored in spawn_context (thread_manager.py:109).
- Worker: PERSONA_PROMPT read only in SDK runtime (main.py:57,91-92); EngineRunner never reads it (runner.py:77-126); engine system prompt is worker-baked system_prompt.md (graph.py:91-94). ENGINE defaults custom (config.py:39, worker/Dockerfile:36).
- Breaks: per-run persona, playbooks, knowledge flywheel never reach the model; backend's §6 replay-identical-prompt design terminates at a sealed valve.

**F2 [BLOCKER][CONFIRMED] Resume is inert under the default engine — three dead wires.**
- Backend resume chain: kill_replace passes resume_from_thread_id (run_manager.py:448) → inherits prior session_id (thread_manager.py:94-108) → mounts prior session volume (manager.py:190-192) + sets RESUME_SESSION_ID (manager.py:154-155).
- Worker engine: reads RESUME_CONTEXT_ID (runner.py:87,99) — never set by backend (manager.py:129-169 full env list). Never reads RESUME_SESSION_ID or /root/.claude (grep: no ".claude" in worker). session_id only ever written from an SDK-only "turn complete" event field (bus.py:145-156; no session_id anywhere in worker/worker/engine).
- Breaks: kill-replace / mode-switch / @mention-remount / resume all start a stranger: new thread_id → fresh checkpoint namespace; old session volume mounted but unread. kill_replace docstring claim "now actually true" (run_manager.py:364-369) is false under the default runtime.

**F3 [BLOCKER][CONFIRMED] Session volume mounted at /root/.claude is unused by the default engine; engine state is ephemeral by default.**
- Backend: mounts sessions/<run>/<thread> at /root/.claude (manager.py:190-192); sets DATABASE_URL only if engine_database_url non-empty (manager.py:152-153), default empty (config.py:47).
- Worker engine: checkpoints via Postgres when DATABASE_URL set, else MemorySaver (checkpointer.py:195-206); mirror + episodic DB at CHECKPOINT_MIRROR_DIR default ./checkpoints (runner.py:88,245) = /app/checkpoints under WORKDIR /app (worker/Dockerfile:19) — container-local; container removed by stop_container force (manager.py:235) on finish (thread_manager.py:257-258).
- Breaks: sessions silently ephemeral under default config; "durable session volumes per-lane" (moat §1/§2) are durable-but-empty; harness diagnosis claim "mirror dies with the shredded workspace" is mechanistically wrong — it dies with container removal at /app/checkpoints (correction).

**F4 [HIGH][CONFIRMED] MODE env is a global constant; per-run mode never crosses the seam.**
- Backend sets MODE=settings.engine_default_mode="development" for every thread (manager.py:145; config.py:43); run.mode (ask/plan/development/debug/goal, mode_engine.py:25-30) stored only in spawn_context (thread_manager.py:111).
- Worker engine gates the whole tool surface on MODE (tools/__init__.py:169-201; runner.py:81): ask/plan → read-only set; development → mutating set + spawn tools.
- Breaks: Ask/Plan threads boot with the development tool surface + envelope; read-only intent survives only via ro mounts and the approval gate; the model is told it's in development mode.

**F5 [HIGH][CONFIRMED] Delivery push targets the golden clone, not ADO — stamp's origin is a local path; no remote rewrite anywhere.**
- stamp_clone clones from the golden local path (manager.py:74); no `remote set-url`/`insteadOf` repo-wide (grep: none); golden's own origin = ADO URL with extraHeader auth (repos.py:139-155, not inherited by clones).
- delivery.push_branch pushes `origin` (delivery.py:204-209) → writes agent/* branches into golden; open_pr then asks ADO for a PR from a branch ADO never received (delivery.py:249,231-232).
- Also FLEET_PAT is injected into writable worker containers (manager.py:167) but nothing in the worker consumes it (grep: no consumer; git-credential helper not in worker image — worker/Dockerfile:20-24) — worker-side git push has no credentials (harmless-by-accident since backend pushes, but the env var is a live secret with no consumer).
- Caveat: a host-level gitconfig insteadOf could rescue delivery — none documented in repo.

Hmm — wait. Let me double-check delivery push once more. `git push -u origin agent/...` with origin=/golden/repos/<name> (a non-bare repo with integration_branch checked out). Pushing a NEW branch succeeds. So push_branch "succeeds" (no DeliveryError), then open_pr creates the PR via ADO API — ADO source branch doesn't exist... Actually, would ADO create a PR for a nonexistent source branch? ADO creates the PR and the branch ref is resolved at creation; ADO returns 400 "does not exist" I believe. Either way, delivery is broken or dangling. And golden gets polluted. CONFIRMED from code; the ONLY uncertainty is deployment gitconfig. I'll state that precisely.

Actually — hmm, one more possibility I should check before calling this CONFIRMED: does the backend host's git have a global insteadOf configured via the backend image or compose? The backend Dockerfile (read: lines 1-23) doesn't configure git. compose files? Let me do a quick grep in infra/ for insteadOf/gitconfig to be thorough. Cheap check.

**F6 [HIGH][CONFIRMED] kill_replace drops preserve_workspace → re-stamp destroys the killed thread's uncommitted work; the handoff/checkpoint organs are dead code.**
- run_manager.py:443-449 spawn call omits preserve_workspace → default False (thread_manager.py:77) → stamp_clone(fresh=True) → shutil.rmtree(dest) (manager.py:64-68) of the shared run workspace (keyed workspaces/<run>/<repo>, manager.py:63) — even for goal-mode threads whose spawn_context stored preserve_workspace=True (thread_manager.py:118).
- write_handoff/git_checkpoint (handoff.py:15-51) have zero production callers (grep repo-wide); read_handoff (memory.py:155-160) therefore always returns None.
- Breaks: kill-replace/mode-switch/@mention-remount silently rmtree uncommitted work; the "living artifact" designed to survive this is never written.

**F7 [HIGH][CONFIRMED] Worker-initiated fan-out (spawn_agent/spawn_swarm) never materializes; spawn_done has no publisher.**
- Tools register spawns and return success strings (fanout.py:187-210, 215-236); the only "execution" is arming a 2h watchdog (tools/__init__.py:307-311); no dispatcher exists (graph.py:736 is a title formatter).
- The completion path the runner documents — spawn_done on the control channel (runner.py:438-444) — has no publisher anywhere in the repo; the backend has no spawn API (grep backend/app/api: no spawn route).
- Breaks: the agent is told spawns are running; nothing runs; registry saturates at 8 (fanout.py:48,94-95) for up to 2h per entry, vetoing subsequent fan-out. (Harness audit noted the veto/race side; the seam side — no materialization, no completion feed — is confirmed here.)

**F8 [MEDIUM][CONFIRMED] Pre-heartbeat boot crashes leave the thread "running" forever; no exit-code/log path.**
- Backend marks status="running" immediately after containers.run returns (thread_manager.py:154-167) — container start ≠ worker boot. No container log fetch (no .logs() anywhere) and no exit-status inspection (container_running only, manager.py:239-246).
- Worker: crashes before runner.run()'s first heartbeat (runner.py:244) — import failure at main.py:276, EngineRunner init KeyError/ValueError (runner.py:78-94), Docker-level failures — publish nothing.
- Detection: only the UI watchdog card on stale heartbeat_at (heartbeats.py:1-8 docstring; LET_IT_RUN dismissal runs.py:458-461); no auto-fail. In-run() crashes DO fail promptly via the failed heartbeat (runner.py:289-293 → heartbeats.py:115-116).

**F9 [MEDIUM][CONFIRMED] Playwright MCP not wired for the default engine; stub backend client.**
- Backend stamps .mcp.json into the stamp (manager.py:88-112,204) — a Claude SDK convention, read only by the SDK runtime; the custom engine discovers MCP only via MCP_SERVERS env (mcp.py:176-184), which the backend never sets (manager.py:129-169).
- Even for the SDK: stamp lands at /workspace/<repo>/.mcp.json while the SDK session cwd is /workspace (main.py:87; manager.py:142) — .mcp.json is discovered at the project root (cwd), so the stamp one level down is likely missed (RISK sub-point; SDK discovery semantics not verified in-repo → UNVERIFIED).
- Backend-side PlaywrightMcpClient.capture never captures — captured:False always, no transport (playwright.py:39-49; honest docstring). npx @playwright/mcp@latest also needs egress on an internal-only network (config.py:48-50).
- Breaks: UI-repo Playwright tooling absent under the default engine; screenshot evidence stubbed.

**F10 [MEDIUM][RISK] Image/version skew: mutable tag drift 0.1.0 vs 0.2.0, no digest, no contracts handshake.**
- config.py:36 worker_image default collegium-worker:0.1.0; worker/Dockerfile:4 header documents building 0.2.0; infra/vm/deploy.sh:24 builds 0.1.0; scripts/rb_live_evidence.sh:7,51 builds/sets 0.2.0.
- contracts package baked into both images at build time (backend/Dockerfile:10, worker/Dockerfile:20) from the build context; no runtime version handshake. Drift → ingest ValidationError → dead-letter (bus.py:125-133) while the thread keeps beating "running".
- Breaks: stale image silently runs; or contract drift dead-letters events invisibly at the thread level.

**F11 [LOW][CONFIRMED] control "mode" message is a domain mismatch and dead on both sides.**
- Backend LaneControl.set_mode sends a PERMISSION mode ("acceptEdits", control.py:26-27; test_events_control.py:28) — no production caller (grep).
- Engine runner parses it as an engine Mode (runner.py:447) → ValueError → silently swallowed (runner.py:456-457).
- Dormant today (no caller); if wired, silently no-ops on the engine and works on the SDK.

**F12 [LOW][RISK] Env-size ceiling on TASK_PROMPT/PERSONA_PROMPT.**
- Both ride container env (manager.py:132-133); execve per-string limit (~128KiB). persona_prompt is bounded (~knowledge top-k 8 with 300/600/2000-char caps per backend audit); task is unbounded user input (run_manager.py:113 passes full task; title truncated only for display, run_manager.py:99).
- Oversize → containers.run raises → ThreadSpawnError → thread marked failed (thread_manager.py:150-152). Fails safe; LOW.

**F13 [LOW][CONFIRMED] IDLE_TTL drift between runtimes; backend never sets it.**
- Backend never injects IDLE_TTL_SECONDS (manager.py:129-169); SDK runtime defaults 600 (main.py:65), engine 900 (runner.py:89). Backend's finish_thread docstring banks on "900s engine default" (thread_manager.py:236-241). Consistent for the default engine; the SDK fallback lingers 600s — harmless drift, worth pinning.

Also COLLEGIUM_PERMISSIONS: worker reads it (runner.py:56-68); backend never sets it and has no ruleset storage (grep backend: no COLLEGIUM_PERMISSIONS; no ruleset model found). So capability-map-only is the production posture — dormant feature, not a break. Mention in verified-OK/observations.

Wait — one check on F1: does the engine get persona another way? The persona_prompt content — for engine runs, does the backend stuff persona into TASK_PROMPT? spawn(prompt=prompt...) — prompt comes from the blueprint spec; persona_prompt separate. In spawn_many: spec["prompt"], spec["persona_prompt"] separate. Blueprints build persona_prompt from mode persona + playbooks. The task prompt is the user's task. So no — persona is not folded into TASK_PROMPT. CONFIRMED F1.

Hmm, but let me be extra careful about F1 severity wording: the engine has its own system_prompt.md baked into the image — a single frozen prompt for all personas/modes. The design philosophy §6 (backend) counts on persona composition. The worker audit's system_prompt.md is generic. So threads run with a generic system prompt — a real functional regression vs design, and silently so. BLOCKER stands (it's the spawn contract's core payload being dropped).

Also should I double check whether the engine reads prompts dir from the image (PROMPTS_DIR) — graph.py:85 PROMPTS_DIR. The prompt is baked at image build (COPY worker/worker → /app/worker-project/worker; prompts dir inside package). Fine.

Now — F5 caveat check: infra for insteadOf. Quick grep in infra/ and scripts/ for "insteadOf" already done repo-wide ("set-url|remote add|insteadOf" → No matches). CONFIRMED none. So F5 stands as confirmed-from-code with no documented escape.

Also for F5: is the git-credential-collegium script present for the BACKEND (delivery)? scripts/git-credential-collegium exists (glob found it) and delivery points GIT_CREDENTIAL_HELPER at settings.scripts_dir/git-credential-collegium (delivery.py:154; config.py:89 scripts_dir="./scripts"). Backend Dockerfile: COPY backend /app/backend — scripts/ NOT copied into the backend image! scripts_dir default "./scripts" relative to cwd /app → /app/scripts — absent in the image (Dockerfile copies only packages/contracts and backend). So backend-side git push in the backend CONTAINER lacks the credential helper file → helper invocation fails → push fails anyway. That's an additional seam break for delivery (backend container). Though local-era: backend runs on host (uvicorn) where ./scripts exists. For the k8s/VM deploy, backend runs in the image. So delivery push is broken two ways in the containerized deploy. I'll fold this into F5 as an aggravating note — actually let me quickly verify the credential helper is needed for push: push to origin=local path needs NO credentials — succeeds into golden. So the missing helper doesn't block the (misdirected) push; it would block a correctly-targeted ADO push... which also has http.extraHeader baked only in golden's config, not the stamp's. OK.

Let me also confirm the F3 correction about harness_diagnosis's claim precisely: harness_diagnosis.md line 65: "replay-fallback mirror lives under ./checkpoints — dies with the shredded workspace unless deploy maps it". Correct mechanism: ./checkpoints resolves under the container WORKDIR /app (worker/Dockerfile:19), i.e. NOT inside the workspace mount; it dies with container removal (manager.py:235, called from finish_thread thread_manager.py:257-258 and abandon flows). Conclusion (ephemeral) stands; mechanism wrong. Also the worker audit's §2 "per-lane session volumes by construction" — under the default engine the per-lane volume is mounted but never written, so isolation is vacuously satisfied but durability is absent.

Backend diagnosis #3 spawn-side verification: "wait-for-exit is best-effort; no force-stop; no orphan reaper — manager.py:253-259 times out and only logs; run_manager.py:416-449 then mounts the session volume anyway." CONFIRMED exactly: manager.py:253-259 loop + log.warning only; run_manager.py:416-419 awaits the wait, then spawn at :443 proceeds → run_thread_container mounts session_subpath(run.id, resume_from_thread_id) (manager.py:190-192). Also: no stop_container fallback in kill_replace (only control.kill pub/sub at run_manager.py:408). All confirmed. One nuance to add: under the default engine the shared-write corruption risk is lower (engine doesn't write /root/.claude) but the workspace rmtree race (F6) is the live one.

Env enumeration completeness (Q1) — let me do the final name-for-name table:

Backend injects (manager.py:129-169):
- RUN_ID → worker main.py:55 / runner.py:78 ✓ required, set
- THREAD_ID → main.py:56 / runner.py:79 ✓
- TASK_PROMPT → main.py:58 / runner.py:80 ✓
- PERSONA_PROMPT → main.py:57 (SDK only); engine DROPS (F1)
- PERMISSION_MODE → main.py:59 (SDK only); engine ignores (uses AUTONOMY) ✓ by design
- BUDGET_USD → main.py:60 / runner.py:84 ✓ (both default 5.0 = config default 5.0 config.py:101 ✓)
- REDIS_URL → main.py:61 / runner.py:85 ✓ (worker_redis_url config.py:34 — from-container URL ✓)
- ANTHROPIC_BASE_URL → SDK only (consumed by claude SDK env convention); engine ignores ✓ by design
- ANTHROPIC_AUTH_TOKEN → SDK only ✓
- MODEL → main.py:64 / runner.py:83 ✓ (gateway_model "kimi-foundry" matches worker default)
- WORKSPACE_DIR → main.py:62 / runner.py:86 ✓ ("/workspace" both)
- ENGINE → main.py:264 ✓
- MODE → runner.py:81 — set, but constant "development" (F4)
- AUTONOMY → runner.py:82 ✓ mapped (manager.py:115-120,146)
- LITELLM_BASE_URL → llm.py:292 ✓
- LITELLM_API_KEY → llm.py:293 ✓
- CANARY (conditional) → main.py:270 / runner.py:94 ✓
- DATABASE_URL (conditional, default off) → checkpointer.py:196,206 ✓ (F3)
- RESUME_SESSION_ID (conditional) → main.py:63 SDK only; engine ignores (F2)
- HTTP_PROXY/HTTPS_PROXY/PIP_CACHE_DIR/npm_config_cache/NO_PROXY (conditional) → consumed by tools/os, fine
- FLEET_PAT (writable only) → NO consumer in worker (F5 side note)
- COLLEGIUM_CREDENTIAL_SCOPE=fleet (writable only) → no consumer in worker (the credential helper that would read it isn't in the image)

Worker requires but backend never sets (all have silent defaults):
- RESUME_CONTEXT_ID → runner.py:87, default None (F2)
- CHECKPOINT_MIRROR_DIR → runner.py:88, default ./checkpoints (F3)
- IDLE_TTL_SECONDS → 600/900 drift (F13)
- APPROVAL_TIMEOUT_S → runner.py:90 default 900 = backend approval_timeout_seconds 900 (config.py:98) ✓ consistent
- COLLEGIUM_PERMISSIONS → runner.py:61 default [] (dormant feature)
- MCP_SERVERS → mcp.py:177 default [] (F9)

Good. That's the full name-for-name.

spawn_context (Q2): built thread_manager.py:109-120; stored in Thread.spawn_context JSON column (thread.py:39); NOT delivered to the worker as a blob — replayed backend-side on kill_replace (run_manager.py:388,445-446) which re-injects prompt/persona_prompt via env. Keys: prompt, persona_prompt, resume_session, mode, context_repos, preserve_workspace, resume_from_thread_id. Consumers: run_manager.py:396 (context_repos), :445-446 (prompt, persona_prompt), runs.py:311 (mode), runs.py:360 (context_repos), goal.py:577 (prompt). "resume_session" — no consumer found (grep spawn_context reads). "preserve_workspace" — stored but never read on replay (F6). So spawn_context has dead keys: resume_session (never read), preserve_workspace (never read on replay). Delivery mechanism = DB row, no env-size ceiling issue for the JSON itself; the env ceiling applies to persona_prompt/task on re-injection (F12).

Workspace mount (Q3): fetcher = git only, no tar → tar-slip N/A in my slice (state as verified-N/A). Mount path /workspace/<repo> (manager.py:205,210); worker tools resolve against WORKSPACE_DIR=/workspace (readonly.py:32 etc.); engine seeds "Workspace root: /workspace" (runner.py:131). Consistent. Worker doesn't trust payload paths — paths derive from env, not from spawn payload. OK.

Session volume (Q4): F3.

Image/version (Q5): F10.

handoff.py (Q6): F6 — handed off to no one; receiver read_handoff exists but writer never runs; git_checkpoint likewise dead. Payload schema is fine trivially because unused.

Boot failure (Q7): F8.

Playwright (Q8): F9.

VERIFIED-OK list:
- Control channel name + schema: backend control.py:18-30 ↔ worker control.py:28,49-54 ✓
- Approval request stream + decision key: forwarder.py:46-58 / approvals.py(worker):59-72 ↔ backend approvals.py:62,123-134 + control.py:32-36 ↔ engine/approvals.py:74,194 ✓ (payload-shape agnostic)
- Events stream key + fields + consumer-group normalization: forwarder.py:23,33-37 ↔ bus.py:35,51-57,98 ✓; dead-letter on poison (bus.py:109-115,125-133) ✓
- Heartbeat key/channel + TTL: forwarder.py:25-26,68-72 ↔ heartbeats.py:25,54 + run_manager.py:471-481 ✓
- MODEL/BUDGET/WORKSPACE_DIR/APPROVAL_TIMEOUT defaults match both sides (config.py:32,98,101; runner.py:83-84,90; main.py:60,62) ✓
- AUTONOMY mapping round-trip: autonomy.py:16-24 → manager.py:115-120 → state.py:38-41 ✓
- CANARY wire both sides: manager.py:150-151 → runner.py:94-97 ✓
- Gateway key minted before container start; spawn fails safe on mint failure (thread_manager.py:131-139) ✓
- Workspace mount path consistency: manager.py:205,210 ↔ runner.py:86,131; context repos ro ✓
- Session subpath per-lane isolation: manager.py:46-50,190-191 (per run/thread) ✓ (though unused by engine)
- No tar extraction in spawn path (fetcher.py, manager.py) — tar-slip N/A here ✓
- Container naming collision fixed (manager.py:220-224) ✓
- _create_card payload-agnostic (approvals.py:129-134) ✓

CORRECTED prior claims:
1. Backend diagnosis #3 (wait-for-exit proceeds after timeout) — CONFIRMED exactly as claimed (manager.py:253-259 log-only; run_manager.py:416-419 then :443-449 spawns/mounts). Added: no stop_container fallback in kill_replace; and under the default engine the session-volume corruption risk is theoretical (engine never writes /root/.claude) while the unguarded workspace rmtree is the live loss (F6).
2. Harness diagnosis §1 "replay-fallback mirror lives under ./checkpoints — dies with the shredded workspace unless deploy maps it" — WRONG MECHANISM: ./checkpoints resolves under container WORKDIR /app (worker/Dockerfile:19), not inside the workspace mount; it dies with container removal (manager.py:235 via thread_manager.py:257-258), not workspace shredding (manager.py:261-266 shreds workspaces/<run> on the host). Conclusion (ephemeral) stands.
3. Worker diagnosis §2 "per-lane session volumes by construction" / design doc §2 "durable session volumes… resume and fork survive workspace shredding" — under the default ENGINE=custom the per-lane volume at /root/.claude is mounted but never written by the worker (no ".claude" reference anywhere in worker/); durability is vacuous. The claim holds only for the SDK fallback.
4. run_manager.kill_replace_thread docstring "the replacement resumes where the killed thread left off — now actually true" (run_manager.py:364-369) — FALSE under the default engine (F2: RESUME_CONTEXT_ID never set; RESUME_SESSION_ID unread by engine; session_id never emitted by engine, bus.py:145-156 is SDK-fed).
5. Backend audit §6 "spawn_context enables replay-identical prompts" — the stored persona_prompt is replayed, but the default engine drops PERSONA_PROMPT entirely (F1), so replay fidelity is moot for the default runtime.

Slice summary 3 sentences.

Findings count check — 12 findings. Let me order by severity: BLOCKER: F1 persona drop, F2 resume inert, F3 ephemeral state (maybe fold F3 into F2? No — distinct: F2 is the resume wire; F3 is the durability wire. Keep separate). HIGH: F4 MODE constant, F5 delivery push origin, F6 preserve_workspace/handoff dead, F7 fan-out never materializes, F8 boot-crash invisibility. MEDIUM: F9 playwright, F10 image skew. LOW: F11 mode-message domain mismatch, F12 env size, F13 idle TTL drift.

Severity sanity: F5 — is it really HIGH/BLOCKER? It breaks the delivery path (branch → PR) which is THE survivor of every writable run. But caveat: local-era dogfood may have a workaround; evidence is unambiguous in-repo. I'll call it BLOCKER CONFIRMED (delivery cannot produce an ADO PR from a stamped workspace; golden polluted). Hmm — one more sanity check on F5: `git clone --quiet <golden> <dest>` where golden path is e.g. ./golden/repos/ServerApp. dest=.workspaces/<run>/ServerApp. origin = "./golden/repos/ServerApp"? git resolves to absolute path at clone time. Yes, origin URL = absolute local path. Push origin → writes to golden. Confident.

Wait, one more consideration for F5: could push to a non-bare repo's new branch be blocked by `receive.denyNonFastForwards` or others? New branch creation is fine by default. Confident.

And also for F5, note sync_before_push's `git fetch origin <branch>` + rebase (delivery.py:165-179) works fine against golden (fresh). So the flow fails only at the final push/PR leg — and it fails by polluting golden + ADO PR error. Right.

I should also mention under F5 the FLEET_PAT-in-worker dead secret: env var with no consumer = false backend assumption (Q1 explicitly asks). And COLLEGIUM_CREDENTIAL_SCOPE likewise.

Now — the UpdateCurrentStep and final answer. The output format is strict: findings with severity/status/evidence/what-breaks/fix; then (a) VERIFIED-OK list, (b) CORRECTED prior claims, (c) 3-sentence slice summary. Let me write it.


agent7:

All verifications complete. I have everything needed for the report.

All key verifications done:

1. **Custom engine never writes `/root/.claude`** — zero `.claude` references in worker/. The session volume mount is pure SDK-era. For ENGINE=custom (default), the "durable session volume" is mounted but unused by the engine; the engine's actual durable state (checkpoints) is MemorySaver by default (`engine_database_url: str = ""` config.py:47) or Postgres if configured. So on kill_replace with default config: the replacement mounts a volume the engine doesn't use, with a session_id the engine doesn't read (RESUME_SESSION_ID vs RESUME_CONTEXT_ID), and its checkpoint namespace is fresh. Resume = clean slate. The only "resume" artifact that exists for the custom engine is the DeltaChannel JSONL mirror — which lands at `./checkpoints` relative to WORKDIR `/app` (Dockerfile:19) → `/app/checkpoints` → container ephemeral layer → destroyed by `container.remove(force=True)` (manager.py:235) and NOT mounted (no volume for it in run_thread_container:184-228). CONFIRMED. The worker audit's claim said "dies with the shredded workspace" — CORRECTION: it dies with the container filesystem (rmtree of workspace is irrelevant to it; `container.remove` is the killer). Actually wait — `./checkpoints` relative to WORKDIR /app → /app/checkpoints. Definitely container-local. Even if it had been under /workspace, only /workspace/<repo> is mounted, not /workspace. So: dies with the container, on EVERY container stop (including finish_thread on the NORMAL path). So the mirror is useless in production: written to a filesystem that's removed at every thread end. And the EpisodicMemory SQLite at `mirror_dir/<thread>-episodes.db` (runner.py:245) — same fate. So: episodic memory (the memory.search substrate) is destroyed at every normal thread completion. That's a durability gap: episodic memory never survives a single thread end, despite being a documented survivor class ("knowledge" is a survivor; episodic memory is the memory substrate...). §1: "Nothing the harness needs is allowed to live only in a workspace" — here it lives only in the container layer, worse than the workspace. CONFIRMED.

2. **Emitter**: `_seq` starts at 0 per runner process; docstring says "re-instantiated per turn so seq is monotonic across the thread's lifetime via the state's compacted_event_ids" — hmm, emitter is created ONCE per runner (runner.py:101) not per turn (contradicting its docstring); _seq monotonic per process. On container REPLACEMENT, the new runner's emitter restarts at seq=0 with the SAME thread_id? No — replacement thread has a new thread_id. But RESUME of the same thread (resume_run keeps same run, new thread row). What about container restart of the SAME thread_id? Doesn't happen (new Thread row each spawn). OK — seq collision not an issue across replacements. Within one process fine.

Actually wait — one subtle thing: `EventEmitter` created once in `__init__` (runner.py:101) — the docstring's "re-instantiated per turn" is stale. Not my seam. Skip.

3. No other shred_workspace callers. CONFIRMED shred only on abandon.

Now let me also double-check `stop_run` interaction with capacity: stop_run sets thread.status="stopped" (run_manager.py:232) — slot freed (ACTIVE_STATUSES excludes stopped) — but container alive. The per-repo write lock: capacity.try_acquire(repo_name) — release happens where? Presumably keyed on thread rows with active statuses too, or a separate set. If the lock is released while the old container still writes the workspace... and a NEW run on the same repo stamps a new clone at workspaces/<new_run>/<repo> — different run_id dir, no shared path. OK, workspace collision is only within the same run_id (resume/replace). Session volume collision: sessions/<run_id>/<thread_id> — replacement mounts old thread's subpath (resume_from_thread_id). stop_run+resume_run: last_thread_id = the stopped thread → mount sessions/<run>/<stopped_thread> while its container lives. CONFIRMED concrete.

What writes there concurrently? For ENGINE=custom: nothing (no .claude writes). For ENGINE=sdk: the SDK session transcripts. So the corruption is SDK-era-real, custom-era-latent. BUT there's a second concurrent-write surface that IS custom-era-real: the workspace stamp. resume_run → spawn(preserve_workspace default False) → stamp_clone fresh → rmtree workspaces/<run>/<repo> + re-clone while old container still has it mounted and may be writing (the old worker is still mid-turn or idling — it ignores "interrupt"). Old worker keeps editing files in a deleted tree; its `git` pushes/branches reference a replaced .git; if it pushes after rmtree but before SIGTERM... nothing ever SIGTERMs it on the resume path! It dies only at idle TTL (900s engine / 600s SDK). So for up to 15 min, two containers share one session volume AND the old one holds a deleted-workspace mount. CONFIRMED BLOCKER-grade window for the stop→resume path (no wait at all, not even the best-effort 15s).

4. **`docker stop(timeout=5)` vs worker drain**: worst-case drain on SIGTERM = up to 5s nudge-poll sleep (runner.py:285) + finally cleanup (4 task gathers + cascade-drain XADD + 3 Redis closes + episodic close). SIGKILL at exactly 5s races it. What's lost: cascade-drain (spawn kills not published — spawned threads... wait, what does registry.drain do exactly? For custom engine spawns — the fanout spawns backend threads via spawn_swarm tool? The registry drain at runner.py:306 `self._spawn_registry.drain(self.thread_id)` — from the audit, drain stops every spawn registered under the thread. If SIGKILL preempts, spawns outlive. The spawns are separate containers with own idle TTLs. Not a corruption vector per se (separate session volumes), but a lifecycle leak. Mention as secondary.

Also: does anything reap orphaned containers ever? reconcile_on_boot: no (backend #3 confirmed). `container_running` exists (H-37) but is only used by wait_for_container_exit. So orphaned containers die only at their own idle TTL — IF the worker process is healthy enough to run its watchdog. A wedged worker (deadlocked) lives forever — no reaper, no docker-level timeout. The container has no restart policy specified... `client.containers.run(..., detach=True, remove=False)` — no restart policy → stays stopped if it crashes. OK.

5. **Ingest drain on abandon**: unregister at run_manager.py:273 ~milliseconds after stop_container returns. Ingest poll block=1000ms. Events XADDed during the worker's 5s SIGTERM window (e.g. turn events up to the SIGKILL point) sit unconsumed → stranded forever (no XAUTOCLAIM, no delete, no TTL). The events table — "the PHI-grade system of record" (bus.py:4) — silently misses the tail of every abandoned run. And deadletter stream grows unbounded. CONFIRMED HIGH.

Wait, one more consideration: are there really events in that window? The worker gets SIGTERM (kill message ALSO sent via pub/sub at :266 — if received, the worker sets _stop, exits idle loop at ≤5s, runs finally: cascade-drain event XADDed at runner.py:310-315 then close at 319 — that final XADD races the backend's unregister ~ms later; the ingest poll may or may not have picked it up). If kill message was LOST and the worker is mid-turn: SIGTERM → turn continues → SIGKILL at 5s → events XADDed between the kill and SIGKILL (the mid-turn node's events at graph.py:284/624) → likely unconsumed (they arrived in the last 5s; consumer polls every ≤1s+processing — decent chance they're consumed, but NO GUARANTEE). RACE = sometimes lost. CONFIRMED RISK: no drain gate.

6. Approvals on abandon: approvals.unregister_run (approvals service stops consuming approvals:{run_id}) — the worker's pending BLPOP times out at 900s → deny → fail-closed. But the worker is SIGKILLed at 5s anyway on abandon. On kill_replace: approvals consumer stays registered (run not unregistered); the pending approval row expires at approval_timeout_seconds=900 (config.py:98) — the old worker's BLPOP might still receive a late human decision via RPUSH and act on it (executing the approved tool on the OLD container — which shares the session volume with the replacement!). Ah — this is a nasty one: kill_replace during a pending approval → human clicks "allow" → resolve_approval RPUSHes (control.py:32-36) → OLD worker's BLPOP returns → old worker RESUMES the graph and EXECUTES the approved mutation (runner.py:218 ainvoke(Command(resume=decision))) → writes to the shared session volume / deleted workspace CONCURRENTLY with the replacement. The 15s wait-for-exit doesn't cover this (BLPOP up to 900s). CONFIRMED concrete concurrency bug at the seam: a decision channel to a "replaced" worker is never closed. The old worker only dies when its BLPOP returns (then continues the turn! kill was already consumed? — wait, if control.kill was received, _stop is set but the approval wait isn't interrupted; after BLPOP returns, the graph resumes and the turn RUNS TO COMPLETION despite _stop — `_invoke_with_approvals` never checks _stop. Then the turn ends, turn_boundary XADDed (attributed to a "replaced" thread, ingested into the DB since the run is still registered), status heartbeats (blocked by ACTIVE_STATUSES guard), idle loop sees _stop, exits). So the old worker can run a FULL TURN + mutations concurrently with its replacement for up to ~900s+. This is the sharpest concrete corruption: two agents mutating concurrently, sharing /root/.claude mount (inert for custom engine) — but the mutations land in the OLD workspace tree (deleted) or... the old container's /workspace/<repo> still points at the ORIGINAL inode tree (rmtree'd on host — bind mount keeps it alive for the container!). Actually a bind mount pins the directory; rmtree on the host deletes CONTENTS. The container sees the deletions (same inode tree). So old worker's writes go into a directory whose contents were deleted and re-cloned → both containers write the SAME directory tree (bind mount = same host path!). Wait: the bind mount means the container's /workspace/<repo> IS the host path workspaces/<run>/<repo>. stamp_clone rmtree + re-clone recreates the path. The old container sees the new clone! So the old worker, mid-turn with stale context, starts editing the FRESH clone concurrently with the replacement worker. TWO AGENTS, ONE WORKSPACE, same files. That's the concrete §1 corruption, in the custom-engine era, today: not the session volume (inert) but the WORKSPACE — which both containers have bind-mounted rw at the same host path.

Hmm — but wait: on kill_replace, was the workspace supposed to be preserved? kill_replace doesn't pass preserve_workspace → fresh=True → rmtree+re-clone → "resume" loses all un-committed/un-pushed work — sessions.py:8 acknowledges "un-pushed file changes are lost by design (workspace shredding)". So by design the replacement starts from a fresh clone (prior work must have been pushed). Fine — but the OLD container isn't dead, and it now shares the NEW clone. Double writer. CONFIRMED.

Let me also sanity check `stamp_clone`'s rmtree timing vs the 15s wait: kill → wait 15s → spawn → stamp_clone rmtree. If old container exited cleanly within 15s, no issue. The window needs the old worker alive >15s after kill: mid-turn LLM call (common — turns routinely exceed 15s), approval BLPOP (up to 900s), or lost kill message (pub/sub reconnect gap). All realistic. CONFIRMED HIGH/BLOCKER.

7. One more prior-claim re-verification: worker #4 "at-least-once replay on crash" — graph.py:519-585 tools node sequential; checkpoints at node boundaries (LangGraph); mutating.py:95 non-atomic writes (not my file — but the seam replay requires RESUME_CONTEXT_ID which is never set → in practice the engine NEVER replays mid-turn state across containers; it restarts fresh. So worker #4's replay risk is real in-process (same container restart of the graph — actually ainvoke resume within the same process via _inject_and_run etc.) but across containers the replay machinery is unwired. Correction/nuance to record: at-least-once replay across container replacement is currently MOOT because resume-context wiring is absent (and MemorySaver default); the failure manifests as total state loss instead of double-execution. When RESUME_CONTEXT_ID gets wired + Postgres set, worker #4 becomes live.

Worker #9 (sink failures swallowed, graph.py:1127-1130): CONFIRMED — try/except around `await sink(events)`, log.warning only, no retry/dead-letter; turn proceeds. Also noted: comment claims "surface sink errors to the log instead of swallowing" — they are still swallowed (log-and-continue), just no longer in an un-awaited task. The durable hole stands.

Backend #3 (wait-for-exit best-effort, no force-stop, no orphan reaper): CONFIRMED with sharpened evidence: wait_for_container_exit returns None on timeout (manager.py:248-259, log-only), kill_replace proceeds to spawn (run_manager.py:416-449), no stop_container anywhere in kill_replace, reconcile_on_boot never stops containers (run_manager.py:616-654).

Also the heartbeat persister: on clean worker exit, the final heartbeat pub/sub message must be RECEIVED by the persister to stamp terminal status. If the persister's pubsub dropped the message (no queue — pub/sub fire-and-forget; if persister is in a reconnect... it has NO reconnect; if its loop died earlier, ALL status transitions stop being persisted). Actually wait — the heartbeat TTL key `thread:{id}:heartbeat` also carries status (forwarder.py:69 SET with ex=90). Does anything READ that key for status? `_wait_for_heartbeat` reads it for readiness only (run_manager.py:481 — truthiness, not status). The watchdog... the "frontend watchdog" reads heartbeat_at from DB (heartbeats.py:4-6). So the persistent liveness signal is DB-only via the persister. The TTL key is only a readiness probe. On worker death, key expires in 90s, beats stop, row stale. Nothing marks the thread timed_out (no such writer found). CONFIRMED: a worker that dies without its final heartbeat (SIGKILL mid-turn on abandon — row already handled; or crash) leaves the row "running" until boot reconciliation. Capacity slot leaked until then. The frontend watchdog "false-alarms" per the docstring — cosmetic; the slot leak is the real cost. This is backend-audit-adjacent (slot leak) but at my seam (exit → status propagation). Include as MEDIUM.

Hmm — wait, actually let me double check capacity release on the normal path: finish_thread stamps completed → row not active → slot freed. Failure path: worker exits 1 after status="failed" heartbeat → persister stamps failed (row active → written) → freed. Crash-without-heartbeat path: row stuck running → slot leaked until boot. CONFIRMED.

And one more: the `reconcile_on_boot` DOES free those slots at boot (marks stopped). OK.

8. shred trigger (Q6) authoritative answer: `abandon_run` only, gated on `stop_container` returning (blocking docker stop+remove) — i.e., gated on container death but NOT on ingest drain; the success/completion path never shreds (philosophy violation — workspaces of completed runs persist forever; only abandon shreds). Also session volumes persist until the (unscheduled) 30d purge. README:229-230 admits nothing schedules retention.

9. Also should I check `Relay.publish_step` failure impact on ack? bus.py:177 ack THEN 178 relay publish. If relay publish raises... _process raises after ack → caught by the loop's H-43 handler → dead-letters an ALREADY-ACKED message (xadd deadletter + xack again — harmless double-ack; deadletter gets a spurious entry). Minor, not seam. Skip? It's a tiny robustness wart — ack before relay means relay errors dead-letter already-stored events (noise). Not seam. Skip per "no cosmetic issues."

10. Confirm `IngestConsumer.stop` on backend shutdown: cancel + aclose — no drain. Backend shutdown = unacked pending + unprocessed entries stranded (same boot gap). Covered by the restart finding.

Let me also double-check the `_ensure_group` + `xreadgroup` with `block=None if in_memory() else 1000` — production block=1000ms. Fine.

One thing I haven't verified: does `docker stop` actually SIGTERM the python process as PID 1? CMD ["python", "-m", "worker.main"] — exec form → python is PID 1 → SIGTERM delivered → runner.py:514-515 signal handlers installed on the loop → graceful. Yes.

And `stop_container`'s `container.stop(timeout=5)` + `container.remove(force=True)`: if the container already exited, stop is a no-op-ish, remove works. OK.

Also note manager.py `wait_for_container_exit` polls `container_running` → `client.containers.get(container_id)` raises NotFound (subclass of DockerException?) if removed → returns False. docker.errors.NotFound is a subclass of APIError which subclasses DockerException. Yes, caught → False. OK.

Line-number precision for citations:
- runner.py:88 (mirror_dir default), :245 (episodic under mirror_dir), :254-259 (checkpointer open + MirroredSaver), :283-288 (idle loop _stop poll), :289-293 (failure path), :294-325 (finally: cancel, gather, drain, close), :306-318 (cascade drain + event), :319-321 (closes), :339-344 (turn_boundary publish), :489-491 (idle-watchdog completion), :426-457 (control pump handles kill/nudge/spawn_done/mode — no interrupt), :211 (wait_decision BLPOP), :514-515 (signal handlers), :237-326 (run), :156-158 (event_sink wiring).
- forwarder.py:28-38 (XADD pipeline), :68-72 (heartbeat TTL ex=90), :74-75 (close = aclose only).
- checkpointer.py:135-147 (aput + mirror append best-effort), :180-206 (MemorySaver fallback), :35-56 (DeltaChannel writes mirror_dir/thread.jsonl).
- approvals.py:193-199 (BLPOP 900s timeout deny), :32 (_DEFAULT_TIMEOUT_S=900).
- graph.py:1118-1130 (_publish_events swallow), :284, :624, :585 (call_tool_direct then event), :630 (ToolMessage appended after event), :519-585 (sequential tools loop).
- manager.py:230-237 (stop_container timeout=5 + remove force), :248-259 (wait_for_container_exit log-only timeout), :261-266 (shred rmtree ignore_errors), :53-80 (stamp_clone rmtree at :68), :184-228 (run_thread_container; volumes at :192, :205, :210; remove=False :225), :127-169 (thread_env; DATABASE_URL at :152-153; RESUME_SESSION_ID at :154-155; no CHECKPOINT_MIRROR_DIR, no RESUME_CONTEXT_ID), :46-50 (session_subpath).
- run_manager.py:249-276 (abandon: kill :266, stop_container :268, shred :272, unregister :273), :360-451 (kill_replace: kill :408, wait :416-419, spawn :443-449), :214-247 (stop_run: interrupt :240, no container stop), :116-148 (resume_run: register :140, no wait/stop), :616-654 (reconcile_on_boot no container stop), :467-491 (_wait_for_heartbeat), api/runs.py:389 (caller).
- bus.py:51-61 (register/unregister), :78-96 (loop block=1000), :97-120 (H-43 dead-letter), :122-178 (_process: commit :161, ack :177), no xautoclaim (grep-verified absence).
- thread_manager.py:73-168 (spawn; inherit session_id :94-102; container start :143-149; register :166), :235-270 (finish_thread: stop first :257-258 then stamp :264-267).
- heartbeats.py:52-71 (loop, no reconnect), :84-122 (persist, ACTIVE_STATUSES guard :115).
- semaphores.py:15 (ACTIVE_STATUSES).
- config.py:47 (engine_database_url default ""), :39 (engine_runtime custom), :92-93 (retention settings), :98 (approval_timeout_seconds 900).
- main.py (backend): :57-63 (ingest.start then reconcile_on_boot; no re-register).
- worker/main.py:171-173 (SDK runtime handles interrupt), :260-275 (dispatch default custom), :63 (RESUME_SESSION_ID), :65 (idle TTL 600 sdk).
- Dockerfile:19 (WORKDIR /app), :36 (ENGINE=custom).
- README.md:229-230 (retention unscheduled).

I think I have everything. One more sanity check on a detail: the stop_run path for the SDK runtime — interrupt is handled (main.py:171) → container stays alive (by design) → the resume double-mount applies to BOTH runtimes (custom: interrupt ignored entirely, worse). Good.

Also double-check: kill_replace marks thread "replaced" BEFORE control.kill (run_manager.py:402-405 commit, then :408 kill). If kill delivery fails, row says replaced but container lives — persister won't update (replaced not in ACTIVE_STATUSES — correct, semaphores.py:15) so the zombie is invisible in the DB. And the replacement spawn registers the run stream again (already registered). Events from the zombie old worker (thread_id = replaced thread) still ingested → the replaced thread's event log keeps growing → the audit/replay record shows activity from a dead thread AFTER the replacement started — replay confusion. Include in the kill_replace finding.

Also confirm drain registry on SIGKILL: runner finally never runs → spawn drain skipped → spawned sub-threads not kill-signaled → they run to their own idle TTL. Secondary.

Now write the report. Format:
[SEVERITY] [STATUS] Title
- Worker evidence / Backend evidence
- What breaks
- Minimal fix

Then VERIFIED-OK list, CORRECTED prior claims, 3-sentence summary.

Findings list (ranked):

1. BLOCKER/CONFIRMED — kill_replace proceeds on wait-timeout with no force-stop → two live containers share the same bind-mounted workspace and session volume; old worker can run a full turn concurrently (approval BLPOP up to 900s; mid-turn >15s; lost kill) and even executes late human approvals after being "replaced".
   - Worker: runner.py:211 (BLPOP up to 900s), runner.py:192-219 (_invoke_with_approvals never checks _stop), runner.py:426-435 (kill sets _stop only, honored at idle loop :283), worker/control.py:41-63 (pub/sub reconnect gap).
   - Backend: run_manager.py:408 (lossy pub/sub kill), :416-419 (15s wait), manager.py:248-259 (timeout log-only, returns None), :443-449 spawn proceeds; manager.py:68 (stamp_clone rmtree), :192+205 (same host paths bind-mounted rw in both containers).
   - Concrete shared paths: host workspaces/<run>/<repo> (bind /workspace/<repo> rw both) — old container sees the re-cloned tree and edits it mid-replacement; sessions/<run>/<old_thread> (bind /root/.claude rw both) — inert for ENGINE=custom, live for sdk; Redis approval decision channel approval:<id>:decision stays open (control.py:32-36 RPUSH; worker BLPOP).
   
2. BLOCKER/CONFIRMED — stop_run → resume_run has NO container stop and NO wait at all: interrupt is a no-op on the custom engine; old container lives up to idle TTL (900s) while the replacement mounts the same session volume and re-stamps the workspace.
   - Worker: runner.py:426-457 (no "interrupt" branch) vs backend control.py:20-21 (publishes interrupt); SDK runtime handles it (main.py:171-173) but still keeps the container alive.
   - Backend: run_manager.py:239-240 (interrupt only), :232 (row stamped stopped → slot freed while container alive), :116-148 (resume_run: no wait/no stop), thread_manager.py:143-149 + manager.py:190-192 (mount old session volume), manager.py:68 (rmtree workspace under live mount).

3. HIGH/CONFIRMED — Resume is silently not-a-resume for ENGINE=custom: backend sets RESUME_SESSION_ID (manager.py:154-155) but the engine reads RESUME_CONTEXT_ID (runner.py:87) which nothing sets → context_id = new thread_id (runner.py:99) → fresh checkpoint namespace; combined with engine_database_url default "" (config.py:47 → manager.py:152-153 skips DATABASE_URL → MemorySaver, checkpointer.py:197-205), kill_replace's "resumes where the killed thread left off — now actually true" (run_manager.py:364-369) is false twice over. Pending interrupts/approvals also lost (runner.py:193-206 reload logic never finds them).

4. HIGH/CONFIRMED — Checkpoint mirror + episodic DB die with the container layer on every thread end: CHECKPOINT_MIRROR_DIR never set by the backend (manager.py:127-169), default ./checkpoints (runner.py:88) resolves under WORKDIR /app (Dockerfile:19) with no volume mount (manager.py:184-228) → removed by container.remove(force=True) (manager.py:235) on every finish_thread/abandon. The "PHI-grade replay fallback" (checkpointer.py:1-9) is never durable in production; episodic memory (runner.py:245) never survives a thread.
   - Correction to worker audit #65: "dies with the shredded workspace" → actually dies with the container filesystem (shred is irrelevant to it).

5. HIGH/CONFIRMED — abandon unregisters the event stream without draining; tail events XADDed during the SIGTERM window (or the final cascade-drain event) race the unregister (ms later) and are stranded unacked forever: no XAUTOCLAIM (grep-verified), no stream TTL/delete (grep-verified), deadletter unbounded. The events table (bus.py:4 "PHI-grade system of record") silently misses the end of every abandoned run.
   - Also: pending-but-unacked entries on backend crash are never reclaimed.

6. HIGH/CONFIRMED — Backend restart severs ingest for all in-flight runs AND orphans their containers: run_streams is in-memory (bus.py:48), main.py:57-63 starts ingest then reconcile_on_boot which marks runs INTERRUPTED but never stops containers (run_manager.py:616-654) and never re-registers streams → live workers keep XADDing into streams nobody reads; their session volumes can then be double-mounted by a resume (no wait, finding 2).

7. MEDIUM/CONFIRMED — Workspace shredding happens ONLY on abandon: completed/merged runs never shred (shred_workspace single caller run_manager.py:272; grep-verified repo-wide) contradicting philosophy §1 "workspaces are shredded at run end"; retention sweep purge_expired_sessions + session_store upload/purge + events TTL all unwired (README.md:229-230; session_store only called from tests). Shred on abandon is gated on container stop (good) but stop_container swallows DockerException (manager.py:236-237) → shred can rmtree under a live container on daemon error (ignore_errors=True at :266 hides the fallout).
   - Also abandon doesn't gate on ingest drain (finding 5).

8. MEDIUM/CONFIRMED — docker stop ladder (SIGTERM→5s→SIGKILL, manager.py:234-235) vs worker drain needs: idle-loop wake alone can take 5s (runner.py:285 wait_for timeout=5.0) so the finally block (cancel+gather, cascade drain :306-318, closes :319-321, episodic close :323) races SIGKILL; mid-turn the worker ignores SIGTERM entirely (turn runs to completion; _stop never checked in-turn) so every abandon/finish_thread of a busy thread is a SIGKILL: in-flight node's events lost (graph.py:284/624 awaited inline — never re-published), checkpoint rewinds to last node boundary, executed mutations since then unrecorded (event emitted only after call_tool_direct returns, graph.py:585→624) → replay/audit hole. On finish_thread this is usually idle and safe; on abandon it's silent.
   - Cascade drain skipped on SIGKILL → spawned threads outlive (runner.py:302-306 comment).

9. MEDIUM/CONFIRMED — Worker crash/SIGKILL without final heartbeat leaves Thread row "running" forever (until next boot): nothing marks timed_out (grep-verified no writer); heartbeat TTL key expires in 90s (forwarder.py:69) but nothing reads it for status; the HeartbeatPersister is pub/sub-only (heartbeats.py:52-71) with NO reconnect loop (contrast worker control.py:40-63) — a Redis drop kills the persister task silently and ALL threads' rows freeze. Capacity slots leak for the interval; the zombie is invisible.
   - Seam relevance: "backend believes the run ended cleanly" — actually the inverse: backend never learns about unclean ends.

10. Worker #9 re-verification — CONFIRMED: graph.py:1127-1130 swallows sink errors (log-only, no retry/dead-letter); the turn commits while the durable event drops. Note H-01 made it awaited (ordering fixed) but failure semantics unchanged. Seam impact: Redis blip during the final turn_boundary/cascade-drain publish (runner.py:344, :310-318 guarded the same way) = permanent hole at exactly the run's end. The runner-level publishes (turn_boundary, engine error) aren't even wrapped — they raise... wait: runner.py:344 `await self.forwarder.publish_events([boundary])` — NOT wrapped → raises → _run_turn raises → run()'s except → status failed. Hmm interesting: a Redis blip on the turn_boundary publish fails the whole turn (status failed) — inconsistent with graph-level swallow. Either way the event is lost. On the failure path: _emit_engine_error (507) also unwrapped — if THAT publish fails too (Redis still down), the exception propagates from the except block → finally → process exit 1? Let me trace: run() except block (289-293): heartbeat (could raise? heartbeat publish at :291 unwrapped → if Redis down, raises → finally runs (gather etc.) → exception propagates out of run() → main() → process exits nonzero. Events lost. OK — mention that the "status: failed" heartbeat/event are themselves best-effort against a dead Redis; backend learns nothing except silence → finding 9 territory.

11. LOW/CONFIRMED — Ingest run_streams never sheds completed runs: unregister only on abandon (run_manager.py:273); every completed run's stream stays in the xreadgroup set forever (bus.py:80) and streams themselves are never trimmed/deleted → unbounded Redis memory + N dead streams per poll. Operational, not correctness.

Also — verified OK list:
- Ack-after-commit + poison-pill dead-letter: bus.py:135-177 (commit :161 before xack :177; validation dead-letter + ack :127-132).
- finish_thread ordering: stop container THEN stamp terminal (thread_manager.py:257-267) — and the persister's ACTIVE_STATUSES guard (heartbeats.py:115) prevents resurrection; pinned by test (test_heartbeats.py:166-171).
- Events flushed inline per node (no fire-and-forget): graph.py:1118-1130 (H-01 awaited), call sites 284/624; turn_boundary before idle heartbeat (runner.py:344→362).
- Graceful SIGTERM path flushes everything on the happy path: signal handlers (runner.py:514-515) → idle-loop exit → finally closes (294-325); final status heartbeats on each terminal path (:291, :432, :489-490).
- kill_replace terminal-guard: refuses already-terminal threads (run_manager.py:385-387).
- stop_run terminal guard H-41 (:221-222), abandon H-41 (:258-259).
- Session unpack path-traversal guard (session_store.py:59-74, C-12 fixed properly).
- Approvals fail closed on timeout/error (approvals.py:193-208) and the approval row expiry clock matches (config.py:98).
- wait_for_container_exit correctly treats NotFound as exited (manager.py:239-246 via DockerException catch).
- _wait_for_heartbeat before nudge after replace (api/runs.py:386-389) — nudge-into-booting-worker covered.

CORRECTED prior claims:
- Backend #3 "wait-for-exit best-effort": CONFIRMED and sharpened — wait_for_container_exit returns None on timeout with only a log (manager.py:258-259); kill_replace has no force-stop fallback; but also found worse: stop_run/resume_run have NO wait at all, and the interrupt they rely on is a no-op on the custom engine.
- Worker #9 (sink failures swallowed): CONFIRMED at graph.py:1127-1130; H-01 fixed ordering/loss-on-shutdown via inline await but failure semantics remain log-and-continue.
- Worker #4 (at-least-once replay): NUANCED — the replay machinery across containers is currently inert because RESUME_CONTEXT_ID is never set and MemorySaver is the default; the dominant failure today is total state loss on replacement, not double-execution. In-process resume paths (nudge re-enter) are unaffected. If the resume wiring is fixed, #4's replay-of-non-idempotent-tools becomes live.
- Worker audit §1 "replay-fallback mirror lives under ./checkpoints — dies with the shredded workspace unless deploy maps it": CORRECTED — it dies with the container's ephemeral layer at WORKDIR /app (Dockerfile:19) on every container.remove (manager.py:235), including the normal finish_thread path; workspace shredding never touches it. "unless deploy maps it" — the deploy never maps it (manager.py:127-169, 184-228).
- harness_diagnosis #8 "kill channel lossy": CONFIRMED both ends (backend publish no ack, control.py:29-30; worker reconnect gap control.py:40-63).

Hmm — finding count is high. The instructions allow zero findings but I've verified each. Let me consolidate to avoid padding: merge 11 into 5 (stream lifecycle), merge 10 into the re-verification section + finding 8 mention. Keep:

1. BLOCKER kill_replace double-mount/concurrent-writer window (15s vs 900s, no force-stop, late-approval execution)
2. BLOCKER stop→resume: no stop, no wait, interrupt is a no-op on the default engine
3. HIGH resume is not a resume (RESUME_CONTEXT_ID unset + MemorySaver default) — kill_replace docstring false
4. HIGH mirror+episodic die with container layer (correction of worker claim)
5. HIGH abandon unregisters stream without drain; no XAUTOCLAIM/TTL; events table misses run tails
6. HIGH boot reconcile orphans containers + severs ingest (no re-register)
7. MEDIUM shred only on abandon; retention/session-store unwired; stop_container exception → shred under live container
8. MEDIUM docker-stop 5s ladder vs drain needs; SIGKILL mid-turn event/mutation hole on abandon; cascade drain skipped
9. MEDIUM crash-without-heartbeat → row stuck running; HeartbeatPersister has no reconnect

Plus VERIFIED-OK and CORRECTED.

Severity calibration per the philosophy: §1 moat violations are the worst class. Findings 1-2 are direct §1 corruption windows → BLOCKER. 3-4 are durability lies → HIGH. 5-6 silent event loss → HIGH (§1: events are survivors). 7-9 MEDIUM.

Update step, then write the final report.


agent8:

One last firsthand check — the encrypted-BYO-PAT contrast for the plaintext-key finding.

- `run.tokens` is never assigned anywhere in backend/app (grep found only unrelated knowledge.py locals). So the API's `"tokens": run.tokens` (api/runs.py:86) always serves the column default. Not my seam per se, but confirms the worker's usage report is never folded into anything. Settlement is purely gateway-side.
- Container start logs only thread_id + container short id (manager.py:227) — env (with the key) is NOT logged. Good.
- `wait_for_container_exit` (248-259): 15s poll, then only logs — matches backend audit #3. Not my seam except that kill_replace proceeds without key release.

I now have everything I need. Let me also double-check the worker's behavior on gateway 429 one more time for question 6, and verify `with_gateway_retry` (sync version) usage — where is it called? graph.py imports both. The sync one is used in tools? Let me quickly grep for with_gateway_retry usage in worker. Not critical — the stream path (with_gateway_retry_aiter, max_retries=2 at graph.py:245-246) is the main LLM path.

Also for question 3, let me nail down what the worker does on a mid-turn 402/429-after-retries:
- Stream-start 429/503: retried 2× (graph.py:245-246), then GatewayRetryError → caught at graph.py:263 → not context-overflow → `{"error": err, "done": True}` → turn fails.
- Mid-stream failure: NOT retried (llm.py:207-210 comment) → same error path.
- 402/400 (budget): not retryable → immediate error → turn fails.
- First turn failure → runner.py:354-357 raises → run() 289-293 → status "failed", engine error event, exit 1 → container exits → backend heartbeat persists "failed" → blueprint _await_thread returns on "failed". For ask: run FAILED (ask.py:124-137), no settle, no key release. So a budget-exhausted first turn = failed run + leaked key + no cost readback. The gateway key is already at cap so further spend on it is blocked by the gateway (key's max_budget is the hard cap) — the leaked key in THIS case is exhausted, so spend risk is nil, but the key row lingers. For other failure causes (LLM 500 storms etc.), the leaked key may still hold budget.

- Nudge-turn failure → runner.py:388-399 → turn fails, thread stays alive, status "idle". Subsequent nudges retry → same failure loop until idle TTL. Burns no budget if the key is exhausted (gateway rejects pre-call), but each attempt is a failed turn. If the failure was transient 429s, next nudge may succeed. OK.

One more divergence nuance for question 3: the worker's `budget.used` persists in the CHECKPOINT (graph.py:309 writes dict into state; checkpointer persists). On kill_replace, a NEW thread spawns with a fresh Budget (runner.py:84: used=0) and fresh gateway key — but the CONVERSATION resumes. So budget accounting resets per thread, not per conversation. The envelope shows $0.00 used after a replace even though the conversation already spent $X on the prior key. Backend run.cost_usd aggregates per-thread settlements (when they happen). So per-conversation budget continuity breaks across kill_replace/remount: the replacement gets a fresh $5 key and a fresh $0 envelope — a conversation can spend N×budget through N replacements. That's a real seam gap: budget is per-thread-key, and replacement mints a new full-budget key with no carryover. CONFIRMED by code: thread_manager.py:104-107 (new Thread with budget_usd=default) + 132-133 (mint with max_budget=thread.budget_usd); run_manager.py:443-449 (spawn on replace). No spend carryover. The old key's spend is also never settled on the replace path (no settle_cost in kill_replace). So: replacement = budget reset. Worth flagging as RISK (budget multiplication via replacements) — mode switches and @mention remounts are user-triggerable.

Wait — is there a cap on replacements? Not that I've seen. Each replacement = new $5 key. A user (or a loop of @mention expansions) can multiply spend. The gateway master account caps total, but per-thread budgets are bypassed by replacement. Flag it.

Also the SDK backstop path (main.py:88): `max_budget_usd` for the legacy SDK runtime — only for ENGINE=sdk. The custom engine has NO hard self-cap (would_exceed uncalled). Gateway key is the only hard cap. OK by design ("gateway per-key budget is authoritative").

Now — severity ranking for findings:

1. **Container-start-failure key leak (commit-ordering defeats H-36)** — HIGH, CONFIRMED. Minted key never committed to DB before container start; failure path's release reads NULL from DB → key leaks untracked. Also process-crash window between mint and commit.

2. **Key release missing on most terminal paths** — BLOCKER/HIGH, CONFIRMED. Release happens only on: goal-blueprint finish_thread (active threads only), stop_run/stop_thread (active threads only), boot reconcile (active-stage runs), spawn failure (broken per #1). Missing on: abandon_run (all threads), kill_replace/remount (old thread), worker-side failure (status failed — excluded from stop_run's filter and finish_thread's early-return), natural idle-TTL completion on non-goal blueprints (ask/swarm/plan/debug/development never call finish_thread; heartbeat stamps completed; nothing releases). Gateway keys have no expiry (mint_key sets no duration — litellm.py:44-48). Backend audit §5's "keys released on every terminal path" (backend_diagnosis.md:103) is FALSE. This also makes the plaintext DB column (thread.py:35) a permanent live-credential store.

3. **settle_cost coverage gaps beyond #11** — HIGH, CONFIRMED. settle only in swarm/ask/goal success paths. plan/debug/development never settle (confirms + extends backend #11 — development was not named in #11). No settle on stop/abandon/failed paths → stopped/failed runs underreport cost (thread.cost_usd stays 0; run.cost_usd recomputed only from settled threads). Also swarm all-failed path returns BEFORE settle (swarm.py:233-243 vs 258-268) → failed swarm cost never read back.

4. **Release-before-settle ordering hazard in goal ship** — MEDIUM, RISK/UNVERIFIED gateway behavior. finish_thread releases fixer keys (goal.py:481 → thread_manager.py:270); _ship later settles all thread_ids (goal.py:451-452) → key_spend on deleted keys → LiteLLM /key/info on a deleted key likely 404s → raise_for_status raises (litellm.py:61) → _ship raises AFTER run transitioned COMPLETED (goal.py:431). UNVERIFIED how LiteLLM responds for deleted keys; if it still reports spend, no issue. Actually wait — settle_cost swallows nothing; read_spend_reconciled → key_spend → raise_for_status on 4xx → propagates. Hmm, but also note settle_cost is called for thread_ids that may include threads whose keys were released. RISK label.

5. **Budget double-entry divergence** — MEDIUM, CONFIRMED (the math) + design-intended (soft vs hard). Worker estimate (llm.py:269-276) bills all input at $2.00/1M, output $6.00/1M for kimi-foundry; gateway bills $0.60/$2.50 + cache-read $0.15 (config.yaml:30-32). Worker overstates ~3.3× input / 2.4× output → 50/80% reminders fire ~3× early; envelope shows exhaustion while 60-70% of the gateway budget remains. Reverse risk: both tables are placeholders (config.yaml:29 "CONFIRM against the Foundry billing page"); if real Foundry pricing exceeds the gateway yaml, the key hard-caps while the worker shows headroom → mid-turn 4xx. Worker treats budget-exhaustion 4xx as non-retryable (llm.py:217-225) → turn fails (graph.py:263-272); first-turn failure kills the thread (runner.py:354-357, 289-293); nudge-turn failure keeps a zombie thread that fails every subsequent nudge until idle TTL (runner.py:388-399). No budget-exhausted signal crosses to the backend as anything but generic "failed". Confirms worker audit's cached-token claim (§5 detail) — estimate_cost reads only input_tokens/output_tokens, no cache fields.

6. **Budget reset across kill_replace/remount** — MEDIUM, CONFIRMED. Replacement thread = new key with full default budget (thread_manager.py:107, 132-133; run_manager.py:443-449); worker Budget resets to 0 (runner.py:84); no spend carryover or settlement of the old key. N replacements = N× budget for one conversation.

7. **"Queued verdict" claim mis-scoped** — CORRECTION. The durable "queued" verdict (backend_diagnosis.md:104) is triggers.py:369-370 (ADO webhook trigger rate limiting), not the LLM gateway seam. Gateway 429s are handled worker-side only: retry 2× on stream start with Retry-After respect (llm.py:164-214), then GatewayRetryError fails the turn. No backend→worker queued signal exists for LLM rate limits; the worker neither honors nor violates it — it doesn't exist at this seam.

8. **Key storage/exposure** — plaintext at rest CONFIRMED (thread.py:35, alembic 0e64fa6df16b:305); NOT exposed via API serializers (api/runs.py:81-89, 173-181 — no gateway_key/gateway_key_alias fields); NOT echoed by the worker into events/deltas (worker grep: LITELLM_API_KEY read only in llm.py:293; deltas carry content/reasoning only, graph.py:252-257); NOT logged by sandbox manager (manager.py:227 logs thread_id/container only). But release_key never clears the column (thread_manager.py:222-233) → DB accumulates every key, and per finding 2 most stay live. So backend #12 is confirmed and made worse by the seam: the plaintext column isn't just at-rest hygiene, it's a live-credential graveyard because release coverage is poor. Also: anyone with DB read (backups, replicas, alembic dumps) holds live spend-capable credentials.

9. **Cross-thread key mixup** — VERIFIED-OK. Per-container env from the thread row (manager.py:127-169, 212-216); one container per thread (manager.py:214-228); spawn always mints fresh (thread_manager.py:132); prewarm pool documented-not-implemented (thread_manager.py:45-59); no container reuse. Worker reads env once per make_llm call (llm.py:292-293). No path where thread A's container gets thread B's key.

10. **Worker final usage report** — not factored into settlement: turn_boundary carries usage (runner.py:339-344) but backend events layer has no usage/cost handling (grep: no matches in app/events); run.tokens never written (no assignment in backend/app). Settlement purely gateway-side (thread_manager.py:211). VERIFIED as designed, with the caveat that worker-reported usage is display-only.

11. **Crash window (question 7)** — worker dies mid-turn after N paid calls: no heartbeat-based reaper exists (heartbeats.py only persists; main.py:57-63 starts no reaper). The key stays live until: user stops the run (stop_run releases only running/idle/queued threads — a crashed thread whose row still says "running" IS covered if the user stops), or backend reboot (reconcile_on_boot covers active-stage runs, releases running/idle/queued threads — run_manager.py:637-651). If the worker managed a final "failed" heartbeat before dying (runner.py:290-291 does heartbeat failed before exit on exception), the row flips to "failed" → then stop_run SKIPS it (run_manager.py:226 filter) and reconcile SKIPS it (637 filter: running/idle/queued only) → key NEVER released. So the crash window for a failed-heartbeat thread is forever. For a SIGKILLed worker (no final beat), row stays "running" → covered by stop/reconcile. Ironic inversion: the cleaner the failure signal, the more permanent the key leak. CONFIRMED.

12. **release_key failure handling** — best-effort log-only (thread_manager.py:229-233); on gateway-down release failure, key stays live, no retry, no reaper. Combined with no expiry → permanent. RISK (depends on gateway availability at thread end).

Also minor: read_spend_reconciled takes ~8s per thread (5s grace + 3 polls × 1s) — swarm with N threads settles serially → N×8s at run end. Cosmetic-ish/perf; the prompt says no cosmetic issues. Skip? It's a latency note at the seam; I'll skip per "no cosmetic".

Also: `settle_cost` recomputes run.cost_usd as sum over threads (thread_manager.py:214-216) — transient undercount while other threads unsettled, and permanent undercount for never-settled threads. Covered in finding 3.

Worker #5/#6 re-verification: the harness_diagnosis §5 detail claim "Cost math ignores cached-token discounts (estimate_cost bills all input at full price)" — CONFIRMED at llm.py:269-276. "budget visible in-loop, flagged not enforced" — CONFIRMED: envelope graph.py:116-128; reminders 324-343; would_exceed defined state.py:51-52 but called only in tests (test_engine_contract.py:274-277) — never in production. The top-finding #6 (dump default) is not my seam. Worker #5 (sequential tool loop) is not my seam. The prompt said "worker #5/#6 cost-math and budget claims" — the cost-math claim is in the §5 moat detail; I'll confirm it.

Backend #11 (plan/debug settle leak) — CONFIRMED and EXTENDED: development.py also never settles (spawns at development.py:124,155; no settle anywhere); and no terminal-path settle (stop/abandon/failed/reconcile). Backend #12 (plaintext key) — CONFIRMED, extended: column never cleared after release; combined with missing releases, DB holds live keys indefinitely. API/WS do not expose it.

Backend §5 claim "keys released on every terminal path" (backend_diagnosis.md:103) — CORRECTED: false per evidence above.

Backend §5 claim "rate-limit overflow becomes a durable 'queued' verdict" — CORRECTED: that's trigger-ingest rate limiting (triggers.py:369-370), unrelated to the LLM gateway seam; no queued signal crosses to the worker.

Severity assignments:
- Key release missing on most terminal paths (incl. abandon, kill_replace, worker-failed, idle-TTL on non-goal blueprints): BLOCKER (§5 moat: cost enforcement depends on key lifecycle; leaked live keys with budget = unbounded spend surface + the audit's own §5 claim is false). Actually is it BLOCKER or HIGH? The keys are capped per-key ($5 default), scoped to one model, and the gateway master still meters. Blast radius per leaked key = its remaining budget (≤$5). But the count grows monotonically — every ask/plan/debug/development thread leaks one. Over 1000s of threads, thousands of live keys each with up to $5 spendable by anyone who obtains them (DB read, gateway API with master key, container inspect while alive). For a cost-moat product, systematic key leakage on the dominant lifecycle path is a moat failure. I'll call it BLOCKER for the moat (§5 enforcement mechanism absent on most paths), with the note that per-key blast radius is capped.
- Container-start-failure mint leak (commit ordering): HIGH CONFIRMED.
- Settle coverage gaps: HIGH CONFIRMED.
- Budget reset on replace: MEDIUM CONFIRMED.
- Divergence 3.3×: MEDIUM CONFIRMED.
- Release-before-settle ordering in goal ship: MEDIUM RISK (gateway behavior UNVERIFIED).
- Plaintext key column never cleared: MEDIUM CONFIRMED (exposure limited to DB readers; not in API/WS/logs).
- release_key best-effort no-retry: LOW RISK.

Let me also double check the finish_thread early-return leak once more for ask: ask.py settles (148) but never finish_thread → the ask thread lingers 900s → idle watchdog completes it (runner.py:483-492) → heartbeat "completed" → persister flips row (heartbeats.py:115-116, "idle" ∈ ACTIVE_STATUSES) → no release anywhere → leak. CONFIRMED for ask. Same for swarm explorers/plan/debug/development threads. goal is the ONLY blueprint calling finish_thread.

Hmm — wait, one more consideration for ask: does the ask blueprint's run end trigger anything else? The run goes COMPLETED; thread lingers; nothing. Yes.

And actually, let me double-check the stop_run filter once more (run_manager.py:225-233): `for l in threads: if l.status in ("running", "idle", "queued")` — collect + mark stopped + release at 239-243. A "failed" thread (budget-exhausted first turn) is excluded → its key is not released even when the user explicitly stops the run. And abandon releases NOTHING. Confirmed.

Also verify reconcile_on_boot's thread filter once more: run_manager.py:637-641: `if t.status in ("running", "idle", "queued")` — failed/replaced excluded. Confirmed.

One more check for completeness on question 1: "backend mints BEFORE container start" — CONFIRMED (thread_manager.py:132 mint, :143 container start). "delivered via env" — CONFIRMED (manager.py:147-148). "worker uses it in llm.py" — CONFIRMED (llm.py:292-305, ChatOpenAI api_key). Auth header: ChatOpenAI sends Authorization: Bearer <key> to base_url. Base URL = worker_gateway_url (http://gateway:4000) — note: LITELLM_BASE_URL is set to `http://gateway:4000` WITHOUT /v1 (config.py:35; manager.py:147). ChatOpenAI appends /chat/completions to base_url... langchain's ChatOpenAI default base is https://api.openai.com/v1; if you pass base_url without /v1 it posts to http://gateway:4000/chat/completions. Does LiteLLM proxy serve /chat/completions (no /v1)? LiteLLM proxy serves both /v1/chat/completions and /chat/completions (it has routes for both). The spike README says LITELLM_BASE_URL=http://gateway:4000/v1 (worker/spike/README.md:36) — WITH /v1. But the backend injects worker_gateway_url default "http://gateway:4000" WITHOUT /v1 (config.py:35). Discrepancy! If LiteLLM only served /v1/*, the worker would 404 every call. LiteLLM proxy does register /chat/completions at root too (it has both). I can't verify the deployed LiteLLM version's routes from this repo. The test patches LITELLM_BASE_URL="http://gw" (no /v1) and only checks construction. So: UNVERIFIED whether the missing /v1 suffix breaks calls against the deployed gateway; flag as a low-risk config inconsistency between spike docs (with /v1) and backend default (without). Actually — llm.py's make_llm passes base_url to ChatOpenAI verbatim. Modern langchain-openai: openai_api_base default "https://api.openai.com/v1"; if you pass "http://gateway:4000", requests go to "http://gateway:4000/chat/completions". LiteLLM proxy DOES expose OpenAI-compatible routes at both / and /v1 (it has `@router.post("/v1/chat/completions")` and `@router.post("/chat/completions")` historically). I'll flag as UNVERIFIED/LOW — a doc-vs-config inconsistency worth one line, since if the gateway ever drops root routes every thread dies at once. Hmm, the prompt says no cosmetic issues — this is borderline functional (deploy-breaking if true). I'll include it as LOW/UNVERIFIED.

Also the SDK path: ANTHROPIC_BASE_URL=http://gateway:4000 (manager.py:137) for the legacy runtime — same note.

Now, one more possible check: `worker_gateway_url` vs `gateway_url` — backend talks to gateway via gateway_url (localhost:4000 default), workers via worker_gateway_url (gateway:4000). Both inject the same master-key-minted virtual key. Fine.

I think evidence gathering is complete. Let me also quickly double-check that there's no `duration` or expiry param in mint_key — litellm.py:44-48: json={"key_alias", "max_budget", "models"} — no duration/expires. Confirmed: keys never expire gateway-side.

Also confirm models scoping: mint_key models defaults to [gateway_model] (litellm.py:47) → key scoped to kimi-foundry only. So a leaked key can only spend on kimi-foundry, capped at remaining max_budget. Blast radius bounded per key.

Total spend exposure: N_leaked_keys × remaining budget. With default $5 and most threads using <$1, each leaked key holds ~$4+ spendable. And the DB holds them all in plaintext forever.

Now write the report. Format per prompt:
[SEVERITY] [STATUS] Title
- Worker evidence / Backend evidence
- What breaks at the seam
- Minimal fix direction

Then (a) VERIFIED-OK list, (b) CORRECTED prior claims, (c) 3-sentence slice summary.

Let me make sure every finding cites file:line on both sides where applicable (some findings are backend-only or worker-only by nature — the prompt says cite both sides that I personally read; for one-sided findings cite the one side plus the relevant seam counterparty).

Findings list:

1. [BLOCKER] [CONFIRMED] Gateway keys are released on only a minority of terminal paths — most threads leak a live, budgeted key permanently
   - Worker evidence: worker self-completes on idle TTL (runner.py:483-492) and heartbeats terminal status (runner.py:290-291, 489-490); the worker has no key-release role (it only holds the key in env, llm.py:292-293).
   - Backend evidence: release_key exists (thread_manager.py:222-233) but is called only from finish_thread (thread_manager.py:270 — goal blueprint only: goal.py:228,243,264,295,310,383,481), _mark on spawn failure (285-286), stop_run (run_manager.py:243), stop_thread (332), reconcile_on_boot (651). Missing from: abandon_run (run_manager.py:249-276), kill_replace_thread (360-451), and every worker-reported terminal state — heartbeats.py:115-116 stamps "completed"/"failed" without any release hook, and stop_run/reconcile filters exclude "failed"/"replaced" threads (run_manager.py:226, 638). mint_key sets no expiry (litellm.py:44-48).
   - Seam break: every ask/swarm/plan/debug/development thread that ends by idle-TTL, every failed thread, every replaced thread, and every abandoned run leaves a live key with remaining budget at the gateway forever; the plaintext copy stays in threads.gateway_key (thread.py:35) because release_key never clears the column.
   - Fix: make terminal-state transition single-point (route heartbeat-persisted terminal statuses and all blueprint end paths through one `terminate_thread` that settles + releases + clears the column); set a `duration` at mint as a gateway-side backstop.

2. [HIGH] [CONFIRMED] Container-start failure leaks the just-minted key — H-36 release defeated by commit ordering
   - Backend: mint sets the key on the in-memory ORM object (thread_manager.py:132-136) but the row is committed without it at :124-125 and re-committed with it only after container start (:154-162). On container-start failure, `_mark(failed)` (:150-152) → release_key (:285-286, :222-228) reads a FRESH session row where gateway_key is still NULL → delete_key never called.
   - Worker: n/a (container never starts).
   - Seam break: a key with full budget is minted, never recorded anywhere, never deleted — invisible to any future cleanup that scans the DB.
   - Fix: commit the key to the row immediately after mint (before container start), or pass the in-memory key to the failure path.

3. [HIGH] [CONFIRMED] settle_cost coverage is success-path-only — stopped/failed/abandoned runs and plan/debug/development blueprints never fold spend into cost_usd
   - Backend: settle_cost (thread_manager.py:203-220) called only from swarm.py:258-268, ask.py:147-148, goal.py:451-452. plan.py spawns planner+critic (plan.py:171-176) and debug.py spawns diagnoser+fixer (debug.py:152-158) with no settle (confirms #11); development.py spawns at :124,:155 with no settle (extends #11). stop_run (run_manager.py:214-247), stop_thread (317-332), abandon_run (249-276), kill_replace (360-451), reconcile (616-654) never settle. swarm all-failed returns before settling (swarm.py:233-243 vs 258).
   - Worker: turn_boundary reports usage (runner.py:339-344) but nothing backend-side consumes it for cost (no usage/cost handling in app/events; run.tokens never written — no assignment exists in backend/app).
   - Seam break: thread.cost_usd stays 0.0 and run.cost_usd underreports on exactly the runs where cost control matters (killed for overrun, failed mid-spend); spend is only visible at the gateway, keyed by a key that (per finding 1) is usually still live.
   - Fix: settle inside the same single-point terminate path as release; settle-before-release ordering.

4. [MEDIUM] [CONFIRMED] Budget double-entry diverges ~3× and can never converge mid-run
   - Worker: estimate_cost bills input_tokens+output_tokens at $2.00/$6.00 per 1M for kimi-foundry (llm.py:262-276), no cached-token fields; envelope shows this estimate as $used/$cap (graph.py:116-128, 300-312); would_exceed exists but is never called in production (state.py:51-52; only test_engine_contract.py:274-277).
   - Backend: gateway bills kimi-foundry at $0.60/$2.50 per 1M with cache_read $0.15 (infra/litellm/config.yaml:30-32); BUDGET_USD env and key max_budget come from the same value (manager.py:135, thread_manager.py:107,133) so caps agree — the used-signals don't.
   - Seam break: worker shows ~3.3× the gateway-metered spend → 50/80% reminders (graph.py:324-343) fire at ~15%/24% of real budget; the "felt budget" is alarmist and untrustworthy. Reverse direction is worse: both price tables are placeholders (config.yaml:29), so if real pricing exceeds the gateway's, the key hard-caps while the worker shows headroom → non-retryable 4xx (llm.py:217-225) → turn fails (graph.py:263-272); first-turn failure kills the thread (runner.py:354-357, 289-293), nudge-turn failure leaves a zombie that fails every nudge until idle TTL (runner.py:388-399).
   - Fix: single source of pricing truth (worker reads pricing from the gateway's /model/info or backend injects it via env); map budget-exceeded 4xx to a distinct terminal signal, not generic failure.

5. [MEDIUM] [CONFIRMED] kill_replace/remount resets the budget: new full-budget key, worker envelope back to $0, old key's spend unsettled
   - Backend: kill_replace spawns a fresh thread (run_manager.py:443-449) → spawn mints a new key with full default budget (thread_manager.py:107, 132-133); no settle of the old key anywhere in 360-451.
   - Worker: replacement container boots a fresh EngineRunner with Budget(used=0) (runner.py:84, 143).
   - Seam break: one conversation can spend N×budget through N replacements (mode switches, @mention remounts — api/runs.py:383,444); per-thread budget is not per-conversation.
   - Fix: carry prior-thread settled spend into the replacement's mint budget (max_budget = budget − prior_spend) and seed BUDGET_USD used accordingly.

6. [MEDIUM] [RISK — gateway behavior UNVERIFIED] goal ship settles cost AFTER keys are released → key_spend on deleted keys may raise, post-COMPLETED
   - Backend: fixer threads finish_thread → release_key (goal.py:481 → thread_manager.py:270, 222-233); _ship then settles all thread_ids (goal.py:451-452) → read_spend_reconciled → key_spend GET /key/info (litellm.py:58-62) with raise_for_status on a deleted key.
   - Seam break: if the gateway 404s spend for deleted keys, _ship raises after the run already transitioned COMPLETED (goal.py:431) — cost lost, exception after terminal. If LiteLLM retains spend rows for deleted keys, harmless. UNVERIFIED which.
   - Fix: settle-before-release everywhere (one ordering), or make settle_cost tolerate missing keys (catch, keep last-known).

7. [MEDIUM] [CONFIRMED] Plaintext gateway_key column is never cleared — DB becomes a permanent live-credential store
   - Backend: thread.py:35 plaintext String; release_key deletes at the gateway but never nulls the column (thread_manager.py:222-233); combined with finding 1, most rows hold keys that are still live and spendable. Not exposed via API (api/runs.py:81-89, 173-181 omit it) or worker events (worker reads the env only in llm.py:292-293; deltas carry content only, graph.py:252-257); not logged (manager.py:227).
   - Seam break: anyone with DB read (backups, replicas, SQL access) holds spend-capable credentials indefinitely; blast radius per key = remaining budget (≤$5 default), count grows monotonically.
   - Fix: clear the column on release; encrypt at rest like BYO PATs (user.py:33 per backend audit #12 — I didn't read user.py myself this session; cite thread.py:35 only, or quickly read user.py:33? The backend audit claims it; I should either verify or not cite. I'll phrase without user.py or mark that part as per backend audit. Better: quickly read user.py:25-40 to confirm the encryption contrast.)

8. [LOW] [RISK] release_key is best-effort with no retry and no reaper
   - Backend: thread_manager.py:229-233 logs and swallows gateway delete failures; no background key reaper exists (main.py:57-63 starts only ingest/approvals/heartbeats/reconcile).
   - Fix: retry queue or periodic gateway-side sweep by alias prefix `thread-`.

9. [LOW] [UNVERIFIED] LITELLM_BASE_URL injected without /v1 while spike docs use /v1
   - Backend: worker_gateway_url default "http://gateway:4000" (config.py:35) injected as LITELLM_BASE_URL (manager.py:147). Worker passes it verbatim as ChatOpenAI base_url (llm.py:292-305) → requests go to /chat/completions at root. Spike docs say http://gateway:4000/v1 (worker/spike/README.md:36-37). Whether the deployed LiteLLM serves root routes is outside this repo — UNVERIFIED. If it ever stops, every thread fails at once.
   - Fix: pin the suffix in one place (append /v1 at injection or in make_llm).

Wait — I should double check finding 9's claim about langchain behavior... ChatOpenAI(base_url="http://gateway:4000") — the openai SDK appends /chat/completions to base_url. Yes. And LiteLLM proxy historically serves both /chat/completions and /v1/chat/completions. So it probably works today. LOW/UNVERIFIED is right.

Also should I mention the ANTHROPIC_BASE_URL for the SDK legacy path — same shape. One line.

Let me quickly read user.py:25-40 to verify the encryption contrast for finding 7, so I can cite it firsthand.


agent9:

I have complete evidence on both sides of the seam. Compiling the audit report.

No frontend directory in this repo — so the UI side of RESUME_RUN / EDIT_AND_RESEND delivery is UNVERIFIABLE here. The tests pin the resume_from_thread_id wiring with a PRIOR session_id set manually ("sess-old-123") — i.e., tests assume the SDK-runtime capture already happened; they never exercise the custom-engine path where session_id is never populated.

I now have everything needed. Let me consolidate findings:

---

**FINDING 1 — [BLOCKER] [CONFIRMED] Custom engine (production default) never populates thread.session_id → all resume paths silently start a stranger**

Worker evidence:
- events.py:163-172 — custom engine's `turn_boundary` emits STATUS/"turn complete" with detail {num_turns, duration_ms, is_error, usage} — NO `session_id` key. (The custom LangGraph engine has no SDK session at all.)
- runner.py:339-344 — `_run_turn` publishes this boundary via forwarder; it's the only turn-complete emission in the custom engine.
- main.py:264 — ENGINE defaults to "custom".

Backend evidence:
- bus.py:150-156 — capture requires `event.detail.get("session_id")`; custom-engine boundary lacks it → `thread.session_id` stays NULL forever. bus.py:156 is the ONLY writer (grep confirmed).
- config.py:39 — `engine_runtime: str = "custom"` — production default.
- api/sessions.py:70 — `resumable: l.session_id is not None and session_volume_exists(...)` → always False for custom-engine threads → Resume button never appears / replay-only banner always shows.
- thread_manager.py:94-101,108 — kill_replace/mode-switch inheritance reads `prior.session_id` = None → replacement row session_id=None.
- manager.py:154-155 — `if thread.session_id: env["RESUME_SESSION_ID"]` → never injected.

So under the default engine: the `/sessions/{run}/resumable` banner says replay-only forever; kill_replace's docstring claim (run_manager.py:364-369 "the replacement resumes where the killed thread left off — now actually true") is FALSE; resume_run's docstring claim (run_manager.py:116-124) is FALSE.

**FINDING 2 — [BLOCKER] [CONFIRMED] Resume env-var name mismatch across the seam: backend injects RESUME_SESSION_ID, custom engine reads RESUME_CONTEXT_ID (never set)**

- Backend: manager.py:154-155 injects `RESUME_SESSION_ID=thread.session_id` (when set). Zero backend hits for RESUME_CONTEXT_ID (grep).
- Worker SDK runtime: main.py:63,93-94 reads RESUME_SESSION_ID → ClaudeAgentOptions(resume=...). Match — for ENGINE=sdk.
- Worker custom engine: runner.py:87 reads RESUME_CONTEXT_ID; runner.py:99 `context_id = resume_context_id or thread_id` keys the LangGraph checkpointer. Backend never sets it; on replacement THREAD_ID is a NEW uuid (thread_manager.py:105) → context_id = new id → fresh checkpoint → `_initial_state()` seeded (runner.py:266-268) → stranger.
- Even the checkpoint substrate: manager.py:152-153 injects DATABASE_URL only `if settings.engine_database_url`; config.py:47 default is `""` → MemorySaver (checkpointer.py:195-205) + JSONL mirror at `./checkpoints` (runner.py:88) — container-ephemeral, not volume-mapped (manager.py:185-197 maps only the session dir + caches + workspace). So custom-engine state survives container replacement in NO configuration the backend can produce.

**FINDING 3 — [HIGH] [CONFIRMED] Edit-and-resend is advertised but 100% unwired end-to-end; the intent silently no-ops**

- Advertised: services/runs.py:23-24 — INTERRUPTED runs offer EDIT_AND_RESEND.
- Gate: intents.py:71-77 — EDIT_AND_RESEND passes gate_intent for INTERRUPTED runs.
- Dispatcher: api/runs.py:279-462 — elif chain has NO EDIT_AND_RESEND branch → falls through to `return {"status": "ok"}` (462). User taps the button, gets ok, nothing happens. No fork, no resend, no error.
- Backend helper dead: services/sessions.py:87-104 `fork_point_before_last_user_message` — no production caller (grep: only tests).
- Worker helper dead: worker/sessions.py:32-38 `fork_for_edit_and_resend` — no production caller (grep: only scripts/test_fork_smoke.py).
- DB column never written: thread.py:30 `forked_from_session_id` — grep shows no writer; runs.py:176 only reads it for display.
- The one caller that exists calls it wrong: scripts/test_fork_smoke.py:86-90 calls `fork_for_edit_and_resend(session_id=..., up_to_message_id=..., cwd=...)` but the signature is `(old_session_id, up_to_message_id)` (worker/sessions.py:32) — TypeError on `session_id=` kwarg + unexpected `cwd`. The path was never executed end-to-end.
- Also, if it were wired on the SDK runtime, the NEW fork id would only reach thread.session_id via the next turn-complete capture (bus.py:150-156) — there's no direct channel; under the custom engine there's no session at all. But since nothing invokes fork, the primary fact is: dead feature.
- Q2 answer: the NEW id never reaches the backend because fork never runs; the backend keeps resuming the OLD pre-fork id — moot, since resume itself is broken under the default engine (Findings 1-2).

**FINDING 4 — [HIGH] [CONFIRMED] resume_run never kills or waits for the old container → two containers on one session volume**

- run_manager.py:116-148 — resume_run: no control.kill, no stop_container, no wait_for_container_exit. (Contrast kill_replace at run_manager.py:408-419 which at least kills + waits 15s.)
- stop_run (the usual precursor, INTERRUPTED) only publishes control.interrupt (run_manager.py:240) — containers keep running.
- Widening factor on the custom engine: "interrupt" is unhandled by the custom engine's control pump (runner.py:419-457 handles kill/nudge/spawn_done/mode only) → stop_run on a custom-engine thread doesn't even interrupt the in-flight turn; the container keeps working, then lingers 900s (runner.py:89 idle_ttl default).
- The replacement spawn mounts the SAME volume: thread_manager.py:147 resume_from_thread_id → manager.py:190-192 mounts sessions/<run>/<old thread> at /root/.claude rw.
- For ENGINE=sdk: old container alive + new container resuming the same session dir = two writers on one session volume — the §1 corruption class, exactly what wait-for-exit exists to prevent (manager.py:248-252 docstring). Window = up to the idle TTL (~10-15 min) after every stop-then-resume.
- For ENGINE=custom: the volume is unused by the engine, so no corruption — but resume is a stranger anyway (Findings 1-2).

Also resume_run has no stage guard: run_manager.py:125-137 checks existence/ownership only; transition(QUEUED, allow_terminal_exit=True) — services/runs.py:37-48 only blocks terminal WITHOUT the flag; resume passes the flag unconditionally. Resuming an ACTIVE (e.g. INVESTIGATING) run? The REST endpoint /sessions/{run_id}/resume (api/sessions.py:77-92) has no stage check → user can resume a RUNNING run → second blueprint execution → second thread spawned (capacity permitting) → two threads on the same... wait, the new thread mounts the LAST thread's volume while the last thread is still actively running. Two live writers, no kill at all. And double-click on Resume → two _execute tasks tracked (run_manager.py:144-147 `_track` overwrites the task entry but both run). This is an idempotency hole too. Confidence: CONFIRMED by code; no guard anywhere in the path.

**FINDING 5 — [MEDIUM] [CONFIRMED] First-turn crash: session_id capture waits for "turn complete" — kill_replace/banner/resume all degrade silently (SDK runtime)**

- Worker emission site: normalize.py:154-167 — session_id leaves the worker ONLY in the ResultMessage ("turn complete") detail. The SystemMessage "init" (which carries session_id at session start) is explicitly filtered as noisy (normalize.py:52 `_NOISY_SYSTEM_SUBTYPES`, :148-152) and never emitted.
- Backend: bus.py:150-156 capture. A container that dies mid-first-turn (OOM, gateway down → main.py:119-123 fail-safe) never emits turn complete → thread.session_id NULL.
- Consequences: api/sessions.py:70 resumable=False (banner says replay-only even though the on-disk ~/.claude volume holds a resumable session); kill_replace inherits None (thread_manager.py:100) → no RESUME_SESSION_ID (manager.py:154) → SDK starts fresh session; the volume is mounted (manager.py:190-192) but unused for resume. Silent context loss; user sees the replacement "resume" per run_manager.py:364-369's claim.

**FINDING 6 — [MEDIUM] [CONFIRMED] seq collision: backend-persisted user messages share the worker's seq space; duplicate (thread_id, seq) rows, no DB constraint**

- Backend writer: runs.py:27-60 `_persist_user_message` inserts Event(seq=thread.next_seq) and bumps (also run_manager.py:334-358 pin_finding).
- Worker writer: normalize.py:83-97 / events.py:33-50 — per-container counters starting at 0, one per thread.
- Ingest: bus.py:137-141 inserts unconditionally; :143-144 bumps next_seq only when `event.seq >= thread.next_seq` — no dedupe, no unique constraint (event.py:26-32 — three non-unique indexes only).
- Collision: after N worker events (seq 0..N-1 → next_seq=N), a persisted user message takes seq=N; the worker's next event (nudge turn's first emission) ALSO has seq=N → two rows (thread_id, N). Replay orders by (thread_id, seq) (services/sessions.py:69) → relative order of the pair is DB-unspecified; transcript JSONL gets both appended in arrival order (bus.py:167-173); incremental fetches with after_seq=N skip BOTH (`seq > after_seq`, services/sessions.py:67-68; transcript.py:58).
- Also the Redis redelivery window: commit at bus.py:161, xack at :177 — a crash between re-processes the message → unconditional re-insert duplicates the event row AND the transcript line. No constraint to stop it.
- Q6's specific fork framing is moot (fork unwired), and stale OLD-lane events after kill_replace land on the OLD thread_id lane (replacement always gets a fresh thread row, thread_manager.py:105) — no cross-lane seq collision there; they just grow a "replaced" lane (bus.py:142-144 has no terminal-status guard). The live seq-collision bug is the user-message path above.

**FINDING 7 — [LOW/MEDIUM] [CONFIRMED] session_store (cross-host mirror) is dead code; retention sweep would leak mirrors if it were wired**

- session_store.py:77-107 upload/materialize/purge — no production callers (grep: only tests + module itself). Cross-host session portability ("VM → AKS" per docstring :1-5) does not exist.
- Latent: purge_expired_sessions (manager.py:268-290) rmtrees local dirs but never calls session_store.purge — if upload were ever wired, mirrors would outlive the 30d replay-only decay the docstring (session_store.py:8-9) promises.
- The materialize-vs-live-container race (my Q5) cannot fire because materialize is never called; when wired, it must inherit kill_replace's wait-for-exit gate (currently best-effort 15s, manager.py:253-259) — flagged as RISK for the wired future, not a live bug.

**Q4 — Hydration: VERIFIED-OK (no seam)**
- hydration.py serves my_tickets / blast_radius / hydrate_title / PrewarmPool stub. Consumers: frontend via api/hydration.py (all five routes behind current_user: :23,34,41,51,59) and run_manager.create_run's title hydration (run_manager.py:88-90). The WORKER fetches nothing over HTTP from the backend at boot — all config arrives via env (runner.py:78-90; main.py:53-65); worker HTTP usage is LLM-gateway-only (llm.py) + tool-side fetches (tools/deferred.py:182, extended.py:82). No request/response shape shared across the seam to mismatch. blast-radius is unscoped by repo permission (any authed user probes any repo name) — org-shared fleet metadata, not session content; LOW at most, arguably by design (§4 no-gate). I'll list as OK with a note... actually blast_radius auth: `_user: User = Depends(current_user)` — any authenticated user can enumerate blast radius for arbitrary repo names, revealing service topology. Cross-tenant? This is single-tenant-per-org by design (sessions are private per user, the fleet is shared). Not a finding in my seam. Skip or LOW. I'll mention in VERIFIED-OK with the caveat.

**Q7 — api/sessions.py: VERIFIED-OK**
- replay (api/sessions.py:24-28): load_run_for_user + replay_events re-checks created_by (services/sessions.py:61-63).
- transcript (31-55): load_run_for_user; disk path derived from owned run_id (transcript.py:23-27); run_ids are server-generated uuids — traversal strings never resolve to a Run row → 404.
- resumable (58-74): load_run_for_user.
- resume (77-92): load_run_for_user + resume_run re-checks created_by (run_manager.py:127-129).
- No cross-tenant content leak found. (Missing stage guard + idempotency on resume = Finding 4.)

**CORRECTED prior claims:**
1. bus.py:145-149 comment — "The worker's 'turn complete' status event carries the SDK session_id (worker/worker/normalize.py). Nothing else writes it, so without this capture the thread is never resumable — the replay-only banner... and kill_replace's claimed resume both depend on this single field." — Accurate for ENGINE=sdk; FALSE premise for the default ENGINE=custom (config.py:39): the custom engine's turn complete (events.py:163-172) carries no session_id, so the capture never fires and the thread is never resumable regardless. The comment documents a mechanism that is inert in the default configuration.
2. backend_diagnosis.md #3 — "run_manager.py:416-449 then mounts the session volume anyway" (kill_replace wait-for-exit timeout): CONFIRMED (run_manager.py:416-419 wait → :443-449 spawn; manager.py:253-259 log-and-proceed). CORRECTION/EXTENSION: the same diagnosis missed that (a) resume_run has NO wait at all, and (b) under the default custom engine the mounted volume is never read — the corruption window and the resume benefit are both engine-gated.
3. run_manager.py:364-369 kill_replace docstring — "the replacement resumes where the killed thread left off — now actually true": only true for ENGINE=sdk after a completed first turn; false for custom (always) and for sdk-pre-first-turn-complete. Partially false as stated.
4. services/sessions.py:1-8 module docstring — "edit-and-resend FORKS sessions within the run (forked_from_session_id)": no such path exists (Finding 3). The docstring describes unbuilt behavior.
5. session_store.py:8-9 — "The 30-day retention policy deletes both the volume AND the mirror": the sweep (manager.py:268-290) never calls session_store.purge; and upload is never called so no mirror exists. Claim is doubly inert.
6. harness_diagnosis.md — worker audit item on replay-fallback mirror: "replay-fallback mirror lives under ./checkpoints — dies with the shredded workspace unless deploy maps it" (line 65) — CONFIRMED from my side: CHECKPOINT_MIRROR_DIR default ./checkpoints (runner.py:88); backend volume map (manager.py:185-210) does not include it.

Wait, also verify one more time that no blueprint passes resume_from_thread_id for ask.py in a way that... I already verified all six blueprints consume the artifact (swarm.py:138, debug.py:128, ask.py:94, plan.py:141, goal.py:222, development.py:127). Good — resume_run's artifact IS consumed. So resume wiring is: artifact → spawn(resume_from_thread_id) → inherit session_id + mount old volume. The chain is complete; it just transmits a NULL session_id under the default engine and a useless volume mount.

One more question from the mandate: "What happens if thread.session_id is NULL — fresh session silently (user sees 'resumed' but context lost)?" — answered: env not injected (manager.py:154 guard), SDK starts fresh (main.py:63,93-94 conditional), no warning event, resume endpoint returns {"continues": run_id} (api/sessions.py:92). CONFIRMED silent.

Also "what do kill_replace / resume / the session banner do then?" — banner: resumable=False (api/sessions.py:70); kill_replace: inherits None (thread_manager.py:94-101); resume: same via spawn. All confirmed.

Let me also double-check the `handle` in normalize for ResultMessage title — "turn complete" matches bus.py:153 exactly. Yes: normalize.py:156.

And confirm the emitters' seq behavior once more for the SDK runtime: Normalizer._seq=0 per container (normalize.py:83). Within one container, seq continues across turns (the Normalizer lives for the container lifetime — main.py:73 one per ThreadRuntime). Same for EventEmitter (runner.py:101 once). So within-lane monotonicity holds per container. Good.

Severity assignment:
- Finding 1 (custom engine never sets session_id): BLOCKER — core advertised feature (resume continuity, §1 moat "durable sessions") is dead in the default configuration.
- Finding 2 (env name mismatch + ephemeral checkpoints): BLOCKER — even if 1 were fixed, resume still fails for custom engine; two independent breaks on the same path. Actually 1 and 2 are the same user-facing failure (custom-engine resume impossible) via two independent mechanisms — I'll present them as one BLOCKER with two confirmed root causes? The mandate wants precision; I'll keep them as separate findings since fixes differ (emit+capture a stable session identity vs. align env var + durable checkpoint keying/mount). Hmm — actually let me merge them into ONE blocker finding with two root causes, cleaner for the report... No — keep separate; they cite different evidence and different fix directions. But mark clearly they're the same user-facing break.

Actually, on reflection: Finding 1 = "identity never captured"; Finding 2 = "resume signal never injected/read + checkpoint state never durable". Both needed to fix resume. Present as 1 blocker with 2 sub-causes? The output format wants a finding per item. I'll do two findings, cross-referenced.

- Finding 3 (edit-and-resend unwired): HIGH (advertised action silently no-ops; feature absent). Could be BLOCKER for the feature, but the feature is "degraded UX" by design docs — HIGH.
- Finding 4 (resume_run no kill/wait + no stage guard/idempotency): HIGH (§1 corruption class on sdk engine; double-spawn on any engine).
- Finding 5 (first-turn crash window): MEDIUM (bounded window; affects sdk only; silent stranger).
- Finding 6 (seq duplicates): MEDIUM (transcript integrity, replay order nondeterminism, after_seq skip).
- Finding 7 (session_store dead + purge gap): LOW.

Also the smoke-test signature mismatch — fold into Finding 3 as evidence.

VERIFIED-OK list:
- bus.py:150-156 ↔ normalize.py:154-167 exact contract match (kind STATUS, title "turn complete", detail key session_id) for the SDK runtime.
- Thread ids never reused across replacement (thread_manager.py:105 uuid) → no cross-container seq collision within a lane.
- Stale old-lane events after kill_replace cannot corrupt the replacement lane (bus.py:137-144 keys on event.thread_id; old lane grows but is terminal).
- Hydration endpoints all authenticated (api/hydration.py:23,34,41,51,59); worker never calls them (no backend HTTP client in worker — env-only config runner.py:78-90, main.py:53-65).
- api/sessions.py ownership guards on all four routes + service-layer re-checks (services/sessions.py:61-63; run_manager.py:127-129).
- Blueprints consume resume_from_thread_id (ask.py:94, development.py:127, plan.py:141, swarm.py:138, debug.py:128, goal.py:222).
- kill_replace DOES wait for container exit before spawn (run_manager.py:416-419; manager.py:248-259) — best-effort but present (matches backend diagnosis #3).
- session_store.unpack path-traversal guard (session_store.py:68-73).
- transcript.py read skips malformed lines; after_seq filtering present.

Now — the report. Also mention `_chronological_merge` cross-lane ordering as fine.

One more sanity check I should do: confirm worker/sessions.py signature vs smoke test call — sessions.py:32 `async def fork_for_edit_and_resend(old_session_id: str, up_to_message_id: str)`; smoke test:86-90 `fork_for_edit_and_resend(session_id=..., up_to_message_id=..., cwd=...)`. `session_id=` doesn't match `old_session_id` → TypeError: unexpected keyword argument 'session_id' (and 'cwd'). Yes, confirmed mismatch.

Also verify the claim "The new thread's session_id is also set from the old one (handled in thread_manager.spawn)" (manager.py:183) — TRUE mechanically (thread_manager.py:94-101) but inherits NULL under default engine.

I think the audit is complete. Everything cited was read this session. Write the final report.


agent10:

All evidence gathered and cross-verified. Compiling the audit.

Root pyproject.toml has `[tool.uv.workspace]` + `[tool.uv.sources] collegium-contracts = { workspace = true }` — so local dev resolves both worker and backend to the SAME editable workspace source (uv.lock:1491,1557 editable packages/contracts). Good — local dev can't drift. The drift window is the DOCKER path: each image does its own `COPY packages/contracts` + `pip install` (worker/Dockerfile:20,25; backend/Dockerfile:10,13) from its own build context at its own build time, with no version pin inside the package (version 0.1.0) and no runtime handshake. So: backend image built from commit A + worker image 0.1.0 (config.py:36 default tag, Dockerfile comment says 0.2.0) built from commit B → different StepEvent/Plan/Decomposition schemas, and the only runtime signal is a dead-letter + watchdog card on validation failure (bus.py:126-133), while additive drift is silently ignored (StepEvent has no extra="forbid" — events.py:50-72, no model_config). Confirmed.

Let me read the root pyproject quickly for exact citation lines, then I have everything. Actually the grep gave me the lines: pyproject.toml:7 [tool.uv.workspace], :15-16 sources. Good enough for citation.

Now assemble the audit. Findings list:

**F1 [BLOCKER/CONFIRMED] Worker's spawn_swarm/spawn_agent are phantom spawns — nothing ever executes, backend never sees them.**
- Worker evidence: fanout.py:229-236 (spawn_swarm only `_registry.register(...)` and returns a success string); fanout.py:208-210 same for spawn_agent; the only consumers of the registry: tools/__init__.py:287-311 (arms 2h watchdog only), runner.py:306 (drain flips status flags), runner.py:444 (finish on spawn_done). 
- Backend evidence: Thread rows are created ONLY by thread_manager.spawn (thread_manager.py:104-125); no API path creates threads from worker calls (runs.py has no such endpoint); capacity counts only Thread rows (semaphores.py:28-33).
- spawn_done has NO producer: grep repo-wide shows only runner.py:438-444 consumes it; nobody publishes a "spawn_done" control message (backend/app/events/control.py — I didn't read control.py... hmm. I grepped "spawn_done" across the whole repo — only runner.py hits. That covers backend producers too. Good.)
- What breaks: the tool's docstring (fanout.py:10-13) promises "Each slice runs as its own thread under the run (NOT a subagent — they're siblings)" and the tool returns "spawned swarm of N threads" (fanout.py:235) — the model believes sibling threads exist; the backend shows nothing; no capacity consumed; no work performed; the 8-cap registry saturates permanently (no spawn_done producer; only 2h timeout flips status, fanout.py:270-277) so all later fan-out is vetoed for the thread's life (fanout.py:159-160, 162-164).
- CORRECTS worker audit #1 partially: the "durable row created async after the tool returns" claim is FALSE — no durable row is ever created; the check-then-act race on live_count exists (fanout.py:201-207, 225-234 — register happens after veto with no lock; but tools run in an executor — concurrent tool calls in one thread could interleave... the graph executes tool calls sequentially per worker audit #5, so within one worker process spawns are serialized by the sequential tool loop; across processes each has its own registry) — the race is bounded by the sequential tool loop and per-process registry, but the deeper truth is the registry guards nothing real.
- Fix: either wire registry entries to real execution (in-process subagent contexts or a backend spawn API with capacity reservation), or remove the tools from the surface until implemented; make spawn_done a real backend→worker message if sibling execution lands.

Severity: BLOCKER for the seam (the worker's half of the swarm contract is unimplemented while telling the model otherwise). This also bears on cap-trinity question: the "8" never multiplies against the "12" — 96-agents scenario impossible because worker spawns are inert. WHO counts WHAT: backend counts Thread rows/containers (semaphores.py:31); worker counts phantom registry entries (fanout.py:91-95). Nothing is double-counted because nothing worker-side is counted at all — the worker-internal swarm consumes zero backend capacity and zero compute.

**F2 [HIGH/CONFIRMED] "input_required" heartbeat status silently drops the thread from capacity accounting AND the per-repo write lock — and then wedges the row.**
- Worker evidence: runner.py:209 (`self.status = "input_required"` while awaiting approval, up to approval_timeout_s=900, runner.py:90,211), runner.py:359 (blocked-escalation sets it as the resting status, heartbeat at 362).
- Backend evidence: heartbeats.py:115-116 mirrors the free-string status into Thread.status while the row is active; semaphores.py:15 ACTIVE_STATUSES excludes "input_required" → active_thread_count (semaphores.py:31) and the repo-conflict check (semaphores.py:46-50) both skip it; run_manager.py:288-294 documents the deliberate exclusion.
- What breaks: a writable development thread that pauses on a 900s approval wait (a) stops counting against global_thread_cap=12, (b) releases the per-repo write lock de facto — a second writable thread on the same repo passes semaphores.py:46-55 while the first container still holds its writable clone mounted (manager.py:199-205) and will resume writing the moment the decision lands (runner.py:211-218). Two live writers on one repo — §2 inverts. Additionally, once the row reads "input_required" the heartbeats.py:115 guard (`thread.status in ACTIVE_STATUSES`) blocks ALL further status mirroring — the row can never heartbeat back to running/idle/completed; reconcile_on_boot:638 also skips it (only running/idle/queued). Only a nudge (run_manager.py:294,309) or explicit stop/kill_replace unsticks it.
- Also: swarm blueprint `_await_thread` (swarm.py:280-281) doesn't list "input_required" — an explorer that lands an approval wait hangs the collect node until idle TTL... actually no: the thread's row is stuck "input_required"; _await_thread polls the ROW status forever — it never becomes terminal → collect hangs indefinitely (until run stop). Read-only explorers under MODE=development (F3) can reach the approval gate. HIGH.
- Fix: include "input_required" in ACTIVE_STATUSES (it is active — container alive, budget meter running) and in the mirror-back path; or have the worker keep heartbeating "running" with an input_required detail field.

**F3 [HIGH/CONFIRMED] MODE env is a global constant, not the run's mode — every worker thread runs MODE=development regardless of run.mode.**
- Backend evidence: manager.py:145 `"MODE": self.settings.engine_default_mode` (never run.mode, though `run` is in scope at thread_env:127); config.py:43 `engine_default_mode: str = "development"`.
- Worker evidence: runner.py:81 `Mode(os.environ.get("MODE", "ask"))`; tools/__init__.py:169-175 MODE_ALLOWED maps development→_DEV_SET; tools/__init__.py:190-193 default-binds spawn_agent+spawn_swarm in development/goal.
- What breaks at the seam: ask-mode threads, swarm explorers (read-only by design, swarm.py:13-14,48) and plan planners all receive the DEVELOPMENT tool surface — mutating tools bound and the phantom fan-out tools offered to every thread. The read-only swarm guarantee then rests entirely on ro mounts (manager.py:210) and the approval gate; under autonomy=autonomous (_engine_autonomy bypassPermissions→autonomous, manager.py:117-120) the gate auto-passes and only the ro mount stops writes — while spawn_swarm becomes callable by Explorer threads, feeding F1's phantom registry. Mode vocabulary also diverges: backend blueprints include "width-swarm" (mode_engine.py:31) which the worker Mode enum (state.py:30-35) lacks — a future fix that passes run.mode verbatim would crash the worker with ValueError for swarm runs (fail-fast, but a latent incompatibility).
- Fix: pass MODE from run.mode (or the thread's persona role) at manager.py:145 and reconcile the mode vocabularies (worker enum vs backend mode/topology names).

**F4 [HIGH/CONFIRMED] Contracts drift: no pin, no handshake, silent additive drift.**
- Packaging evidence: worker/pyproject.toml:20 + backend/pyproject.toml:23 both declare bare `"collegium-contracts"` (no version specifier). Local dev is safe: root pyproject.toml:15-16 `collegium-contracts = { workspace = true }` + uv.lock:1491/1557 editable. Docker deploys are not: worker/Dockerfile:20,25 and backend/Dockerfile:10,13 each `COPY packages/contracts` + `pip install` from their OWN build context/time; worker image tag drift already visible: config.py:36 expects `collegium-worker:0.1.0`, worker/Dockerfile:4 comment builds `:0.2.0`. contracts package version is 0.1.0 (packages/contracts/pyproject.toml:3) — no bump discipline visible.
- Runtime evidence: worker heartbeat payload carries only {thread_id, run_id, status} (forwarder.py:68-72) — no contracts version. Backend ingest validates StepEvent shape (bus.py:124) but never compares schema_version (no schema_version read anywhere in backend/app except literal payload strings in delivery.py:113, plan.py:39, debug.py:35 — grep confirmed). StepEvent has no extra="forbid" (events.py:50-72) → a newer worker's additive fields are silently dropped; an older worker missing required fields dead-letters (bus.py:126-133) — the only "detection" is a dead-letter stream + log, not a handshake. The contract's own versioning rule (events.py:14-18: "Consumers MUST guard on schema_version") is unimplemented on the consumer side.
- What breaks: worker and backend images built from different commits run different Plan/Decomposition/StepEvent schemas with zero detection; failures surface as unexplained dead-lettered events or silently dropped fields.
- Fix: pin contracts version in both images from a single source of truth; add schema_version guard in bus.py:_process; worker reports contracts version at boot (heartbeat/env) and backend refuses mismatch loudly.

**F5 [MEDIUM/CONFIRMED] Queue visibility: spawn_many publishes "queued" under fake thread ids; queue is poll-and-race, and queued slices rely on idle-TTL slot release.**
- thread_manager.py:182-198: while True retry; `"queued" not in str(exc)` string sniffing at :190 (only the cap message contains "queued", semaphores.py:40 — the write-lock message at :43/:55 doesn't, but spawn_many passes writable_repo=None at :187 so that path is unreachable here — fragile coupling confirmed but currently unreachable).
- thread_manager.py:196-197 publishes thread_status "queued" with `spec.get("thread_hint", spec["persona"])` — a fake id ("explorer-0"); relay.py:54-60's own comment states publish_thread_status "requires a real thread_id" and the prior fake-id misuse "was silently dropped by the UI". During the queue wait NO Thread row exists (created only after try_acquire at thread_manager.py:86,104-125) — GET /runs/{id}/threads (runs.py:165-183) can't show queued slices either. So queueing is neither visible (fake-id status likely dropped) nor ordered (asyncio.gather + fixed 5s sleep race, thread_manager.py:198,200). Confirms backend #8 with the extra seam detail.
- Cap-trinity trace: swarm _hydrate clamps requested to 12 (swarm.py:109) but _fanout spawns ALL Lead-returned slices unclamped (swarm.py:165-171); over-cap slices wait in _spawn_one until slots free — slots free only when active threads leave ACTIVE_STATUSES, i.e. explorer idle→completed via the worker's idle TTL (runner.py:89 default 900s; main.py:65 legacy 600s) because the swarm blueprint never calls finish_thread on explorers (swarm.py:225-268 settles cost only). So a 50-slice decomposition under cap 12 doesn't retry "forever" (correcting backend #2's phrasing slightly) — it drains in ~12-thread waves gated by 15-min idle TTLs, with the decompose Lead thread (swarm.py:135-139) and later synthesis thread each holding a slot the whole time. Pathological latency, not deadlock — unless F2's input_required wedge intervenes (then _await_thread at swarm.py:280 never returns).
- Fix: publish a run-scoped note (publish_note) or create real QUEUED Thread rows; replace poll-and-race with a FIFO waiter queue; clamp decomposition.slices to remaining capacity; call finish_thread on explorers at collect.

**F6 [MEDIUM/CONFIRMED] POST /runs double-create spawns two full runs — no idempotency key, no DB constraint; write lock is the only backstop and only for writable modes.**
- runs.py:63-69 CreateRunBody has no idempotency key; runs.py:92-103 passes through to run_manager.create_run; run_manager.py:96-105 creates Run with id=uuid4 (run.py:30 PK; no unique constraint on created_by+work_item_id or title); run_manager.py:113 immediately tracks _execute → blueprint spawns threads.
- Seam trace for a double-click: two Run rows, two blueprints, two thread sets — each thread its own container (manager.py:224 name=full uuid), own session volume sessions/<run_id>/<thread_id> (manager.py:190-192), own gateway key (thread_manager.py:132-136). NOT two containers on one volume (different run_ids), NOT two workers on one thread_id. What DOES happen: double spend + double threads; for writable modes (development.py:126 passes writable_repo=writable) the second run's thread hits the write lock (semaphores.py:46-55) → ThreadSpawnError → _guarded_execute marks the second run FAILED (run_manager.py:184-210) — loud but wasteful; for read-only modes (ask/swarm/plan/debug — all writable_repo=None per the grep) both runs proceed concurrently = silent double spend. Triggers path is deduped (trigger.py:38-40 uq_trigger_dedupe; _log_event returns None on IntegrityError, triggers.py:200-204) — the UI path has nothing.
- Confirms backend #5 at the seam with the precise blast radius.
- Fix: idempotency key header/body + unique constraint (e.g. (created_by, idempotency_key)) or client-side request dedupe.

**F7 [LOW/CONFIRMED] register_run timing is safe, but unregistration/abandon and backend-restart leave orphan streams.**
- Q5 answer: register_run is called at RUN-CREATION time (run_manager.py:109), before any container exists (thread_manager.py:143 starts it; thread_manager.py:166 re-registers redundantly). The consumer group is created with id="0" (bus.py:73) → the group's first read drains the FULL stream history, so events XADDed by a booting worker before registration are never lost. A worker cannot boot before its queue turn: spawn() raises before row/container creation (thread_manager.py:86-88). VERIFIED-OK for the asked question.
- The inverse gap: abandon_run unregisters (run_manager.py:273) while kill rides lossy pub/sub (worker control listener) — trailing events from a dying worker XADD to an unregistered stream and pile up unconsumed; forwarder.publish_events uses XADD with NO maxlen (forwarder.py:33-37) → unbounded stream growth. Backend restart: run_streams is in-memory (bus.py:48); reconcile_on_boot (run_manager.py:616-654) marks everything interrupted but never re-registers streams and never stops containers (confirmed: only DB updates + key release) — orphaned containers (backend #3) keep publishing events/heartbeats into streams nobody consumes and a heartbeat pub/sub the persister ignores only because the row is no longer active (heartbeats.py:115).
- Fix: TTL/maxlen on event streams; re-register active runs at boot or drop streams for terminal runs; stop containers in reconcile (backend #3's fix).

**F8 [LOW/CONFIRMED] Trigger contract conformance: mostly clean; received_at dropped; schema_version unguarded; drain_queued hardcodes source.**
- Backend normalizers construct contracts TriggerEvent directly (triggers.py:36, 66-78, 94-109, 123-134, 146-160) — required fields enforced by construction (TriggerError raised for missing ids: triggers.py:53-54, 87-88, 120-121, 142-143). Idempotency key (contract triggers.py:35-37) backed by uq_trigger_dedupe (trigger.py:38-40). Fail-closed identity (contract docstring triggers.py:4) implemented at triggers.py:354-358.
- Fields declared but effectively ignored: received_at (contract :33) — normalizers never set it (defaults to construction time) and _log_event never persists event.received_at (triggers.py:193-198; TriggerEventLog.received_at has its own default, trigger.py:52) — flap/rate windows (triggers.py:258, 307) use insert time; fine today, silently wrong if a source ever backdates. schema_version (contract :23) never read. changed_by_descriptor optional (contract :28-31) — backend treats None fine (identity.resolve_descriptor(None) presumably → None → failed verdict; build normalizer passes None deliberately at triggers.py:99).
- drain_queued reconstructs TriggerEvent with source=ADO_WEBHOOK hardcoded (triggers.py:515) and drops changed_by_descriptor — harmless for _task_text today (only ado normalizers enqueue), but a cron/manual source would drain mislabeled. Contract declares CRON and MANUAL sources (triggers.py:18-19); I found no cron/manual normalizer in triggers.py (only ADO ×4) — the contract admits sources the backend never produces. UNVERIFIED whether a cron ingress exists elsewhere — quick grep: TriggerSource usage in backend... I only grepped schema_version. Let me be honest: normalizers in triggers.py are the four ADO ones; the engine filters Trigger rows by event.source.value (triggers.py:329) so CRON rows could match if anything produced cron events. Mark as LOW/RISK.
- Also confirms backend #4 (at-most-once): _log_event commits at triggers.py:201 before create_run at :381; stuck "received" rows have no reaper (drain_queued filters status="queued" only, :486). CONFIRMED.

**VERIFIED-OK list:**
1. register_run before container start; group at "0" → no unread window (run_manager.py:109; bus.py:73,80; thread_manager.py:166).
2. Per-thread session volumes per run_id+thread_id — a double-created run cannot put two containers on one volume (manager.py:190-192, 224).
3. Thread/run ids are fresh uuid4 per spawn (thread_manager.py:105; run_manager.py:97) — no thread_id collision across double runs.
4. Backend Run stages strictly use the contracts RunStage enum (run_manager.py:20,103; services/runs.py:37-48 transition(run, RunStage)); runs.py:260-267 validates intent/source strings against contract enums with 422 (H-23).
5. Trigger dedupe constraint exists and matches the contract idempotency key (trigger.py:38-40 vs contracts triggers.py:35-37).
6. Swarm threads are read-only end-to-end at the backend seam: spawn_many hardcodes writable_repo=None (thread_manager.py:187) and context repos mount ro (manager.py:210) — the per-repo write lock is intentionally not engaged (semaphores.py:41).
7. "turn complete" title coupling holds on both engine paths: worker events.py:166 and normalize.py:156 emit exactly "turn complete"; backend captures session_id off that exact title (bus.py:150-156).
8. StepEvent seq monotonicity is honored at the seam: ingest upserts thread.next_seq from event.seq (bus.py:142-144).
9. Local-dev contracts resolution is unified via uv workspace (root pyproject.toml:15-16).

**CORRECTED prior claims:**
1. Worker audit #1 said "durable row created async after the tool returns" for spawn registrations — FALSE. No durable row exists anywhere for worker-internal spawns; the registry is the whole lifecycle (fanout.py:229-236; consumers: tools/__init__.py:307-311, runner.py:306,444). The check-then-act race it describes (fanout.py:201-207) is real code but guards a registry that spawns nothing; within one worker the sequential tool loop (graph.py — worker audit #5) serializes tool calls anyway, and the registry is per-process (fanout.py:131, runner.py:112-114).
2. Backend #2's "38 threads queue-retrying forever" — over-precise: retry is unbounded in code (thread_manager.py:182 `while True`) but slots DO free when explorers hit idle TTL (runner.py:89/483-492), so over-cap slices drain in slow waves; the true dead-end case is a slice whose thread wedges in "input_required" (F2) which _await_thread never exits (swarm.py:280-281).
3. Backend #5's "double-click = double run + double spend" — confirmed, but the sharper seam statement: read-only modes double silently; writable modes fail the second run loudly via the write lock (semaphores.py:46-55 → run_manager.py:184-210). No shared-volume or shared-thread_id corruption occurs (uuid4 + per-run volumes).
4. Backend #6 slot leak on row-insert failure — CONFIRMED as written: thread_manager.py:122-129, try/finally only closes the session; commit_reservation (line 129) is skipped on insert raise and release is never called; reservation leaks until restart (semaphores.py:61-68). I verified — no additional release path exists in spawn() (the gateway-mint failure at 137-139 and container failure at 150-152 happen AFTER commit_reservation and mark the row failed, which un-counts it — those paths are clean).
5. Worker audit #2 (_veto never checks repo) — CONFIRMED literally (fanout.py:152-174 never reads req.repo; SpawnRequest.repo at :58 is dead data), but its blast radius is different at the seam: since spawns never execute and never become backend threads, no two-writers-on-one-repo can arise FROM the worker fanout; the actual two-writer path at the seam today is F2 (input_required dropping the lock).

Prior claims re-verified as accurate: backend #1 (single-process locks — semaphores.py:20-26 in-memory; thread.py has no repo_scope constraint — confirmed), backend #8 (poll-and-race — thread_manager.py:182-198 confirmed), worker #3 (cap drift 8 vs 100 — fanout.py:48 vs philosophy.md:86-87,105 confirmed, plus backend 12 at config.py:100).

Severity ordering for output: F1 BLOCKER, F2 HIGH, F3 HIGH, F4 HIGH, F5 MEDIUM, F6 MEDIUM, F7 LOW, F8 LOW.

One more sanity pass on F2's mirror-back-wedge: heartbeats.py:115 `if status and thread.status in ACTIVE_STATUSES:` — when the approval resolves, worker heartbeats "running" (runner.py:212-213); row is "input_required" ∉ ACTIVE_STATUSES → not mirrored. Row stuck. Yes. And nudge_thread sets row.status="running" directly in DB (run_manager.py:309) — that unsticks it. stop_thread sets "stopped" (run_manager.py:325). finish_thread checks `thread.status in ("completed","failed","stopped","replaced")` returns early; "input_required" proceeds → stops container, sets final status (thread_manager.py:252,264-267) — so blueprint finish_thread (goal.py) would unstick it too, but swarm blueprint never calls finish_thread. OK.

Also for F3, honesty check: does anything else set MODE per-run? manager.py:145 is the only MODE in the env dict (lines 129-149 read). thread_env receives run; never reads run.mode. Confirmed. Note also AUTONOMY IS per-run (manager.py:146 ← permission_mode_for(run.autonomy), thread_manager.py:141) — so the mode/autonomy pair is half-connected.

Also should I double check `_engine_autonomy` mapping vs worker Autonomy enum: state.py:38-41 has supervised/gated/autonomous; _engine_autonomy returns "autonomous"|"gated"|"supervised" (manager.py:117-120). Consistent. VERIFIED-OK (add to list).

Word the summary tightly. Format per instructions: findings with severity/status/title, worker+backend evidence, what breaks, fix. Then (a) VERIFIED-OK, (b) CORRECTED, (c) 3-sentence slice summary.
