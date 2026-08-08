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
  proposed_scope: "repo",
  created_at: "2026-08-01T00:00:00Z",
};
const sharedItem = {
  id: 3, content: "audit log in same transaction", trigger_description: "drizzle writes",
  scope: "global", repo: null, status: "approved", created_by: 2, source_run_id: null,
  proposed_scope: null,
  created_at: "2026-07-30T00:00:00Z",
};

const repos = [{ id: 1, name: "LivekitScribe", status: "ready" }];

beforeEach(() => {
  get.mockReset();
  post.mockReset();
  get.mockImplementation((path: string) => {
    if (path === "/knowledge/pending") return Promise.resolve([draftItem]);
    if (path === "/repos") return Promise.resolve(repos);
    return Promise.resolve([sharedItem, draftItem]);
  });
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
    // W9-M5: the selector defaults to the PROPOSED scope, not flat "global"
    await waitFor(() => expect(screen.getByDisplayValue("share: repo")).toBeInTheDocument());
    // proposed repo scope with no entry.repo → picker shown; pick one
    fireEvent.change(screen.getByLabelText("repo name"), { target: { value: "LivekitScribe" } });
    fireEvent.click(approveBtn);
    await waitFor(() =>
      expect(post).toHaveBeenCalledWith("/knowledge/7/approve", { scope: "repo", repo: "LivekitScribe" }));
  });

  it("W9-M4: reject posts to the reject endpoint and reloads", async () => {
    renderScreen(<KnowledgeScreen />);
    fireEvent.click(await screen.findByRole("button", { name: "reject" }));
    await waitFor(() => expect(post).toHaveBeenCalledWith("/knowledge/7/reject", {}));
  });

  it("W9-M6: repo scope offers a registry-backed picker, not free text", async () => {
    renderScreen(<KnowledgeScreen />);
    await screen.findAllByText("always branch from develop");
    fireEvent.change(screen.getByDisplayValue("share: repo"), { target: { value: "global" } });
    fireEvent.change(screen.getByDisplayValue("share: global"), { target: { value: "repo" } });
    const picker = screen.getByLabelText("repo name");
    expect(picker.tagName).toBe("SELECT");
    expect(screen.getByRole("option", { name: "LivekitScribe" })).toBeInTheDocument();
  });

  it("W9-L16: a failed drafts fetch renders an error, not a silent empty inbox", async () => {
    get.mockImplementation((path: string) =>
      path === "/knowledge/pending"
        ? Promise.reject(new Error("502 bad gateway"))
        : Promise.resolve(path === "/repos" ? repos : [sharedItem]));
    renderScreen(<KnowledgeScreen />);
    expect(await screen.findByRole("alert")).toHaveTextContent("drafts failed to load");
  });
});
