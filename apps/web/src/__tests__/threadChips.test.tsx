import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ThreadChips } from "../components/ThreadChips";
import type { Thread } from "../types";

const thread = (
  id: string,
  persona = "explorer",
  status: Thread["status"] = "running",
): Thread => ({
  id, persona, repo_scope: null, status, cost_usd: 0, budget_usd: 5,
  steps: 3, forked_from_session_id: null, heartbeat_at: null,
  has_container: true, created_at: null, finished_at: null,
});

describe("ThreadChips", () => {
  it("hides for a single worker thread (ask mode)", () => {
    const { container } = render(
      <ThreadChips threads={[thread("l1", "researcher")]} onOpen={() => {}} />,
    );
    expect(container.querySelector("[data-testid='thread-chips']")).toBeNull();
  });

  it("renders one chip per worker thread for a real swarm", () => {
    render(
      <ThreadChips
        threads={[thread("l1", "explorer"), thread("l2", "researcher")]}
        onOpen={() => {}}
      />,
    );
    expect(screen.getByText("explorer")).toBeInTheDocument();
    expect(screen.getByText("researcher")).toBeInTheDocument();
  });

  it("renders a lead chip when a lead thread is present", () => {
    render(
      <ThreadChips
        threads={[thread("lead", "lead"), thread("l1", "researcher")]}
        onOpen={() => {}}
      />,
    );
    expect(screen.getByText("lead")).toBeInTheDocument();
    expect(screen.getByText("researcher")).toBeInTheDocument();
  });
});
