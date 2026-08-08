import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { SessionsScreen } from "../features/sessions/SessionsScreen";
import { useRuns } from "../stores/run";
import { useSession } from "../stores/session";
import type { Run } from "../types";

// W6-M9: coverage for the REAL composer in SessionsScreen — the old
// components/Composer.tsx had tests but was dead code, so the actual send
// path (idempotency key, busy-gated Enter, terminal-stage blocking) had
// none.

const apiMock = vi.hoisted(() => ({
  get: vi.fn<(path: string) => Promise<unknown>>(),
  post: vi.fn<(path: string, body?: unknown) => Promise<unknown>>(),
  patch: vi.fn(),
}));

vi.mock("../lib/api", async (importOriginal) => {
  const orig = await importOriginal<typeof import("../lib/api")>();
  return {
    ...orig,
    api: {
      get: (path: string) => apiMock.get(path),
      post: (path: string, body?: unknown) => apiMock.post(path, body),
      patch: (path: string, body?: unknown) => apiMock.patch(path, body),
    },
  };
});

vi.mock("../lib/push", () => ({
  pushSupported: () => false,
  hasSubscription: () => Promise.resolve(false),
  subscribeToPush: () => Promise.resolve(false),
  unsubscribeFromPush: () => Promise.resolve(true),
}));

const startedRun: Run = {
  id: "r-new", mode: "ask", autonomy: "auto", stage: "investigating", title: "t",
  auto_summary: null, repo: null, work_item_id: null, available_actions: [],
  failure_reason: null, cost_usd: 0, tokens: 0, last_active_at: null, created_at: null,
};

function renderSessions() {
  return render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <MemoryRouter initialEntries={["/app"]}>
        <SessionsScreen />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("SessionsScreen composer (W6-M9)", () => {
  beforeEach(() => {
    (globalThis as unknown as { WebSocket: unknown }).WebSocket = vi.fn(() => ({
      onopen: null, onmessage: null, onclose: null, onerror: null, close: vi.fn(),
    }));
    useSession.setState({
      me: { id: 1, username: "sahil", display_name: "Sahil", role: "admin" },
      booted: true,
    });
    useRuns.setState({
      runs: [], runsLoaded: false, current: null, threads: [], events: [],
      deltas: [], socketConnected: false,
    });
    apiMock.get.mockImplementation((path: string) => {
      if (path === "/runs") return Promise.resolve([]);
      if (path === "/repos") return Promise.resolve([]);
      if (path === "/hydration/my-tickets") return Promise.resolve([]);
      if (path === "/runs/r-new") return Promise.resolve(startedRun);
      if (path.startsWith("/runs/r-new/threads")) return Promise.resolve([]);
      if (path.startsWith("/runs/r-new/events")) return Promise.resolve([]);
      if (path.startsWith("/approvals")) return Promise.resolve([]);
      return Promise.resolve(null);
    });
    apiMock.post.mockImplementation((path: string) =>
      path === "/runs" ? Promise.resolve(startedRun) : Promise.resolve({ ok: true }));
  });

  afterEach(() => {
    useRuns.getState().closeRun();
    apiMock.get.mockReset();
    apiMock.post.mockReset();
  });

  it("Enter starts ONE run with a per-draft idempotency key", async () => {
    renderSessions();
    const composer = await screen.findByPlaceholderText(/describe the task/i);
    fireEvent.change(composer, { target: { value: "audit the retry logic" } });
    // Held-open POST: the second Enter lands while the first is in flight.
    let resolvePost!: (v: unknown) => void;
    apiMock.post.mockImplementationOnce(
      (path: string) =>
        path === "/runs"
          ? new Promise((res) => { resolvePost = res; })
          : Promise.resolve({ ok: true }),
    );
    fireEvent.keyDown(composer, { key: "Enter" });
    fireEvent.keyDown(composer, { key: "Enter" });
    await waitFor(() => expect(apiMock.post).toHaveBeenCalledTimes(1));
    const body = apiMock.post.mock.calls[0][1] as { task: string; idempotency_key?: string };
    expect(body.task).toBe("audit the retry logic");
    expect(typeof body.idempotency_key).toBe("string");
    resolvePost(startedRun);
    await waitFor(() => expect(useRuns.getState().current?.id).toBe("r-new"));
  });

  it("keeps the composer ENABLED on a terminal run — send chains a fresh lane (§1)", async () => {
    // No dead sessions: a completed run is chatable from where it stopped.
    const finished: Run = { ...startedRun, id: "r-done", stage: "completed" };
    useRuns.setState({ current: finished, runs: [finished], runsLoaded: true });
    renderSessions();
    const composer = await screen.findByPlaceholderText(/pick the session back up/i);
    expect(composer).not.toBeDisabled();
    fireEvent.change(composer, { target: { value: "one more thing" } });
    fireEvent.keyDown(composer, { key: "Enter" });
    await waitFor(() =>
      expect(apiMock.post).toHaveBeenCalledWith(
        expect.stringContaining("/intent"),
        expect.objectContaining({ text: "one more thing" }),
      ));
  });
});
