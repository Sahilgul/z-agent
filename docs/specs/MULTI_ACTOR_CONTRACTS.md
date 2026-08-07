# Multi-Actor Contracts — the spec that must come first (Plan Phase 3)

The Collegium harness has multiple actors touching the same workspace and the
same conversation. This spec defines who can do what, when, and how conflicts
are detected. Getting this wrong means silent corruption (two agents editing
the same file, a human approving a tool call that targets a different file
than the one they reviewed). So it is fixed here, before the approval code.

## Actors

| Actor | Scope | What it can do |
|-------|-------|----------------|
| **Lead** | the run | decompose, spawn threads, route approvals, hold the run budget |
| **Thread** | one repo, one conversation | run the agent loop, emit StepEvents, hold the thread budget |
| **Subagent** | an isolated context under a thread | fan-out work (Phase 6), own context_id, parent's budget |
| **Human** | the run | approve/deny, nudge, switch mode, edit-and-resend, abandon |
| **Watchdog** | the run | drift detection, collision radar, budget reminders (Phase 8) |

## The four contracts

### 1. Workspace ownership (the file-lock contract)

A thread OWNS its repo workspace for the duration of its activation. No other
thread writes to the same repo. The collision radar (Phase 8) warns when two
threads' repo scopes overlap; the Lead refuses to spawn a thread whose repo
scope collides with a running thread's.

- Read-only tools (file_read, file_search, terminal_exec readonly) never lock.
- Mutating tools (file_edit, file_write, terminal_exec mutating) take a
  write lock on the target path for the duration of the tool call.
- A lock is released when the tool call completes (success or error).
- Subagents inherit their parent thread's workspace lock scope.

### 2. Read-before-edit (the content-hash contract)

Every file_edit and file_write MUST carry the expected current content hash.
The tool computes the actual hash before applying the edit; if they differ,
the tool REFUSES the edit and returns the actual current content. This is the
"the file changed since you read it" guard — it prevents an agent from
clobbering a human's edit or another thread's edit (in the collision case).

- `file_edit(file_path, old_string, new_string, expected_hash?)` — if
  expected_hash is provided and mismatches, the tool returns the current
  content and the agent must re-read before retrying.
- `file_write(file_path, content, expected_hash?)` — same; null expected_hash
  = create-new-file (fails if the file exists).
- The hash is sha256 of the file content, hex-encoded, truncated to 16 chars
  (enough for collision detection, short for the event payload).

### 3. Two-phase verbatim approval (the approval contract)

A mutating tool call in SUPERVISED or GATED autonomy is NOT executed on the
first request. It goes through two phases:

**Phase 1 — preview**: the tool call is intercepted. The engine emits an
approval-card StepEvent carrying the EXACT command/edit that would be
executed (verbatim — the full command string, the full diff, the full file
content for a write). The human sees this in the feed and approves or denies.
The thread BLOCKS until the decision arrives (Redis BLPOP, plan §6).

**Phase 2 — execute**: on approval, the tool executes with the verbatim
args from phase 1 (NOT the args the agent might have mutated in the
meantime — the verbatim contract). On denial, the tool returns "denied by
user" and the agent continues from there.

- `always_allow` persists the tool CLASS (e.g. file_edit) for the run; future
  calls of that class skip phase 1. NEVER persists a specific file or command
  (that would let an agent escalate by reusing an always-allow on a new target).
- DESTRUCTIVE tools (rm, git push --force, etc.) NEVER get always_allow —
  every call is verbatim-approved, every time.
- AUTONOMOUS: nothing is bridged (bypassPermissions). The gateway per-key
  budget is the only backstop.

### 4. Turn-boundary isolation (the task contract)

A task_id is the unit of interruption and resumption. A nudge interrupts the
current task and starts a new one within the same context_id. Edit-and-resend
forks at a task boundary (replay up to a task_id, then diverge). Two tasks
never run concurrently in the same context — the graph is sequential per
context.

- The EventEmitter allocates task_id at turn start; all events in the turn
  carry it.
- The checkpointer keys on context_id; get_state_history(context_id) returns
  the task boundaries (each task = one checkpoint).
- A subagent gets a derived context_id (`{thread_id}::worker-{n}`) so its
  tasks are isolated from the parent's.

## What this means for the code

- `tools/mutating.py` (Phase 3): file_edit, file_write, terminal_exec (full)
  with the content-hash guard and the write-lock.
- `approvals.py` (engine-side, Phase 3): the two-phase gate, the verbatim
  buffer, the always-allow set (per-run, tool-class only), the BLPOP wait.
- `graph.py` (Phase 3): the tools node routes mutating calls through the
  approval gate; read-only calls execute directly.
- `events.py` (Phase 3): a new approval-card StepEvent kind carrying the
  verbatim preview.
