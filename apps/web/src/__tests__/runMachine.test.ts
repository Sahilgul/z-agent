import { describe, expect, it } from "vitest";
import {
  agentWorking,
  criticalLaneIds,
  foldStream,
  isStaleLane,
  stageMeta,
  visibleActions,
  WATCHDOG_STALE_MS,
} from "../lib/runMachine";
import type { Lane } from "../types";

describe("stageMeta", () => {
  it("maps rail stages to indexes", () => {
    expect(stageMeta("investigating").index).toBe(2);
    expect(stageMeta("completed").tone).toBe("ok");
  });
  it("treats failed/abandoned as off-rail danger", () => {
    expect(stageMeta("failed").index).toBe(-1);
    expect(stageMeta("abandoned").tone).toBe("danger");
  });
  it("interrupted reads as preserved, not failed", () => {
    expect(stageMeta("interrupted").label).toContain("preserved");
    expect(stageMeta("interrupted").tone).toBe("warn");
  });
});

describe("visibleActions (§1a live-state rules)", () => {
  it("hides decision buttons while the agent works, keeps Stop", () => {
    const shown = visibleActions("developing", ["stop_run", "create_pr", "abandon_run"]);
    expect(shown).toEqual(["stop_run"]);
  });
  it("shows everything when awaiting the user", () => {
    const shown = visibleActions("awaiting_user", ["approve_plan", "reject_plan", "stop_run"]);
    expect(shown).toEqual(["approve_plan", "reject_plan", "stop_run"]);
  });
  it("agentWorking matches the same stage set", () => {
    expect(agentWorking("verifying")).toBe(true);
    expect(agentWorking("awaiting_user")).toBe(false);
    expect(agentWorking("completed")).toBe(false);
  });
});

describe("isStaleLane (watchdog)", () => {
  const base: Lane = {
    id: "l1", persona: "explorer", repo_scope: null, status: "running",
    cost_usd: 0, budget_usd: 5, steps: 3, forked_from_session_id: null,
    heartbeat_at: null, has_container: true, created_at: null, finished_at: null,
  };
  it("flags a running lane with no heartbeat at all", () => {
    expect(isStaleLane(base, Date.now())).toBe(true);
  });
  it("flags a heartbeat older than the stale window", () => {
    const now = Date.now();
    const lane = { ...base, heartbeat_at: new Date(now - WATCHDOG_STALE_MS - 1000).toISOString() };
    expect(isStaleLane(lane, now)).toBe(true);
  });
  it("never flags terminal or fresh lanes", () => {
    const now = Date.now();
    expect(isStaleLane({ ...base, status: "completed" }, now)).toBe(false);
    expect(isStaleLane({ ...base, heartbeat_at: new Date(now).toISOString() }, now)).toBe(false);
  });
});

describe("criticalLaneIds", () => {
  const lane = (id: string, steps: number, status: Lane["status"] = "running"): Lane => ({
    id, persona: "explorer", repo_scope: null, status, cost_usd: 0, budget_usd: 5,
    steps, forked_from_session_id: null, heartbeat_at: null, has_container: true,
    created_at: null, finished_at: null,
  });
  it("picks the busiest active lane", () => {
    const ids = criticalLaneIds([lane("a", 3), lane("b", 9), lane("c", 1)]);
    expect([...ids]).toEqual(["b"]);
  });
  it("ignores terminal lanes even when they have more steps", () => {
    const ids = criticalLaneIds([lane("a", 3), lane("b", 99, "completed")]);
    expect(ids.size).toBe(0); // only one active lane left → just "the lane"
  });
  it("empty for a single lane", () => {
    expect(criticalLaneIds([lane("a", 5)]).size).toBe(0);
  });
});

describe("foldStream", () => {
  it("collapses deltas by lane+kind into one growing bubble", () => {
    const items = foldStream([], [
      { lane_id: "l1", kind: "thinking", text: "let me " },
      { lane_id: "l1", kind: "thinking", text: "check" },
      { lane_id: "l1", kind: "message", text: "answer" },
    ]);
    expect(items).toHaveLength(2);
    expect(items[0].text).toBe("let me check");
    expect(items[1].kind).toBe("message");
  });
  it("keeps stored events in order before live bubbles", () => {
    const items = foldStream(
      [{ lane_id: "l1", kind: "command", title: "grep x", detail: { output: "hit" }, seq: 0 }],
      [{ lane_id: "l1", kind: "thinking", text: "…" }],
    );
    expect(items.map((i) => i.kind)).toEqual(["command", "thinking"]);
    expect(items[0].text).toBe("hit");
  });
});
