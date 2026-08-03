import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { SwarmView } from "../features/swarm/SwarmView";
import type { Lane } from "../types";

const lane = (
  id: string,
  persona = "explorer",
  status: Lane["status"] = "running",
  heartbeat_at: string | null = null,
): Lane => ({
  id, persona, repo_scope: null, status, cost_usd: 0, budget_usd: 5,
  steps: 3, forked_from_session_id: null, heartbeat_at, has_container: true,
  created_at: null, finished_at: null,
});

describe("SwarmView", () => {
  it("hides for a single worker lane (ask mode)", () => {
    const { container } = render(
      <SwarmView
        lanes={[lane("l1", "researcher", "running")]}
        now={Date.now()}
        stage="investigating"
        onOpenLane={() => {}}
        onNudge={() => {}}
        onLetItRun={() => {}}
      />,
    );
    expect(container.querySelector("[data-testid='swarm-view']")).toBeNull();
  });

  it("shows for two or more worker lanes", () => {
    const { container } = render(
      <SwarmView
        lanes={[lane("l1", "explorer"), lane("l2", "explorer")]}
        now={Date.now()}
        stage="investigating"
        onOpenLane={() => {}}
        onNudge={() => {}}
        onLetItRun={() => {}}
      />,
    );
    expect(container.querySelector("[data-testid='swarm-view']")).not.toBeNull();
  });

  it("shows when a lead lane is present (orchestrated fan-out)", () => {
    const { container } = render(
      <SwarmView
        lanes={[lane("lead", "lead", "running"), lane("l1", "researcher")]}
        now={Date.now()}
        stage="investigating"
        onOpenLane={() => {}}
        onNudge={() => {}}
        onLetItRun={() => {}}
      />,
    );
    expect(container.querySelector("[data-testid='swarm-view']")).not.toBeNull();
  });

  it("suppresses the watchdog banner on a terminal run even with a stale lane", () => {
    // A completed run with a row stranded at "running" (lost status-change
    // beat) must not nag the user to nudge a finished lane.
    const stale = lane("l1", "explorer", "running", new Date(0).toISOString());
    const { container } = render(
      <SwarmView
        lanes={[stale, lane("l2", "explorer")]}
        now={Date.now()}
        stage="completed"
        onOpenLane={() => {}}
        onNudge={() => {}}
        onLetItRun={() => {}}
      />,
    );
    expect(container.querySelector("[data-testid='watchdog-banner']")).toBeNull();
  });

  it("shows the watchdog banner for a stale lane on an active run", () => {
    const stale = lane("l1", "explorer", "running", new Date(0).toISOString());
    const { container } = render(
      <SwarmView
        lanes={[stale, lane("l2", "explorer")]}
        now={Date.now()}
        stage="investigating"
        onOpenLane={() => {}}
        onNudge={() => {}}
        onLetItRun={() => {}}
      />,
    );
    expect(container.querySelector("[data-testid='watchdog-banner']")).not.toBeNull();
  });
});
