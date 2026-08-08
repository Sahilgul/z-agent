/** Exit evidence: the 4 previously-missing card kinds
 *  (todo-checklist, compaction, ⚠ warning, ◆ recap) + the dedicated approval
 *  kind render from LIVE-shaped StepEvents in the production EventStream.
 *  W7-L1: the Feed surface was deleted — EventStream is the only console. */

import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { EventStream } from "../components/EventStream";
import type { StepEvent } from "../types";

const ev = (seq: number, kind: StepEvent["kind"], title: string, detail: Record<string, unknown>): StepEvent => ({
  run_id: "r1",
  thread_id: "t1",
  seq,
  ts: new Date().toISOString(),
  kind,
  title,
  detail,
  sdk_message_uuid: null,
});

// Live-shaped payloads exactly as the engine emits them.
const TODO = ev(1, "command", "update_tasks", {
  kind: "todo-checklist",
  tasks: {
    artifact: [
      { id: "t1", content: "wire the gate" },
      { id: "t2", content: "write tests" },
    ],
    tracker: { t1: "completed", t2: "in_progress" },
  },
});
const COMPACTION = ev(2, "status", "compaction", {
  kind: "compaction_card", pruned: 4, summarized: 2, kept: 9,
  before_tokens: 12000, after_tokens: 3100, forced: false,
});
const WARNING = ev(3, "status", "⚠ same failing call 3x", {
  kind: "warning", warning: "stuck_loop", detail: "same failing call 3x",
});
const RECAP = ev(4, "status", "◆ recap: stage plan", {
  kind: "recap", stage: "plan", summary: "Stage advanced to plan. Goal: add rate limiting",
});
const APPROVAL_CARD = ev(5, "approval", "approval: terminal_exec", {
  kind: "approval_card", action_id: "ap-tc1", approval_id: "ap-tc1",
  tool: "terminal_exec", args: { command: "git push origin collegium/x" },
  destructive: true, always_allowable: false,
});
const APPROVAL_DECISION = ev(6, "approval", "approval edited_allow", {
  kind: "approval_decision", action_id: "ap-tc1", approval_id: "ap-tc1",
  decision: "edited_allow", edited: true,
});

describe("EventStream — card parity from live StepEvents", () => {
  it("renders the todo-checklist card with checkbox states", () => {
    render(<EventStream events={[TODO]} deltas={[]} />);
    expect(screen.getByTestId("todo-checklist")).toBeInTheDocument();
    expect(screen.getByText("wire the gate")).toBeInTheDocument();
    expect(screen.getByText("write tests")).toBeInTheDocument();
    expect(screen.getByText("☑")).toBeInTheDocument();
    expect(screen.getByText("▸")).toBeInTheDocument();
  });

  it("renders the compaction card with counts and token delta", () => {
    render(<EventStream events={[COMPACTION]} deltas={[]} />);
    const card = screen.getByTestId("compaction-card");
    expect(card.textContent).toContain("pruned 4");
    expect(card.textContent).toContain("12000 → 3100 tokens");
  });

  it("renders the ⚠ warning card", () => {
    render(<EventStream events={[WARNING]} deltas={[]} />);
    expect(screen.getByTestId("warning-card").textContent).toContain("same failing call 3x");
  });

  it("renders the ◆ recap block", () => {
    render(<EventStream events={[RECAP]} deltas={[]} />);
    expect(screen.getByTestId("recap-card").textContent).toContain("Stage advanced to plan");
  });

  it("renders approval card verbatim with action_id pairing + destructive badge", () => {
    const { container } = render(<EventStream events={[APPROVAL_CARD, APPROVAL_DECISION]} deltas={[]} />);
    const cards = container.querySelectorAll('[data-testid="approval-card"]');
    expect(cards).toHaveLength(2);
    // action_id pairing: card and decision share the same action id.
    expect(cards[0].getAttribute("data-action-id")).toBe("ap-tc1");
    expect(cards[1].getAttribute("data-action-id")).toBe("ap-tc1");
    // VERBATIM args, never paraphrased.
    expect(cards[0].textContent).toContain("git push origin collegium/x");
    expect(cards[0].textContent).toContain("destructive");
    expect(cards[1].textContent).toContain("edited_allow");
    expect(cards[1].textContent).toContain("edited");
  });

  it("approval card shows the clean command, not the raw JSON wire dump", () => {
    render(<EventStream events={[APPROVAL_CARD]} deltas={[]} />);
    const cmd = screen.getByTestId("approval-command");
    expect(cmd.textContent).toContain("git push origin collegium/x");
    // the compact {"command": ...} serialization is gone from the card
    expect(screen.getByTestId("approval-card").textContent).not.toContain('{"command"');
    // and a shell command gets the terminal chrome
    expect(screen.getByTestId("terminal-frame")).toBeInTheDocument();
  });

  it("approval card renders the cmd alias as a command, not raw JSON", () => {
    // Models habitually call terminal_exec with cmd= (other harnesses' key);
    // pre-normalization transcripts persist it. The card must still render
    // the terminal treatment, never the wire dump.
    const aliased = ev(7, "approval", "approval: terminal_exec", {
      kind: "approval_card", action_id: "ap-tc2", approval_id: "ap-tc2",
      tool: "terminal_exec", args: { cmd: "pwd && ls -la", timeout_ms: 30000 },
    });
    render(<EventStream events={[aliased]} deltas={[]} />);
    expect(screen.getByTestId("approval-command").textContent).toContain("pwd && ls -la");
    expect(screen.getByTestId("approval-card").textContent).not.toContain('"cmd"');
    expect(screen.getByTestId("terminal-frame")).toBeInTheDocument();
  });
});
