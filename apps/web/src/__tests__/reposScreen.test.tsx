import { fireEvent, screen, waitFor } from "@testing-library/react";
import { renderScreen } from "./render";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ReposScreen } from "../features/repos/ReposScreen";

const get = vi.fn();
const post = vi.fn();
vi.mock("../lib/api", () => ({
  api: { get: (...a: unknown[]) => get(...a), post: (...a: unknown[]) => post(...a) },
}));

const repos = [
  { id: 1, name: "ServerApp", integration_branch: "main", status: "ready",
    status_detail: "", last_fetch_head: "abc1234567890" },
  { id: 2, name: "ClientApp", integration_branch: "develop", status: "cloning",
    status_detail: "fetching objects", last_fetch_head: null },
];

describe("ReposScreen", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    get.mockImplementation((url: string) =>
      Promise.resolve(url.startsWith("/repos/remote-branches") ? { branches: ["main", "develop"] } : repos)
    );
    post.mockResolvedValue({});
  });

  it("lists repos with status and HEAD", async () => {
    renderScreen(<ReposScreen />);
    expect(await screen.findByTestId("repo-ServerApp")).toBeInTheDocument();
    expect(screen.getByText("ready")).toBeInTheDocument();
    expect(screen.getByText("abc1234")).toBeInTheDocument();
    expect(screen.getByText("not fetched yet")).toBeInTheDocument();
  });

  it("fetches remote branches before registering — never free-typed", async () => {
    renderScreen(<ReposScreen />);
    await screen.findByTestId("repo-ServerApp");
    fireEvent.click(screen.getByRole("button", { name: "+ add repo" }));
    fireEvent.change(screen.getByPlaceholderText("e.g. Billing-Engine"), {
      target: { value: "Billing-Engine" },
    });
    fireEvent.click(screen.getByRole("button", { name: "fetch branches" }));
    await waitFor(() =>
      expect(get).toHaveBeenCalledWith("/repos/remote-branches?name=Billing-Engine")
    );
    const select = await screen.findByRole("combobox");
    expect(select).toHaveValue("main");
    fireEvent.click(screen.getByRole("button", { name: "register & onboard" }));
    await waitFor(() =>
      expect(post).toHaveBeenCalledWith("/repos", {
        name: "Billing-Engine", integration_branch: "main",
      })
    );
  });

  it("shows remote errors in the form", async () => {
    get.mockImplementation((url: string) =>
      url.startsWith("/repos/remote-branches")
        ? Promise.reject(new Error("repository not found or access denied"))
        : Promise.resolve(repos)
    );
    renderScreen(<ReposScreen />);
    await screen.findByTestId("repo-ServerApp");
    fireEvent.click(screen.getByRole("button", { name: "+ add repo" }));
    fireEvent.change(screen.getByPlaceholderText("e.g. Billing-Engine"), {
      target: { value: "Ghost" },
    });
    fireEvent.click(screen.getByRole("button", { name: "fetch branches" }));
    expect(await screen.findByText("repository not found or access denied")).toBeInTheDocument();
  });
});
