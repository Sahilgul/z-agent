import { fireEvent, screen, waitFor } from "@testing-library/react";
import { renderScreen } from "./render";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ProposalsScreen } from "../features/proposals/ProposalsScreen";

const get = vi.fn();
const post = vi.fn();
vi.mock("../lib/api", () => ({
  api: { get: (...a: unknown[]) => get(...a), post: (...a: unknown[]) => post(...a) },
}));

const items = [
  {
    id: 1, source: "janitor", repo: "ClientApp", title: "Dead code in billing",
    body: "utils.ts re-export chain is unreferenced", evidence: ["ClientApp/src/lib/utils.ts:14"],
    impact: "high", confidence: "medium", rank_score: 6, status: "proposed",
    promoted_run_id: null, created_at: "2026-08-01T00:00:00Z",
  },
  {
    id: 2, source: "perfector", repo: null, title: "Batch hydration endpoints",
    body: "two tickets hit the same cold path", evidence: ["ServerApp/areas/hydration.py:40"],
    impact: "medium", confidence: "high", rank_score: 6, status: "proposed",
    promoted_run_id: null, created_at: "2026-08-01T01:00:00Z",
  },
];

describe("ProposalsScreen", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    get.mockResolvedValue({ items });
    post.mockResolvedValue({ status: "accepted", run_id: "r-1" });
  });

  it("lists ranked proposals with source, levels, and evidence", async () => {
    renderScreen(<ProposalsScreen />);
    expect(await screen.findByText("Dead code in billing")).toBeInTheDocument();
    expect(screen.getByText("janitor")).toBeInTheDocument();
    expect(screen.getByText("perfector")).toBeInTheDocument();
    expect(screen.getByText("impact high")).toBeInTheDocument();
    expect(screen.getByText("ClientApp/src/lib/utils.ts:14")).toBeInTheDocument();
  });

  it("accept posts to the accept endpoint and reloads", async () => {
    renderScreen(<ProposalsScreen />);
    const buttons = await screen.findAllByRole("button", { name: "accept" });
    fireEvent.click(buttons[0]);
    await waitFor(() => expect(post).toHaveBeenCalledWith("/proposals/1/accept", {}));
    await waitFor(() => expect(get).toHaveBeenCalledTimes(2));
  });

  it("dismiss posts to the dismiss endpoint", async () => {
    renderScreen(<ProposalsScreen />);
    const buttons = await screen.findAllByRole("button", { name: "dismiss" });
    fireEvent.click(buttons[1]);
    await waitFor(() => expect(post).toHaveBeenCalledWith("/proposals/2/dismiss", {}));
  });

  it("shows decided status when show-all is enabled", async () => {
    get.mockResolvedValue({
      items: [{ ...items[0], status: "accepted", promoted_run_id: "abcdef123456" }],
    });
    renderScreen(<ProposalsScreen />);
    fireEvent.click(screen.getByRole("checkbox"));
    await waitFor(() => expect(get).toHaveBeenCalledWith("/proposals?status="));
    expect(await screen.findByText("accepted")).toBeInTheDocument();
    expect(screen.getByText("run abcdef12")).toBeInTheDocument();
  });
});
