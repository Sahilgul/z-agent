import { fireEvent, screen, waitFor } from "@testing-library/react";
import { renderScreen } from "./render";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApprovalQueue } from "../components/ApprovalQueue";

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
  kind: "Bash", payload: { cmd: "npm test" },
};

describe("ApprovalQueue", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    hasSub.mockResolvedValue(false);
    subscribe.mockResolvedValue(true);
    post.mockResolvedValue({});
  });

  it("renders cards and posts decisions", async () => {
    get.mockResolvedValue([card]);
    renderScreen(<ApprovalQueue runId={card.run_id} />);
    expect(await screen.findByText("Bash")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "allow once" }));
    await waitFor(() =>
      expect(post).toHaveBeenCalledWith("/approvals/a-1/decide", { decision: "allow_once" })
    );
  });

  it("asks the API for the open run only", async () => {
    get.mockResolvedValue([card]);
    renderScreen(<ApprovalQueue runId={card.run_id} />);
    await waitFor(() =>
      expect(get).toHaveBeenCalledWith(`/approvals?run_id=${encodeURIComponent(card.run_id)}`)
    );
  });

  it("asks for push opt-in only when a card is waiting", async () => {
    get.mockResolvedValue([card]);
    renderScreen(<ApprovalQueue runId={card.run_id} />);
    expect(await screen.findByTestId("push-ask")).toBeInTheDocument();
  });

  it("renders nothing when nothing is waiting", async () => {
    get.mockResolvedValue([]);
    renderScreen(<ApprovalQueue runId={card.run_id} />);
    await waitFor(() => expect(get).toHaveBeenCalled());
    expect(screen.queryByTestId("approval-queue")).not.toBeInTheDocument();
    expect(screen.queryByTestId("push-ask")).not.toBeInTheDocument();
  });

  it("hides the opt-in when already subscribed", async () => {
    hasSub.mockResolvedValue(true);
    get.mockResolvedValue([card]);
    renderScreen(<ApprovalQueue runId={card.run_id} />);
    expect(await screen.findByText("Bash")).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByTestId("push-ask")).not.toBeInTheDocument());
  });

  it("enable push calls subscribe and dismisses", async () => {
    get.mockResolvedValue([card]);
    renderScreen(<ApprovalQueue runId={card.run_id} />);
    fireEvent.click(await screen.findByRole("button", { name: "enable push" }));
    await waitFor(() => expect(subscribe).toHaveBeenCalled());
    await waitFor(() => expect(screen.queryByTestId("push-ask")).not.toBeInTheDocument());
  });
});
