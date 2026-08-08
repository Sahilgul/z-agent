import { render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";
import { SessionResume } from "../components/SessionResume";
import type { Run } from "../types";

const apiMock = vi.hoisted(() => ({
  get: vi.fn<(path: string) => Promise<unknown>>(),
  post: vi.fn<(path: string, body?: unknown) => Promise<unknown>>(),
}));

vi.mock("../lib/api", async (importOriginal) => {
  const orig = await importOriginal<typeof import("../lib/api")>();
  return {
    ...orig,
    api: {
      get: (path: string) => apiMock.get(path),
      post: (path: string, body?: unknown) => apiMock.post(path, body),
    },
  };
});

const run = (stage: Run["stage"]): Run => ({
  id: "r1", mode: "ask", autonomy: "auto", stage, title: "t",
  auto_summary: null, repo: null, work_item_id: null, available_actions: [],
  failure_reason: null, cost_usd: 0, tokens: 0, last_active_at: null, created_at: null,
});

function renderCard(stage: Run["stage"], resumable: boolean, onEdit = vi.fn()) {
  apiMock.get.mockResolvedValue({ resumable });
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const onResumed = vi.fn();
  render(
    <QueryClientProvider client={qc}>
      <SessionResume run={run(stage)} working={false} onResumed={onResumed} onEdit={onEdit} />
    </QueryClientProvider>,
  );
  return { onResumed, onEdit };
}

describe("SessionResume (W-B2)", () => {
  afterEach(() => {
    apiMock.get.mockReset();
    apiMock.post.mockReset();
  });

  it("never renders for a live run (a resume then would double-execute)", () => {
    const { container } = render(
      <QueryClientProvider client={new QueryClient()}>
        <SessionResume run={run("developing")} working onResumed={() => {}} />
      </QueryClientProvider>,
    );
    expect(container.firstChild).toBeNull();
  });

  it("never renders for an abandoned run (the workspace was shredded)", () => {
    const { container } = render(
      <QueryClientProvider client={new QueryClient()}>
        <SessionResume run={run("abandoned")} working={false} onResumed={() => {}} />
      </QueryClientProvider>,
    );
    expect(container.firstChild).toBeNull();
  });

  it("shows the continue button on an interrupted run with a live volume", async () => {
    renderCard("interrupted", true);
    expect(await screen.findByText("continue session")).toBeInTheDocument();
  });

  it("shows replay-only copy on a completed run whose volume expired", async () => {
    renderCard("completed", false);
    expect(await screen.findByText(/replay only/)).toBeInTheDocument();
    expect(screen.queryByText("continue session")).not.toBeInTheDocument();
  });

  it("offers edit & resend only on interrupted runs", async () => {
    const { onEdit } = renderCard("interrupted", true);
    const btn = await screen.findByText("edit & resend");
    btn.click();
    expect(onEdit).toHaveBeenCalled();
  });

  it("surfaces a resume failure as a toast and re-arms the button", async () => {
    apiMock.post.mockRejectedValue(new Error("boom"));
    renderCard("interrupted", true);
    const btn = await screen.findByText("continue session");
    btn.click();
    await waitFor(() => expect(apiMock.post.mock.calls[0]?.[0]).toBe("/sessions/r1/resume"));
    await waitFor(() => expect(screen.getByText("continue session")).not.toBeDisabled());
  });

  it("disables while the resume is in flight (no double-click double-resume)", async () => {
    let resolvePost: (v: unknown) => void = () => {};
    apiMock.post.mockImplementation(() => new Promise((r) => { resolvePost = r; }));
    renderCard("interrupted", true);
    const btn = await screen.findByText("continue session");
    btn.click();
    await waitFor(() => expect(screen.getByText("resuming…")).toBeDisabled());
    resolvePost({});
    await waitFor(() => expect(screen.getByText("continue session")).not.toBeDisabled());
  });
});
