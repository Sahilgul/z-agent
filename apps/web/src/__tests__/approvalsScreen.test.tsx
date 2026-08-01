import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApprovalsScreen } from "../features/approvals/ApprovalsScreen";

const get = vi.fn();
const post = vi.fn();
vi.mock("../lib/api", () => ({
  api: { get: (...a: unknown[]) => get(...a), post: (...a: unknown[]) => post(...a) },
}));

const subscribe = vi.fn();
const hasSub = vi.fn();
vi.mock("../lib/push", () => ({
  pushSupported: () => true,
  hasSubscription: () => hasSub(),
  subscribeToPush: () => subscribe(),
}));

const card = {
  id: "a-1", run_id: "run-1234567890", lane_id: "lane-abcdef123456",
  tool: "Bash", input: { cmd: "npm test" },
};

describe("ApprovalsScreen", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    hasSub.mockResolvedValue(false);
    subscribe.mockResolvedValue(true);
    post.mockResolvedValue({});
  });

  it("renders cards and posts decisions", async () => {
    get.mockResolvedValue([card]);
    render(<ApprovalsScreen />);
    expect(await screen.findByText("Bash")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "allow once" }));
    await waitFor(() =>
      expect(post).toHaveBeenCalledWith("/approvals/a-1/decide", { decision: "allow_once" })
    );
  });

  it("asks for push opt-in only when a card is waiting", async () => {
    get.mockResolvedValue([card]);
    render(<ApprovalsScreen />);
    expect(await screen.findByTestId("push-ask")).toBeInTheDocument();
  });

  it("hides the opt-in when nothing is waiting", async () => {
    get.mockResolvedValue([]);
    render(<ApprovalsScreen />);
    expect(await screen.findByText("nothing waiting on you")).toBeInTheDocument();
    expect(screen.queryByTestId("push-ask")).not.toBeInTheDocument();
  });

  it("hides the opt-in when already subscribed", async () => {
    hasSub.mockResolvedValue(true);
    get.mockResolvedValue([card]);
    render(<ApprovalsScreen />);
    expect(await screen.findByText("Bash")).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByTestId("push-ask")).not.toBeInTheDocument());
  });

  it("enable push calls subscribe and dismisses", async () => {
    get.mockResolvedValue([card]);
    render(<ApprovalsScreen />);
    fireEvent.click(await screen.findByRole("button", { name: "enable push" }));
    await waitFor(() => expect(subscribe).toHaveBeenCalled());
    await waitFor(() => expect(screen.queryByTestId("push-ask")).not.toBeInTheDocument());
  });
});
