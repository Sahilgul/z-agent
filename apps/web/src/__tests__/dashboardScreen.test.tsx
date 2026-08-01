import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { DashboardScreen } from "../features/dashboard/DashboardScreen";

const get = vi.fn();
vi.mock("../lib/api", () => ({
  api: { get: (...a: unknown[]) => get(...a) },
}));

const dash = {
  days: 30,
  total: { cost_usd: 3.5, tokens: 1750, runs: 3 },
  by_day: { "2026-08-01": { cost_usd: 3.5, tokens: 1750, runs: 3 } },
  by_mode: { development: { cost_usd: 3.0, tokens: 1500, runs: 2 }, ask: { cost_usd: 0.5, tokens: 250, runs: 1 } },
  by_repo: { ServerApp: { cost_usd: 2.5, tokens: 1250, runs: 2 } },
  by_user: { "Ali Raza": { cost_usd: 3.5, tokens: 1750, runs: 3 } },
};

const deliveries = {
  items: [
    { id: 1, title: "logging migration", runs: 2,
      stages: { developing: 1, completed: 1 }, cost_usd: 2.0,
      prs: [{ repo: "ServerApp", ado_pr_id: 42, status: "open" }] },
  ],
};

describe("DashboardScreen", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    get.mockImplementation((url: string) =>
      Promise.resolve(url.startsWith("/stats") ? dash : deliveries)
    );
  });

  it("renders totals and bucket bars", async () => {
    render(<DashboardScreen />);
    expect(await screen.findByTestId("dash-total")).toHaveTextContent("$3.50");
    expect(await screen.findByTestId("bar-by mode-development")).toBeInTheDocument();
    expect(screen.getByTestId("bar-by repo-ServerApp")).toBeInTheDocument();
    expect(screen.getByTestId("bar-by teammate-Ali Raza")).toBeInTheDocument();
  });

  it("renders campaign rollups with stages and PRs", async () => {
    render(<DashboardScreen />);
    expect(await screen.findByText("logging migration")).toBeInTheDocument();
    expect(screen.getByText("developing ×1")).toBeInTheDocument();
    expect(screen.getByText("ServerApp PR 42 · open")).toBeInTheDocument();
  });

  it("requests the 30-day cost window and deliveries", async () => {
    render(<DashboardScreen />);
    await screen.findByTestId("dash-total");
    expect(get).toHaveBeenCalledWith("/stats/cost?days=30");
    expect(get).toHaveBeenCalledWith("/deliveries");
  });
});
