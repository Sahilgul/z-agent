import { fireEvent, screen, waitFor } from "@testing-library/react";
import { renderScreen } from "./render";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { IdeasScreen } from "../features/ideas/IdeasScreen";

const get = vi.fn();
const post = vi.fn();
vi.mock("../lib/api", () => ({
  api: { get: (...a: unknown[]) => get(...a), post: (...a: unknown[]) => post(...a) },
}));

const threadList = [{
  id: 5, title: "Ship the fleet graph to threads?", body: "", created_by: 1,
  status: "open", summary: null, promoted_run_id: null, comment_count: 2,
  created_at: "2026-08-01T00:00:00Z",
}];

const detail = {
  ...threadList[0],
  summary: {
    consensus: "worth doing", disagreements: ["sequencing"],
    recommendation: "pilot on ServerApp", open_questions: [],
  },
  comments: [
    { id: 1, author_type: "user", author_name: "Sahil", body: "strong yes", created_at: null },
    { id: 2, author_type: "agent", author_name: "counsel", body: "wait for the flywheel", created_at: null },
  ],
};

beforeEach(() => {
  get.mockReset();
  post.mockReset();
  get.mockImplementation((path: string) =>
    Promise.resolve(path === "/ideas" ? threadList : detail));
  post.mockResolvedValue({});
});

describe("IdeasScreen", () => {
  it("lists threads with voice counts", async () => {
    renderScreen(<IdeasScreen />);
    expect(await screen.findByText("Ship the fleet graph to threads?")).toBeInTheDocument();
    expect(screen.getByText(/2 voices · open/)).toBeInTheDocument();
  });

  it("thread view pins the Lead synthesis above raw voices", async () => {
    renderScreen(<IdeasScreen />);
    fireEvent.click(await screen.findByText("Ship the fleet graph to threads?"));
    expect(await screen.findByText(/lead synthesis · all voices/)).toBeInTheDocument();
    expect(screen.getByText(/worth doing/)).toBeInTheDocument();
    expect(screen.getByText("strong yes")).toBeInTheDocument();
  });

  it("Counsel's comment wears the 11th-member badge and voice", async () => {
    renderScreen(<IdeasScreen />);
    fireEvent.click(await screen.findByText("Ship the fleet graph to threads?"));
    expect(await screen.findByText("counsel · 11th member")).toBeInTheDocument();
    expect(screen.getByText("wait for the flywheel")).toBeInTheDocument();
  });

  it("ask counsel posts to the endpoint", async () => {
    renderScreen(<IdeasScreen />);
    fireEvent.click(await screen.findByText("Ship the fleet graph to threads?"));
    fireEvent.click(await screen.findByRole("button", { name: "ask counsel" }));
    await waitFor(() => expect(post).toHaveBeenCalledWith("/ideas/5/ask-counsel", {}));
  });

  it("promote to plan posts once and disables while in flight (W9-H1)", async () => {
    let resolvePost: (v: unknown) => void = () => {};
    post.mockImplementation((path: string) =>
      path.endsWith("/promote") ? new Promise((r) => { resolvePost = r; }) : Promise.resolve({}));
    renderScreen(<IdeasScreen />);
    fireEvent.click(await screen.findByText("Ship the fleet graph to threads?"));
    fireEvent.click(await screen.findByRole("button", { name: "promote to plan" }));
    // While the POST is in flight the button must be disabled — a second
    // click must not mint a second run.
    expect(await screen.findByRole("button", { name: "promoting…" })).toBeDisabled();
    resolvePost({});
    await waitFor(() => expect(post).toHaveBeenCalledWith("/ideas/5/promote", {}));
  });

  it("hides the promote button on an already-promoted thread", async () => {
    get.mockImplementation((path: string) =>
      Promise.resolve(path === "/ideas" ? threadList : { ...detail, status: "promoted", promoted_run_id: "r9" }));
    renderScreen(<IdeasScreen />);
    fireEvent.click(await screen.findByText("Ship the fleet graph to threads?"));
    await screen.findByText(/lead synthesis · all voices/);
    expect(screen.queryByRole("button", { name: /promote/ })).not.toBeInTheDocument();
  });

  it("comment composer refuses empty voices", async () => {
    renderScreen(<IdeasScreen />);
    fireEvent.click(await screen.findByText("Ship the fleet graph to threads?"));
    const btn = await screen.findByRole("button", { name: "comment" });
    expect(btn).toBeDisabled();
  });
});
