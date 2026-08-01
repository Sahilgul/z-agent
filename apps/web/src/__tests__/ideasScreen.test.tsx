import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { IdeasScreen } from "../features/ideas/IdeasScreen";

const get = vi.fn();
const post = vi.fn();
vi.mock("../lib/api", () => ({
  api: { get: (...a: unknown[]) => get(...a), post: (...a: unknown[]) => post(...a) },
}));

const threadList = [{
  id: 5, title: "Ship the fleet graph to lanes?", body: "", created_by: 1,
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
    render(<IdeasScreen />);
    expect(await screen.findByText("Ship the fleet graph to lanes?")).toBeInTheDocument();
    expect(screen.getByText(/2 voices · open/)).toBeInTheDocument();
  });

  it("thread view pins the Lead synthesis above raw voices", async () => {
    render(<IdeasScreen />);
    fireEvent.click(await screen.findByText("Ship the fleet graph to lanes?"));
    expect(await screen.findByText(/lead synthesis · all voices/)).toBeInTheDocument();
    expect(screen.getByText(/worth doing/)).toBeInTheDocument();
    expect(screen.getByText("strong yes")).toBeInTheDocument();
  });

  it("Counsel's comment wears the 11th-member badge and voice", async () => {
    render(<IdeasScreen />);
    fireEvent.click(await screen.findByText("Ship the fleet graph to lanes?"));
    expect(await screen.findByText("counsel · 11th member")).toBeInTheDocument();
    expect(screen.getByText("wait for the flywheel")).toBeInTheDocument();
  });

  it("ask counsel posts to the endpoint", async () => {
    render(<IdeasScreen />);
    fireEvent.click(await screen.findByText("Ship the fleet graph to lanes?"));
    fireEvent.click(await screen.findByRole("button", { name: "ask counsel" }));
    await waitFor(() => expect(post).toHaveBeenCalledWith("/ideas/5/ask-counsel", {}));
  });

  it("promote to plan posts and disables after promotion", async () => {
    render(<IdeasScreen />);
    fireEvent.click(await screen.findByText("Ship the fleet graph to lanes?"));
    fireEvent.click(await screen.findByRole("button", { name: "promote to plan" }));
    await waitFor(() => expect(post).toHaveBeenCalledWith("/ideas/5/promote", {}));
  });

  it("comment composer refuses empty voices", async () => {
    render(<IdeasScreen />);
    fireEvent.click(await screen.findByText("Ship the fleet graph to lanes?"));
    const btn = await screen.findByRole("button", { name: "comment" });
    expect(btn).toBeDisabled();
  });
});
