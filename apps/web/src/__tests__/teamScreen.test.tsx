import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { renderScreen } from "./render";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { TeamScreen } from "../features/team/TeamScreen";
import { useSession } from "../stores/session";

const get = vi.fn();
const post = vi.fn();
vi.mock("../lib/api", async (importOriginal) => {
  const orig = await importOriginal<typeof import("../lib/api")>();
  return { ...orig, api: { get: (...a: unknown[]) => get(...a), post: (...a: unknown[]) => post(...a) } };
});

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
    useSession.setState({ me: { id: 1, username: "sahil", display_name: "Sahil", role: "admin" } });
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

  it("regenerate shows a fresh code; deactivate is a two-tap confirm (W-H16)", async () => {
    renderScreen(<TeamScreen />);
    const regenButtons = await screen.findAllByRole("button", { name: "new code" });
    fireEvent.click(regenButtons[1]);
    await waitFor(() => expect(post).toHaveBeenCalledWith("/team/users/2/regenerate-code", {}));
    expect(await screen.findByTestId("one-time-code")).toBeInTheDocument();

    const row = screen.getByTestId("user-ali.r");
    // one tap arms the confirm, nothing is sent yet
    fireEvent.click(within(row).getByRole("button", { name: "deactivate" }));
    expect(post).not.toHaveBeenCalledWith("/team/users/2/deactivate", {});
    fireEvent.click(within(row).getByRole("button", { name: /confirm/ }));
    await waitFor(() => expect(post).toHaveBeenCalledWith("/team/users/2/deactivate", {}));
  });

  it("W-H16: never offers self-deactivation", async () => {
    renderScreen(<TeamScreen />);
    const ownRow = await screen.findByTestId("user-sahil");
    expect(within(ownRow).queryByRole("button", { name: "deactivate" })).toBeNull();
    expect(within(ownRow).getByText("you")).toBeInTheDocument();
  });

  it("W-H16: toasts instead of dying silently when regen fails", async () => {
    renderScreen(<TeamScreen />);
    await screen.findByTestId("user-ali.r");
    post.mockRejectedValueOnce(new Error("409 user active"));
    const row = screen.getByTestId("user-ali.r");
    fireEvent.click(within(row).getByRole("button", { name: "new code" }));
    await waitFor(() => expect(post).toHaveBeenCalledWith("/team/users/2/regenerate-code", {}));
    // failure surfaces as a toast, not an unhandled rejection crashing the row
    expect(screen.queryByTestId("one-time-code")).not.toBeInTheDocument();
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
