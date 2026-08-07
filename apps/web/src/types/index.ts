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
  cost_usd: number;
  tokens: number;
  last_active_at: string | null;
  created_at: string | null;
}

export type LaneStatus =
  | "queued" | "running" | "idle" | "interrupted" | "completed"
  | "failed" | "stopped" | "replaced" | "pinned";

export interface Thread {
  id: string;
  persona: string;
  repo_scope: string | null;
  status: LaneStatus;
  cost_usd: number;
  budget_usd: number;
  steps: number;
  forked_from_session_id: string | null;
  heartbeat_at: string | null;
  has_container: boolean;
  created_at: string | null;
  finished_at: string | null;
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
  | { type: "note"; text: string };

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
  must_change_pin: boolean;
}

/** Logical destinations — the rail's IA. Routed via react-router; see
 *  lib/routes.ts for the screen → URL map. */
export type Screen =
  | "sessions" | "knowledge" | "ideas"
  | "proposals" | "dashboard" | "repos" | "team";
