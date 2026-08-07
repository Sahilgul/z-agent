"""Identifier contract — the one-way door (decided before any rename locks in).

The Collegium harness has FOUR identifier layers. They are NOT interchangeable;
each has a specific scope, lifetime, and persistence surface. Getting this
wrong propagates everywhere (checkpoints, events, approvals, budget), so it
is fixed here, once.

Hierarchy (outer → inner):

  run_id      — a user-initiated work request. Owns 1+ threads.
                Lifetime: until the run is abandoned/completed.
                Persistence: runs table.

  thread_id   — the durable WORK UNIT (was "lane_id"). One repo, one
                conversation, one container-per-activation. A container is a
                rental that dies at IDLE_TTL; a fresh container resumes the SAME
                thread. This IS the LangGraph checkpoint `thread_id`.
                Lifetime: until the thread completes/fails/stops.
                Persistence: threads table + LangGraph checkpoints (by thread_id).

  context_id  — the CONVERSATION context identifier = the LangGraph checkpoint
                namespace. For a top-level thread, context_id == thread_id.
                For a subagent/worker (fan-out), context_id is DERIVED:
                `{thread_id}::worker-{n}` — isolated conversation state, but
                traceable to the parent thread for budget + audit.
                Lifetime: same as the owning thread (or worker run).
                Persistence: LangGraph checkpoints (by context_id).

  task_id     — a single TURN / agent-loop invocation within a context. One
                task_id = one `query()` → one ReAct loop → one ResultMessage /
                turn boundary. This is the unit of resumption, interruption, and
                per-turn budget accounting. Maps to a LangGraph checkpoint
                `checkpoint_id` (a specific point in the context's history).
                Lifetime: forever (the replayable audit unit).
                Persistence: events table (sdk_message_uuid bridges to it) +
                LangGraph checkpoint history.

Rules (load-bearing, never violate):

1. `run_id` is the budget + ownership boundary. A run owns ≥1 threads; the run
   budget is the sum of its threads' usage.
2. `thread_id` is the work unit. One thread = one repo. A run may own multiple
   threads (multi-thread run, formerly "swarm"). Threads never touch each
   other's files.
3. `context_id` is what the LLM sees as its conversation identity. Top-level:
   context_id == thread_id. Subagents: context_id is derived from the parent
   thread_id so checkpoints are namespaced but traceable.
4. `task_id` is the turn boundary. Interrupt/resume operates at the task
   level: a nudge interrupts the current task and starts a new one within the
   same context_id. Edit-and-resend forks at the task level (replay up to a
   task_id, then diverge).
5. Every StepEvent carries run_id + thread_id + task_id (+ context_id is
   thread_id for top-level threads; subagent events carry the derived
   context_id). seq is monotonic per thread_id.
6. The LangGraph checkpointer keys on context_id (= thread_id for top-level).
   Fork/resume uses get_state_history(context_id) and update_state.

Renaming (mechanical, this phase):
  lane_id  → thread_id   (everywhere: contracts, DB, Redis, env, worker, UI)
  lane     → thread      (class names, table names, channel names, docstrings)
  LANE_ID  → THREAD_ID   (env vars)
  "swarm"  → "multi-thread run" (UI chips + docs; the spawn_swarm TOOL keeps
              its name — it's a verb, not the concept)
"""

from __future__ import annotations

# This module is documentation-only — the identifiers are enforced by the
# Pydantic models in events.py and the DB schema. It exists so the one-way-door
# decision is discoverable from the contracts package itself.

IDENTIFIER_LAYERS = ("run_id", "thread_id", "context_id", "task_id")

__all__ = ["IDENTIFIER_LAYERS"]
