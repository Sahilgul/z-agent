/** Pure run-state logic (plan §8 lib/runMachine.ts) — the tested heart of the
 *  session: stage presentation, live-state interaction rules (§1a), watchdog
 *  detection, and the swarm critical path. No React imports. */

import type { Lane, RunStage } from "../types";

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

/** A lane is a watchdog candidate when it claims to be active but its
 *  heartbeat has gone stale — the UI shows "nudge / let it run" (§4). */
export function isStaleLane(lane: Lane, now: number): boolean {
  if (lane.status !== "running" && lane.status !== "queued") return false;
  if (!lane.heartbeat_at) return lane.status === "running";
  return now - Date.parse(lane.heartbeat_at) > WATCHDOG_STALE_MS;
}

// ------------------------------------------------------------- critical path
/** Critical path (§4 swarm view): the chain whose completion gates the run.
 *  v1 heuristic — among ACTIVE lanes, the one carrying the most work (steps)
 *  is the head of the critical path; ties break by oldest lane. Terminal lanes
 *  are never on it. */
export function criticalLaneIds(lanes: Lane[]): Set<string> {
  const active = lanes.filter(
    (l) => l.status === "running" || l.status === "queued" || l.status === "idle",
  );
  if (active.length < 2) return new Set(); // a single lane is just "the lane"
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
  laneId: string;
  ok: boolean | null;
  /** Still streaming — drives the open/closed state of collapsible sections. */
  live: boolean;
  /** Who spoke: agent prose vs the user's own message. Drives chat alignment. */
  role: "user" | "agent" | null;
}

/** Fold WS typing deltas + stored events into display items. Deltas for the
 *  same (lane, kind) collapse into one growing bubble; stored events pass
 *  through untouched. Pure — tested without React. */
export function foldStream(
  events: { lane_id: string; kind: string; title: string; detail: Record<string, unknown>; seq: number }[],
  deltas: { lane_id: string; kind: string; text: string }[],
): StreamItem[] {
  const items: StreamItem[] = events.map((e) => ({
    key: `e-${e.lane_id}-${e.seq}`,
    kind: e.kind,
    title: e.title,
    text: String(e.detail.text ?? e.detail.output ?? ""),
    laneId: e.lane_id,
    ok: typeof e.detail.ok === "boolean" ? (e.detail.ok as boolean) : null,
    live: false,
    role: e.detail.role === "user" ? "user" : e.detail.role === "agent" ? "agent" : null,
  }));
  const live = new Map<string, StreamItem>();
  for (const d of deltas) {
    const k = `${d.lane_id}:${d.kind}`;
    const existing = live.get(k);
    if (existing) {
      existing.text += d.text ?? "";
    } else {
      live.set(k, {
        key: `d-${k}`,
        kind: d.kind,
        title: d.kind === "thinking" ? "thinking…" : "typing…",
        text: d.text ?? "",
        laneId: d.lane_id,
        ok: null,
        live: true,
        role: null,
      });
    }
  }
  return [...items, ...live.values()];
}
