import { fireEvent, screen, waitFor } from "@testing-library/react";
import { renderScreen } from "./render";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { TeamScreen } from "../features/team/TeamScreen";

const get = vi.fn();
const post = vi.fn();
vi.mock("../lib/api", () => ({
  api: { get: (...a: unknown[]) => get(...a), post: (...a: unknown[]) => post(...a) },
}));

const users = [
  { id: 1, username: "sahil", display_name: "Sahil", role: "admin", status: "active",
    ado_email: "sahil@org.com", ado_bound: true },
  { id: 2, username: "ali.r", display_name: "Ali Raza", role: "member", status: "pending",
    ado_email: "ali.r@org.com", ado_bound: false },
];

const stats = { total_runs: 12, runs_by_stage: { completed: 10 }, runs_by_mode: { ask: 8, development: 4 }, total_cost_usd: 3.5 };

describe("TeamScreen", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    get.mockImplementation((url: string) =>
      Promise.resolve(url === "/team/users" ? users : stats)
    );
    post.mockResolvedValue({ setup_code: "ZT-9F3K2" });
  });

  it("lists users with roles, status, and ADO binding", async () => {
    renderScreen(<TeamScreen />);
    expect(await screen.findByTestId("user-sahil")).toBeInTheDocument();
    expect(screen.getByTestId("user-ali.r")).toBeInTheDocument();
    expect(screen.getByText("bound")).toBeInTheDocument();
    expect(screen.getByText("pending")).toBeInTheDocument();
  });

  it("add teammate shows the one-time code", async () => {
    renderScreen(<TeamScreen />);
    await screen.findByTestId("user-sahil");
    fireEvent.change(screen.getByPlaceholderText("username"), { target: { value: "new.dev" } });
    fireEvent.click(screen.getByRole("button", { name: "add" }));
    await waitFor(() =>
      expect(post).toHaveBeenCalledWith("/team/users", {
        username: "new.dev", display_name: "", ado_email: "",
      })
    );
    expect(await screen.findByTestId("one-time-code")).toHaveTextContent("ZT-9F3K2");
  });

  it("regenerate shows a fresh code; deactivate reloads", async () => {
    renderScreen(<TeamScreen />);
    const regenButtons = await screen.findAllByRole("button", { name: "new code" });
    fireEvent.click(regenButtons[1]);
    await waitFor(() => expect(post).toHaveBeenCalledWith("/team/users/2/regenerate-code", {}));
    expect(await screen.findByTestId("one-time-code")).toBeInTheDocument();
    const deactivateButtons = screen.getAllByRole("button", { name: "deactivate" });
    fireEvent.click(deactivateButtons[0]);
    await waitFor(() => expect(post).toHaveBeenCalledWith("/team/users/1/deactivate", {}));
  });

  it("renders metadata-only stats", async () => {
    renderScreen(<TeamScreen />);
    const row = await screen.findByTestId("team-stats");
    expect(row).toHaveTextContent("runs: 12");
    expect(row).toHaveTextContent("$3.50");
    expect(row).toHaveTextContent("ask×8 development×4");
  });

  it("shows an admin-only note when the server denies", async () => {
    get.mockRejectedValue(new Error("403 forbidden: admin only"));
    renderScreen(<TeamScreen />);
    expect(await screen.findByTestId("team-denied")).toBeInTheDocument();
  });
});
