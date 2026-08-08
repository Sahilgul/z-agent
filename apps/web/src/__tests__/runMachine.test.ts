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
  // Fixtures mirror backend/app/services/runs.py ACTIONS_BY_STAGE — the
  // backend NEVER advertises stop_run/abandon_run (the UI hardcodes them).
  it("adds the hardcoded Stop while the agent works (backend advertises nothing)", () => {
    const shown = visibleActions("developing", []);
    expect(shown).toEqual(["stop_run"]);
  });
  it("shows the backend's decisions plus Stop when awaiting the user", () => {
    const shown = visibleActions("awaiting_user", ["review_plan", "approve_plan", "reject_plan"]);
    expect(shown).toEqual(["stop_run", "review_plan", "approve_plan", "reject_plan"]);
  });
  it("never double-renders stop/abandon if a backend version starts advertising them", () => {
    const shown = visibleActions("developing", ["stop_run", "abandon_run", "create_pr"]);
    expect(shown).toEqual(["stop_run", "create_pr"]);
  });
  it("renders nothing on terminal stages (interrupted included)", () => {
    expect(visibleActions("completed", [])).toEqual([]);
    expect(visibleActions("interrupted", ["edit_and_resend", "resume_run"])).toEqual([]);
  });
  it("agentWorking excludes verifying (W-H3 — the human's review turn)", () => {
    expect(agentWorking("verifying")).toBe(false);
    expect(agentWorking("developing")).toBe(true);
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
  // M-89: the queued-no-heartbeat branch was untested. A queued thread is
  // waiting to start, so the absence of a heartbeat is NOT a watchdog
  // failure (only a running thread with no heartbeat is stale).
  it("does not flag a queued thread with no heartbeat", () => {
    expect(isStaleThread({ ...base, status: "queued" }, Date.now())).toBe(false);
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
    // M-88: was vacuous — only one active thread, so active.length<2
    // returned empty regardless of whether the terminal thread was
    // filtered. Now two active + a terminal with the most steps: the
    // terminal must be excluded and the busiest ACTIVE thread picked.
    const ids = criticalThreadIds([
      thread("a", 3),
      thread("b", 5),
      thread("c", 99, "completed"),
    ]);
    expect([...ids]).toEqual(["b"]); // c (terminal, 99 steps) is ignored
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

  // --- message timestamps + "took Ns" ---

  it("carries ts through and measures took-Ns from the preceding user message", () => {
    const items = foldStream(
      [
        { thread_id: "l1", kind: "message", title: "q", detail: { text: "q", role: "user" }, seq: 0, ts: "2026-08-01T00:00:00Z" },
        { thread_id: "l1", kind: "message", title: "a", detail: { text: "a", role: "agent" }, seq: 1, ts: "2026-08-01T00:00:17Z" },
      ],
      [],
    );
    expect(items[0].ts).toBe("2026-08-01T00:00:00Z");
    expect(items[0].durationS).toBeNull(); // the user asks; only the agent "took" time
    expect(items[1].durationS).toBe(17);
  });

  it("scopes took-Ns per thread and skips replies with no preceding user message", () => {
    const items = foldStream(
      [
        { thread_id: "l2", kind: "message", title: "a", detail: { text: "a", role: "agent" }, seq: 0, ts: "2026-08-01T00:00:10Z" },
        { thread_id: "l1", kind: "message", title: "q", detail: { text: "q", role: "user" }, seq: 1, ts: "2026-08-01T00:00:00Z" },
        { thread_id: "l1", kind: "message", title: "a", detail: { text: "a", role: "agent" }, seq: 2, ts: "2026-08-01T00:01:10Z" },
      ],
      [],
    );
    expect(items[0].durationS).toBeNull(); // l2 reply: no user message in l2
    expect(items[2].durationS).toBe(70); // l1 reply measured from l1's question
  });

  it("never shows a negative duration on out-of-order replay", () => {
    const items = foldStream(
      [
        { thread_id: "l1", kind: "message", title: "q", detail: { text: "q", role: "user" }, seq: 0, ts: "2026-08-01T00:00:30Z" },
        { thread_id: "l1", kind: "message", title: "a", detail: { text: "a", role: "agent" }, seq: 1, ts: "2026-08-01T00:00:00Z" },
      ],
      [],
    );
    expect(items[1].durationS).toBeNull();
  });
});
