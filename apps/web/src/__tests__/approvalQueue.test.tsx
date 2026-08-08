import { fireEvent, screen, waitFor } from "@testing-library/react";
import { renderScreen } from "./render";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApprovalQueue } from "../components/ApprovalQueue";
import type { Approval } from "../types";

const get = vi.fn();
const post = vi.fn();
vi.mock("../lib/api", async (importOriginal) => {
  const orig = await importOriginal<typeof import("../lib/api")>();
  return {
    ...orig,
    api: { get: (...a: unknown[]) => get(...a), post: (...a: unknown[]) => post(...a) },
  };
});

const toastWarning = vi.fn();
const toastError = vi.fn();
vi.mock("@/components/ui/sonner", () => ({
  toast: {
    success: vi.fn(),
    warning: (...a: unknown[]) => toastWarning(...a),
    error: (...a: unknown[]) => toastError(...a),
  },
  Toaster: () => null,
}));

const subscribe = vi.fn();
const hasSub = vi.fn();
const unsubscribePush = vi.fn();
vi.mock("../lib/push", () => ({
  pushSupported: () => true,
  hasSubscription: () => hasSub(),
  subscribeToPush: () => subscribe(),
  unsubscribeFromPush: () => unsubscribePush(),
}));

const card: Approval = {
  id: "a-1", run_id: "run-1234567890", thread_id: "thread-abcdef123456",
  kind: "Bash", payload: { cmd: "npm test", always_allowable: true },
  created_at: null,
};

describe("ApprovalQueue", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    hasSub.mockResolvedValue(false);
    subscribe.mockResolvedValue(true);
    post.mockResolvedValue({ ok: true, decision: "allow_once" });
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

  it("shows the unsubscribe toggle when already subscribed (W10-#8)", async () => {
    hasSub.mockResolvedValue(true);
    get.mockResolvedValue([card]);
    renderScreen(<ApprovalQueue runId={card.run_id} />);
    expect(await screen.findByText("Bash")).toBeInTheDocument();
    expect(screen.queryByTestId("push-ask")).not.toBeInTheDocument();
    // the off switch: disabling calls unsubscribeFromPush and hides the row
    unsubscribePush.mockResolvedValue(true);
    fireEvent.click((await screen.findByTestId("push-on")).querySelector("button")!);
    await waitFor(() => expect(unsubscribePush).toHaveBeenCalled());
    await waitFor(() => expect(screen.queryByTestId("push-on")).not.toBeInTheDocument());
  });

  it("enable push calls subscribe and dismisses", async () => {
    get.mockResolvedValue([card]);
    renderScreen(<ApprovalQueue runId={card.run_id} />);
    fireEvent.click(await screen.findByRole("button", { name: "enable push" }));
    await waitFor(() => expect(subscribe).toHaveBeenCalled());
    await waitFor(() => expect(screen.queryByTestId("push-ask")).not.toBeInTheDocument());
  });

  // ------------------------------------------------------------- W3 truths

  it("W-H4: a destructive card hides 'always allow' and says why", async () => {
    get.mockResolvedValue([{ ...card, payload: { args: { command: "rm -rf /" }, destructive: true, always_allowable: false } }]);
    renderScreen(<ApprovalQueue runId={card.run_id} />);
    expect(await screen.findByText("destructive")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "always allow" })).not.toBeInTheDocument();
    expect(screen.getByText("never auto-allowed")).toBeInTheDocument();
  });

  it("W4-L2: a legacy card with no flags is treated as destructive", async () => {
    get.mockResolvedValue([{ ...card, payload: { cmd: "npm test" } }]);
    renderScreen(<ApprovalQueue runId={card.run_id} />);
    await screen.findByText("Bash");
    expect(screen.queryByRole("button", { name: "always allow" })).not.toBeInTheDocument();
    expect(screen.getByText("never auto-allowed")).toBeInTheDocument();
  });

  it("W-H4: a non-destructive always-allowable card keeps 'always allow'", async () => {
    get.mockResolvedValue([card]);
    renderScreen(<ApprovalQueue runId={card.run_id} />);
    expect(await screen.findByRole("button", { name: "always allow" })).toBeInTheDocument();
  });

  it("W4-M2: toasts when the recorded decision differs from the click (timeout)", async () => {
    post.mockResolvedValue({ ok: true, decision: "timeout" });
    get.mockResolvedValue([card]);
    renderScreen(<ApprovalQueue runId={card.run_id} />);
    fireEvent.click(await screen.findByRole("button", { name: "allow once" }));
    await waitFor(() => expect(toastWarning).toHaveBeenCalledWith(
      expect.stringContaining("timeout"), expect.anything(),
    ));
  });

  it("W4-L1: a 409 re-drive explains itself instead of dead-ending", async () => {
    const { ApiError } = await import("../lib/api");
    post.mockRejectedValue(new ApiError(409, "approval already decided (deny)"));
    get.mockResolvedValue([card]);
    renderScreen(<ApprovalQueue runId={card.run_id} />);
    fireEvent.click(await screen.findByRole("button", { name: "allow once" }));
    await waitFor(() => expect(toastWarning).toHaveBeenCalledWith(
      "already decided elsewhere", expect.objectContaining({ description: expect.stringContaining("already decided") }),
    ));
    // The card comes back (rollback) so the truth refetch can settle it.
    await waitFor(() => expect(screen.getByText("Bash")).toBeInTheDocument());
  });

  it("W-H6: edit posts edited_allow with the trimmed command", async () => {
    get.mockResolvedValue([{ ...card, payload: { args: { command: "rm -rf build/" }, destructive: true, always_allowable: false } }]);
    renderScreen(<ApprovalQueue runId={card.run_id} />);
    fireEvent.click(await screen.findByRole("button", { name: "edit" }));
    const area = screen.getByLabelText("edited command");
    expect((area as HTMLTextAreaElement).value).toBe("rm -rf build/");
    fireEvent.change(area, { target: { value: "rm -rf build/tmp" } });
    fireEvent.click(screen.getByRole("button", { name: "allow edited" }));
    await waitFor(() => expect(post).toHaveBeenCalledWith("/approvals/a-1/decide", {
      decision: "edited_allow",
      edited_args: { command: "rm -rf build/tmp" },
    }));
  });

  it("W4-M3: knowledge-draft cards never render in the session queue", async () => {
    get.mockResolvedValue([
      card,
      { ...card, id: "k-1", kind: "knowledge", payload: { preview: "draft fact" } },
    ]);
    renderScreen(<ApprovalQueue runId={card.run_id} />);
    await screen.findByText("Bash");
    expect(screen.queryByText("knowledge")).not.toBeInTheDocument();
  });
});
