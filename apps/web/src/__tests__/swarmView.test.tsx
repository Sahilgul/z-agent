import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { SwarmView } from "../features/swarm/SwarmView";
import type { Thread } from "../types";

const thread = (
  id: string,
  persona = "explorer",
  status: Thread["status"] = "running",
  heartbeat_at: string | null = null,
): Thread => ({
  id, persona, repo_scope: null, status, cost_usd: 0, budget_usd: 5,
  steps: 3, forked_from_session_id: null, heartbeat_at, has_container: true,
  created_at: null, finished_at: null,
});

describe("SwarmView", () => {
  it("hides for a single worker thread (ask mode)", () => {
    const { container } = render(
      <SwarmView
        threads={[thread("l1", "researcher", "running")]}
        now={Date.now()}
        stage="investigating"
        onOpenThread={() => {}}
        onNudge={() => {}}
        onLetItRun={() => {}}
      />,
    );
    expect(container.querySelector("[data-testid='swarm-view']")).toBeNull();
  });

  it("shows for two or more worker threads", () => {
    const { container } = render(
      <SwarmView
        threads={[thread("l1", "explorer"), thread("l2", "explorer")]}
        now={Date.now()}
        stage="investigating"
        onOpenThread={() => {}}
        onNudge={() => {}}
        onLetItRun={() => {}}
      />,
    );
    expect(container.querySelector("[data-testid='swarm-view']")).not.toBeNull();
  });

  it("shows when a lead thread is present (orchestrated fan-out)", () => {
    const { container } = render(
      <SwarmView
        threads={[thread("lead", "lead", "running"), thread("l1", "researcher")]}
        now={Date.now()}
        stage="investigating"
        onOpenThread={() => {}}
        onNudge={() => {}}
        onLetItRun={() => {}}
      />,
    );
    expect(container.querySelector("[data-testid='swarm-view']")).not.toBeNull();
  });

  it("suppresses the watchdog banner on a terminal run even with a stale thread", () => {
    // A completed run with a row stranded at "running" (lost status-change
    // beat) must not nag the user to nudge a finished thread.
    const stale = thread("l1", "explorer", "running", new Date(0).toISOString());
    const { container } = render(
      <SwarmView
        threads={[stale, thread("l2", "explorer")]}
        now={Date.now()}
        stage="completed"
        onOpenThread={() => {}}
        onNudge={() => {}}
        onLetItRun={() => {}}
      />,
    );
    expect(container.querySelector("[data-testid='watchdog-banner']")).toBeNull();
  });

  it("shows the watchdog banner for a stale thread on an active run", () => {
    const stale = thread("l1", "explorer", "running", new Date(0).toISOString());
    const { container } = render(
      <SwarmView
        threads={[stale, thread("l2", "explorer")]}
        now={Date.now()}
        stage="investigating"
        onOpenThread={() => {}}
        onNudge={() => {}}
        onLetItRun={() => {}}
      />,
    );
    expect(container.querySelector("[data-testid='watchdog-banner']")).not.toBeNull();
  });
});
