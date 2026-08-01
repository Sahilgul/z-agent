/** Hand-mirrors of zagent_contracts (packages/contracts) — keep in sync. */

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

export interface Lane {
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
  | "test_run" | "message" | "notebook" | "status";

export interface StepEvent {
  schema_version: number;
  run_id: string;
  lane_id: string;
  seq: number;
  ts: string;
  kind: StepKind;
  title: string;
  detail: Record<string, unknown>;
  sdk_message_uuid: string | null;
}

export type WsMessage =
  | { type: "step"; event: StepEvent }
  | { type: "lane_status"; lane_id: string; status: string }
  | { type: "run_stage"; stage: RunStage; available_actions: string[] }
  | { type: "delta"; run_id: string; lane_id: string; kind: StepKind; text: string };

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
  lane_id: string;
  tool: string;
  input: Record<string, unknown>;
  status: string;
  created_at: string | null;
}

export interface Me {
  id: number;
  username: string;
  display_name: string;
  role: string;
  must_change_pin: boolean;
}
