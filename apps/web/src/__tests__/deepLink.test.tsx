import { render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { SessionsScreen } from "../features/sessions/SessionsScreen";
import type { Run } from "../types";

// W-B5: a push notification targets /app?run=<id>&card=<id> — the sessions
// screen must open that run and scroll the target approval card into view,
// then strip the params so the link isn't re-consumed on every render.

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

const runFixture: Run = {
  id: "r2", mode: "plan", autonomy: "auto", stage: "awaiting_user", title: "deep-linked",
  auto_summary: null, repo: null, work_item_id: null, available_actions: [],
  failure_reason: null, cost_usd: 0, tokens: 0, last_active_at: null, created_at: null,
};

const card = {
  id: "c9", run_id: "r2", thread_id: null, kind: "tool_call",
  payload: {}, created_at: null, expires_at: null,
};

function LocationProbe({ seen }: { seen: string[] }) {
  const loc = useLocation();
  seen.push(loc.search);
  return null;
}

describe("deep-link consumer (W-B5)", () => {
  beforeEach(() => {
    Element.prototype.scrollIntoView = vi.fn();
    (globalThis as unknown as { WebSocket: unknown }).WebSocket = vi.fn(() => ({
      onopen: null, onmessage: null, onclose: null, onerror: null, close: vi.fn(),
    }));
    apiMock.get.mockImplementation((path: string) => {
      if (path === "/runs") return Promise.resolve([]);
      if (path === "/runs/r2") return Promise.resolve(runFixture);
      if (path.startsWith("/runs/r2/threads")) return Promise.resolve([]);
      if (path.startsWith("/runs/r2/events")) return Promise.resolve([]);
      if (path.startsWith("/approvals")) return Promise.resolve([card]);
      if (path === "/repos") return Promise.resolve([]);
      return Promise.resolve(null);
    });
  });

  afterEach(() => {
    apiMock.get.mockReset();
    apiMock.post.mockReset();
  });

  it("opens ?run=, focuses ?card=, then strips the params", async () => {
    const seen: string[] = [];
    render(
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <MemoryRouter initialEntries={["/app?run=r2&card=c9"]}>
          <Routes>
            <Route path="/app" element={<><SessionsScreen /><LocationProbe seen={seen} /></>} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    );
    // The linked run is fetched and opened…
    await waitFor(() => expect(apiMock.get).toHaveBeenCalledWith("/runs/r2"));
    // …the linked card renders with the focus ring…
    const article = await screen.findByText("tool_call");
    expect(article.closest("article")?.id).toBe("approval-c9");
    await waitFor(() => expect(Element.prototype.scrollIntoView).toHaveBeenCalled());
    // …and the params were stripped (the LAST seen search must be clean).
    await waitFor(() => expect(seen[seen.length - 1]).toBe(""));
  });

  it("does nothing without a ?run param", async () => {
    render(
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <MemoryRouter initialEntries={["/app"]}>
          <Routes>
            <Route path="/app" element={<SessionsScreen />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    );
    await waitFor(() => expect(apiMock.get).toHaveBeenCalledWith("/runs"));
    expect(apiMock.get).not.toHaveBeenCalledWith("/runs/r2");
  });
});
