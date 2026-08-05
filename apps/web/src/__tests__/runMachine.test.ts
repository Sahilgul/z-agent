import { describe, expect, it } from "vitest";
import {
  agentWorking,
  criticalThreadIds,
  foldStream,
  isStaleThread,
  stageMeta,
  visibleActions,
  WATCHDOG_STALE_MS,
} from "../lib/runMachine";
import type { Thread } from "../types";

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

describe("visibleActions (live-state rules)", () => {
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

describe("isStaleThread (watchdog)", () => {
  const base: Thread = {
    id: "l1", persona: "explorer", repo_scope: null, status: "running",
    cost_usd: 0, budget_usd: 5, steps: 3, forked_from_session_id: null,
    heartbeat_at: null, has_container: true, created_at: null, finished_at: null,
  };
  it("flags a running thread with no heartbeat at all", () => {
    expect(isStaleThread(base, Date.now())).toBe(true);
  });
  it("flags a heartbeat older than the stale window", () => {
    const now = Date.now();
    const thread = { ...base, heartbeat_at: new Date(now - WATCHDOG_STALE_MS - 1000).toISOString() };
    expect(isStaleThread(thread, now)).toBe(true);
  });
  it("never flags terminal or fresh threads", () => {
    const now = Date.now();
    expect(isStaleThread({ ...base, status: "completed" }, now)).toBe(false);
    expect(isStaleThread({ ...base, heartbeat_at: new Date(now).toISOString() }, now)).toBe(false);
  });
});

describe("criticalThreadIds", () => {
  const thread = (id: string, steps: number, status: Thread["status"] = "running"): Thread => ({
    id, persona: "explorer", repo_scope: null, status, cost_usd: 0, budget_usd: 5,
    steps, forked_from_session_id: null, heartbeat_at: null, has_container: true,
    created_at: null, finished_at: null,
  });
  it("picks the busiest active thread", () => {
    const ids = criticalThreadIds([thread("a", 3), thread("b", 9), thread("c", 1)]);
    expect([...ids]).toEqual(["b"]);
  });
  it("ignores terminal threads even when they have more steps", () => {
    const ids = criticalThreadIds([thread("a", 3), thread("b", 99, "completed")]);
    expect(ids.size).toBe(0); // only one active thread left → just "the thread"
  });
  it("empty for a single thread", () => {
    expect(criticalThreadIds([thread("a", 5)]).size).toBe(0);
  });
});

describe("foldStream", () => {
  it("collapses deltas by thread+kind into one growing bubble", () => {
    const items = foldStream([], [
      { thread_id: "l1", kind: "thinking", text: "let me " },
      { thread_id: "l1", kind: "thinking", text: "check" },
      { thread_id: "l1", kind: "message", text: "answer" },
    ]);
    expect(items).toHaveLength(2);
    expect(items[0].text).toBe("let me check");
    expect(items[1].kind).toBe("message");
  });
  it("keeps stored events in order before live bubbles", () => {
    const items = foldStream(
      [{ thread_id: "l1", kind: "command", title: "grep x", detail: { output: "hit" }, seq: 0 }],
      [{ thread_id: "l1", kind: "thinking", text: "…" }],
    );
    expect(items.map((i) => i.kind)).toEqual(["command", "thinking"]);
    expect(items[0].text).toBe("hit");
  });
  it("gives distinct keys to user and agent messages sharing a seq", () => {
    // The worker and the backend each allocate seq independently, so a user
    // message and an agent message can land on the same (thread_id, seq). The
    // key must distinguish them or React reconciles them onto one component
    // and the agent's prose renders inside the user's bubble.
    const items = foldStream(
      [
        { thread_id: "l1", kind: "message", title: "q", detail: { text: "my q", role: "user" }, seq: 0 },
        { thread_id: "l1", kind: "message", title: "a", detail: { text: "my a", role: "agent" }, seq: 0 },
      ],
      [],
    );
    expect(items).toHaveLength(2);
    expect(new Set(items.map((i) => i.key)).size).toBe(2);
    expect(items[0].role).toBe("user");
    expect(items[1].role).toBe("agent");
  });

  // --- console parity: typed status events render as their card kinds ---

  it("maps typed status events to their display card kinds", () => {
    const items = foldStream(
      [
        { thread_id: "l1", kind: "status", title: "tasks", seq: 1, detail: { kind: "todo-checklist", tasks: { artifact: [], tracker: {} } } },
        { thread_id: "l1", kind: "status", title: "compaction", seq: 2, detail: { kind: "compaction_card", pruned: 3 } },
        { thread_id: "l1", kind: "status", title: "⚠ stuck", seq: 3, detail: { kind: "warning", detail: "same failing call 3x" } },
        { thread_id: "l1", kind: "status", title: "◆ recap", seq: 4, detail: { kind: "recap", stage: "plan", summary: "advanced" } },
      ],
      [],
    );
    expect(items.map((i) => i.kind)).toEqual(["todo_checklist", "compaction", "warning", "recap"]);
  });

  it("drops untyped status plumbing but keeps typed status events", () => {
    const items = foldStream(
      [
        { thread_id: "l1", kind: "status", title: "session-init", detail: {}, seq: 0 },
        { thread_id: "l1", kind: "status", title: "turn-complete", detail: { kind: "turn_boundary" }, seq: 1 },
        { thread_id: "l1", kind: "status", title: "⚠ budget 80%", detail: { kind: "warning", detail: "budget 80% used" }, seq: 2 },
      ],
      [],
    );
    expect(items).toHaveLength(1);
    expect(items[0].kind).toBe("warning");
  });

  it("passes approval events through with action_id intact", () => {
    const items = foldStream(
      [
        { thread_id: "l1", kind: "approval", title: "approval: terminal_exec", seq: 5, detail: { kind: "approval_card", action_id: "ap-1", tool: "terminal_exec", args: { command: "git push" } } },
        { thread_id: "l1", kind: "approval", title: "approval allow", seq: 6, detail: { kind: "approval_decision", action_id: "ap-1", decision: "allow" } },
      ],
      [],
    );
    expect(items.map((i) => i.kind)).toEqual(["approval", "approval"]);
    expect(items[0].detail.action_id).toBe("ap-1");
    expect(items[1].detail.action_id).toBe(items[0].detail.action_id);
  });
});
