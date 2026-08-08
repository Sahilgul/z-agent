/** Hand-mirrors of collegium_contracts (packages/contracts) — keep in sync. */

export type RunStage =
  | "queued" | "provisioning" | "investigating" | "planning" | "awaiting_user"
  | "developing" | "verifying" | "pr_ready" | "interrupted" | "completed"
  | "failed" | "abandoned";

export interface Run {
  id: string;
  mode: string;
  autonomy: string;
  stage: RunStage;
  title: string;
  auto_summary: string | null;
  repo: string | null;
  work_item_id: number | null;
  available_actions: string[];
  /** WHY the run failed — persisted backend-side so the banner survives a reload. */
  failure_reason: string | null;
  cost_usd: number;
  tokens: number;
  last_active_at: string | null;
  created_at: string | null;
}

export type LaneStatus =
  | "queued" | "running" | "idle" | "interrupted" | "completed"
  // W5-L2: "pinned" removed — it was a cosmetic WS flash, never a row state;
  // pins land as durable run events with a run note now.
  | "failed" | "stopped" | "replaced" | "input_required";

export interface Thread {
  id: string;
  persona: string;
  repo_scope: string | null;
  status: LaneStatus;
  cost_usd: number;
  budget_usd: number;
  steps: number;
  forked_from_session_id: string | null;
  /** Gateway alias the lane runs on (spawn_context); null on pre-selection rows. */
  model?: string | null;
  heartbeat_at: string | null;
  has_container: boolean;
  created_at: string | null;
  finished_at: string | null;
}

/** One selectable model in the composer dropdown (GET /models). */
export interface ModelOption {
  alias: string;
  label: string;
  price_in_per_mtok: number;
  price_out_per_mtok: number;
  cache_read_per_mtok: number | null;
  /** reasoning_effort values the model accepts; empty = on/off toggle only. */
  reasoning_efforts: string[];
}

export type StepKind =
  | "thinking" | "command" | "file_read" | "file_edit" | "mcp_call"
  | "test_run" | "message" | "notebook" | "status" | "approval";

export interface StepEvent {
  /** Present on the live WS payload; replay rows are serialized without it. */
  schema_version?: number;
  run_id: string;
  thread_id: string;
  seq: number;
  ts: string;
  kind: StepKind;
  title: string;
  detail: Record<string, unknown>;
  sdk_message_uuid: string | null;
}

export type WsMessage =
  | { type: "step"; event: StepEvent }
  | { type: "thread_status"; thread_id: string; status: string }
  | { type: "run_stage"; stage: RunStage; available_actions: string[] }
  // The relay wraps typing deltas in an envelope; the payload is one level down.
  | { type: "delta"; delta: { run_id: string; thread_id: string; kind: StepKind; text: string } }
  | {
      type: "approval_card";
      approval: { id: string; kind: string; payload: Record<string, unknown>; thread_id: string | null };
    }
  | { type: "approval_resolved"; approval_id: string; decision: string }
  // L-22: a run-scoped informational note (e.g. swarm capped a fanout
  // request). Surfaced as a transient toast.
  | { type: "note"; text: string }
  // W1-L1 / W9-L10: a repo finished onboarding (publish_global fans it out
  // over every open run socket) — the rack invalidates its query.
  | { type: "repo_added"; repo: string };

export interface PlanStep {
  id: number;
  index: number;
  title: string;
  description: string;
  repo: string | null;
  files: string[];
  success_criterion: string;
  status: string;
}

export interface PlanPayload {
  id: number;
  run_id: string;
  status: string;
  structured: { title?: string; steps?: unknown[]; critic_notes?: string[] } & Record<string, unknown>;
  decided_by: number | null;
  decided_at: string | null;
  created_at: string | null;
  steps: PlanStep[];
}

export interface Approval {
  id: string;
  run_id: string;
  thread_id: string | null;
  kind: string;
  payload: Record<string, unknown>;
  created_at: string | null;
  /** When the worker stops waiting and denies on its own. */
  expires_at?: string | null;
}

export interface ResumableThread {
  thread_id: string;
  persona: string;
  resumable: boolean;
}

export interface Me {
  id: number;
  username: string;
  display_name: string;
  role: string;
  // W-H15: `must_change_pin` deleted — the backend never returned it, so any
  // guard on it was dead. First-login onboarding is its own route now.
}

/** Logical destinations — the rail's IA. Routed via react-router; see
 *  lib/routes.ts for the screen → URL map. */
export type Screen =
  | "sessions" | "knowledge" | "ideas"
  | "proposals" | "dashboard" | "repos" | "team";
