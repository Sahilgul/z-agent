/** Pure run-state logic (plan §8 lib/runMachine.ts) — the tested heart of the
 *  session: stage presentation, live-state interaction rules (§1a), watchdog
 *  detection, and the swarm critical path. No React imports. */

import type { Thread, RunStage } from "../types";

export interface StageMeta {
  label: string;
  index: number; // position on the pipeline rail (-1 = off-rail terminal)
  tone: "ok" | "info" | "warn" | "danger";
}

const RAIL: RunStage[] = [
  "queued", "provisioning", "investigating", "planning", "awaiting_user",
  "developing", "verifying", "pr_ready", "completed",
];

export function stageMeta(stage: RunStage): StageMeta {
  const idx = RAIL.indexOf(stage);
  switch (stage) {
    case "completed":
      return { label: "completed", index: idx, tone: "ok" };
    case "failed":
    case "abandoned":
      return { label: stage, index: -1, tone: "danger" };
    case "interrupted":
      return { label: "stopped — all work preserved", index: -1, tone: "warn" };
    case "awaiting_user":
      return { label: "waiting on you", index: idx, tone: "warn" };
    default:
      return { label: stage, index: idx, tone: "info" };
  }
}

export const RAIL_STAGES = RAIL;

/** §1a live-state rules: while the agent WORKS, decision buttons hide; the
 *  Stop button and typed Lead-nudges stay available. The backend's
 *  available_actions is the source of truth for WHAT may be offered; this
 *  decides WHEN the UI may show them. */
const AGENT_WORKING: RunStage[] = [
  "provisioning", "investigating", "planning", "developing", "verifying",
];
const ALWAYS_SHOW = new Set(["stop_run"]);

export function visibleActions(stage: RunStage, available: string[]): string[] {
  if (!AGENT_WORKING.includes(stage)) return available;
  return available.filter((a) => ALWAYS_SHOW.has(a));
}

export function agentWorking(stage: RunStage): boolean {
  return AGENT_WORKING.includes(stage);
}

// ------------------------------------------------------------------ watchdog
export const WATCHDOG_STALE_MS = 3 * 60 * 1000;

/** Run stages where no thread is doing anything — a finished run must never
 *  nag the user to nudge a thread, even if a beat was lost and the row is
 *  stranded at "running". The backend's heartbeat fix keeps the row honest,
 *  but the UI must be correct even when a beat slips through. */
const TERMINAL_STAGES = new Set([
  "completed", "failed", "abandoned", "interrupted",
]);

/** A thread is a watchdog candidate when it claims to be active but its
 *  heartbeat has gone stale — the UI shows "nudge / let it run" (§4). */
export function isStaleThread(thread: Thread, now: number): boolean {
  if (thread.status !== "running" && thread.status !== "queued") return false;
  if (!thread.heartbeat_at) return thread.status === "running";
  return now - Date.parse(thread.heartbeat_at) > WATCHDOG_STALE_MS;
}

/** Stale threads for a run, suppressing the watchdog once the run is terminal.
 *  A completed run with a row stranded at "running" (a lost status-change
 *  beat) would otherwise show "heartbeat stale — nudge" forever. */
export function staleThreads(
  threads: Thread[], now: number, stage: string,
): Thread[] {
  if (TERMINAL_STAGES.has(stage)) return [];
  return threads.filter((l) => isStaleThread(l, now));
}

// ------------------------------------------------------------- critical path
/** Critical path (§4 swarm view): the chain whose completion gates the run.
 *  v1 heuristic — among ACTIVE threads, the one carrying the most work (steps)
 *  is the head of the critical path; ties break by oldest thread. Terminal threads
 *  are never on it. */
export function criticalThreadIds(threads: Thread[]): Set<string> {
  const active = threads.filter(
    (l) => l.status === "running" || l.status === "queued" || l.status === "idle",
  );
  if (active.length < 2) return new Set(); // a single thread is just "the thread"
  const head = [...active].sort(
    (a, b) => b.steps - a.steps || (a.created_at ?? "").localeCompare(b.created_at ?? ""),
  )[0];
  return new Set([head.id]);
}

// ------------------------------------------------------------- event merging
export interface StreamItem {
  key: string;
  kind: string;
  title: string;
  text: string;
  threadId: string;
  ok: boolean | null;
  /** Still streaming — drives the open/closed state of collapsible sections. */
  live: boolean;
  /** Who spoke: agent prose vs the user's own message. Drives chat alignment. */
  role: "user" | "agent" | null;
  /** The typed card payload (todo tasks, compaction counts, approval args…). */
  detail: Record<string, unknown>;
}

/** Typed status sub-kinds → display card kinds (RF console parity, §19 card
 *  taxonomy): todo-checklist, compaction card, ⚠ warning, ◆ recap, approvals. */
const DETAIL_KIND_MAP: Record<string, string> = {
  "todo-checklist": "todo_checklist",
  compaction_card: "compaction",
  warning: "warning",
  critic_finding: "warning",
  compaction_rollback: "warning",
  recap: "recap",
  approval_card: "approval",
  approval_decision: "approval",
};

/** Display kind for an event: the typed detail sub-kind wins over the raw
 *  StepKind so typed STATUS events render as their card kinds. */
export function displayKind(e: { kind: string; detail: Record<string, unknown> }): string {
  const sub = typeof e.detail?.kind === "string" ? e.detail.kind : null;
  return (sub && DETAIL_KIND_MAP[sub]) || e.kind;
}

/** Status events are the agent's plumbing, not its progress: every turn emits
 *  session-init / thinking_tokens / turn-complete bookkeeping. Rendering them
 *  makes one healthy reply look like a stack of restarts, so untyped status
 *  events never reach the stream. TYPED status events (todo, compaction,
 *  warning, recap, approval) are progress and DO render (RF). */
function isPlumbing(e: { kind: string; detail?: Record<string, unknown> }): boolean {
  if (e.kind !== "status") return false;
  const sub = typeof e.detail?.kind === "string" ? e.detail.kind : null;
  return !sub || !(sub in DETAIL_KIND_MAP);
}

/** Fold WS typing deltas + stored events into display items. Deltas for the
 *  same (thread, kind) collapse into one growing bubble; stored events pass
 *  through untouched. Pure — tested without React. */
export function foldStream(
  events: { thread_id: string; kind: string; title: string; detail: Record<string, unknown>; seq: number }[],
  deltas: { thread_id: string; kind: string; text: string }[],
): StreamItem[] {
  // The key must distinguish a user-authored message from an agent-authored
  // one even when they share (thread_id, seq) — which happens today because the
  // worker and the backend each run their own seq allocator (worker/worker/
  // normalize.py and backend/app/api/runs.py:_persist_user_message). Without
  // the role in the key, two messages on the same seq collapse into one
  // React component instance and the agent's prose renders inside the
  // user's green bubble.
  const items: StreamItem[] = events.filter((e) => !isPlumbing(e)).map((e) => ({
    key: `e-${e.thread_id}-${e.seq}-${e.detail.role ?? "agent"}`,
    kind: displayKind(e),
    title: e.title,
    text: String(e.detail.text ?? e.detail.output ?? ""),
    threadId: e.thread_id,
    ok: typeof e.detail.ok === "boolean" ? (e.detail.ok as boolean) : null,
    live: false,
    role: e.detail.role === "user" ? "user" : e.detail.role === "agent" ? "agent" : null,
    detail: e.detail,
  }));
  const live = new Map<string, StreamItem>();
  for (const d of deltas) {
    const k = `${d.thread_id}:${d.kind}`;
    const existing = live.get(k);
    if (existing) {
      existing.text += d.text ?? "";
    } else {
      live.set(k, {
        key: `d-${k}`,
        kind: d.kind,
        title: d.kind === "thinking" ? "thinking…" : "typing…",
        text: d.text ?? "",
        threadId: d.thread_id,
        ok: null,
        live: true,
        role: null,
        detail: {},
      });
    }
  }
  return [...items, ...live.values()];
}
