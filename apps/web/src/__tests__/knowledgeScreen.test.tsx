import { fireEvent, screen, waitFor } from "@testing-library/react";
import { renderScreen } from "./render";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { KnowledgeScreen } from "../features/knowledge/KnowledgeScreen";

const get = vi.fn();
const post = vi.fn();
vi.mock("../lib/api", () => ({
  api: { get: (...a: unknown[]) => get(...a), post: (...a: unknown[]) => post(...a) },
}));

const draftItem = {
  id: 7, content: "always branch from develop", trigger_description: "when creating branches",
  scope: "user", repo: null, status: "draft", created_by: 1, source_run_id: "r1-abcdefgh",
  created_at: "2026-08-01T00:00:00Z",
};
const sharedItem = {
  id: 3, content: "audit log in same transaction", trigger_description: "drizzle writes",
  scope: "global", repo: null, status: "approved", created_by: 2, source_run_id: null,
  created_at: "2026-07-30T00:00:00Z",
};

beforeEach(() => {
  get.mockReset();
  post.mockReset();
  get.mockImplementation((path: string) =>
    Promise.resolve(path === "/knowledge/pending" ? [draftItem] : [sharedItem, draftItem]));
  post.mockResolvedValue({});
});

describe("KnowledgeScreen", () => {
  it("renders the draft inbox with the PHI checkpoint marker", async () => {
    renderScreen(<KnowledgeScreen />);
    // the draft shows in BOTH the inbox and the corpus (own drafts are yours)
    expect((await screen.findAllByText("always branch from develop")).length).toBe(2);
    expect(screen.getByText(/PHI checkpoint/)).toBeInTheDocument();
    expect(screen.getByText(/distilled from run r1-abcde/)).toBeInTheDocument();
  });

  it("renders the shared corpus with scope and status", async () => {
    renderScreen(<KnowledgeScreen />);
    expect(await screen.findByText("audit log in same transaction")).toBeInTheDocument();
    expect(screen.getByText(/global · approved/)).toBeInTheDocument();
  });

  it("approve posts the chosen scope and reloads", async () => {
    renderScreen(<KnowledgeScreen />);
    const approveBtn = await screen.findByRole("button", { name: "approve" });
    fireEvent.click(approveBtn);
    await waitFor(() =>
      expect(post).toHaveBeenCalledWith("/knowledge/7/approve", { scope: "global", repo: null }));
  });

  it("repo scope reveals the repo name input", async () => {
    renderScreen(<KnowledgeScreen />);
    await screen.findAllByText("always branch from develop");
    fireEvent.change(screen.getByDisplayValue("share: global"), { target: { value: "repo" } });
    expect(screen.getByPlaceholderText("repo name")).toBeInTheDocument();
  });
});
