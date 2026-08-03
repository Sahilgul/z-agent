import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { LaneChips } from "../components/LaneChips";
import type { Lane } from "../types";

const lane = (
  id: string,
  persona = "explorer",
  status: Lane["status"] = "running",
): Lane => ({
  id, persona, repo_scope: null, status, cost_usd: 0, budget_usd: 5,
  steps: 3, forked_from_session_id: null, heartbeat_at: null,
  has_container: true, created_at: null, finished_at: null,
});

describe("LaneChips", () => {
  it("hides for a single worker lane (ask mode)", () => {
    const { container } = render(
      <LaneChips lanes={[lane("l1", "researcher")]} onOpen={() => {}} />,
    );
    expect(container.querySelector("[data-testid='lane-chips']")).toBeNull();
  });

  it("renders one chip per worker lane for a real swarm", () => {
    render(
      <LaneChips
        lanes={[lane("l1", "explorer"), lane("l2", "researcher")]}
        onOpen={() => {}}
      />,
    );
    expect(screen.getByText("explorer")).toBeInTheDocument();
    expect(screen.getByText("researcher")).toBeInTheDocument();
  });

  it("renders a lead chip when a lead lane is present", () => {
    render(
      <LaneChips
        lanes={[lane("lead", "lead"), lane("l1", "researcher")]}
        onOpen={() => {}}
      />,
    );
    expect(screen.getByText("lead")).toBeInTheDocument();
    expect(screen.getByText("researcher")).toBeInTheDocument();
  });
});
