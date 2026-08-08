import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Run, StepEvent, Thread, WsMessage } from "../types";

// First direct tests of the run store's WS handler (W0): the socket is faked
// at the global WebSocket seam (same pattern as ws.test.ts) and `api` is
// module-mocked per test. Pins: step upsert/dedupe, per-thread gap catch-up,
// terminal-stage delta clearing, reconnect resync, unknown-status tolerance.

type FakeWs = {
  url: string;
  onopen: (() => void) | null;
  onmessage: ((e: { data: string }) => void) | null;
  onclose: ((e?: { code?: number }) => void) | null;
  onerror: (() => void) | null;
  close: ReturnType<typeof vi.fn>;
};

let sockets: FakeWs[];

const apiMock = {
  get: vi.fn<(path: string) => Promise<unknown>>(),
  post: vi.fn<(path: string, body?: unknown) => Promise<unknown>>(),
  patch: vi.fn(),
};

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

// The store talks to the session store only for the 4401 path.
vi.mock("../stores/session", () => ({
  useSession: { setState: vi.fn() },
}));

import { useRuns } from "../stores/run";

const run: Run = {
  id: "r1", mode: "ask", autonomy: "auto", stage: "developing", title: "t",
  auto_summary: null, repo: null, work_item_id: null, available_actions: [],
  failure_reason: null,
  cost_usd: 0, tokens: 0, last_active_at: null, created_at: null,
};

const thread: Thread = {
  id: "l1", persona: "lead", repo_scope: null, status: "running",
  cost_usd: 0, budget_usd: 0, steps: 0, forked_from_session_id: null,
  heartbeat_at: null, has_container: true, created_at: null, finished_at: null,
};

const ev = (seq: number, kind: StepEvent["kind"] = "message", detail: Record<string, unknown> = {}): StepEvent => ({
  schema_version: 1, run_id: "r1", thread_id: "l1", seq,
  ts: "2026-08-01T00:00:00Z", kind, title: `event ${seq}`, detail,
  sdk_message_uuid: null,
});

function installFakeWebSocket() {
  sockets = [];
  (globalThis as unknown as { WebSocket: unknown }).WebSocket = vi.fn((url: string) => {
    const ws: FakeWs = { url, onopen: null, onmessage: null, onclose: null, onerror: null, close: vi.fn() };
    sockets.push(ws);
    return ws;
  });
}

function send(msg: WsMessage) {
  sockets[sockets.length - 1].onmessage!({ data: JSON.stringify(msg) });
}

async function openRun() {
  apiMock.get.mockImplementation((path: string) => {
    if (path === "/runs/r1") return Promise.resolve(run);
    if (path === "/runs/r1/threads") return Promise.resolve([thread]);
    if (path === "/runs/r1/events") return Promise.resolve([ev(0)]);
    return Promise.reject(new Error(`unexpected GET ${path}`));
  });
  await useRuns.getState().openRun("r1");
}

describe("run store WS handler", () => {
  beforeEach(() => {
    installFakeWebSocket();
    apiMock.get.mockReset();
    apiMock.post.mockReset();
    useRuns.setState({ runs: [], runsLoaded: false, current: null, threads: [], events: [], deltas: [], socketConnected: false });
  });
  afterEach(() => {
    useRuns.getState().closeRun();
  });

  it("hydrates from REST and marks the socket connected on open", async () => {
    await openRun();
    expect(useRuns.getState().events).toHaveLength(1);
    expect(useRuns.getState().threads).toHaveLength(1);
    sockets[0].onopen!();
    expect(useRuns.getState().socketConnected).toBe(true);
  });

  it("W-H11: openRun pages the replay endpoint until a short page (no 500-cap tail loss)", async () => {
    const page1 = Array.from({ length: 500 }, (_, i) => ev(i));
    apiMock.get.mockImplementation((path: string) => {
      if (path === "/runs/r1") return Promise.resolve(run);
      if (path === "/runs/r1/threads") return Promise.resolve([thread]);
      if (path === "/runs/r1/events") return Promise.resolve(page1);
      if (path === "/runs/r1/events?after_seq=499") return Promise.resolve([ev(500)]);
      return Promise.reject(new Error(`unexpected GET ${path}`));
    });
    await useRuns.getState().openRun("r1");
    expect(useRuns.getState().events).toHaveLength(501);
    expect(useRuns.getState().events.some((e) => e.seq === 500)).toBe(true);
  });

  it("W1-L1: a repo_added global invalidates the repos query instead of warning unknown-type", async () => {
    const { queryClient } = await import("../lib/queryClient");
    const spy = vi.spyOn(queryClient, "invalidateQueries");
    await openRun();
    send({ type: "repo_added", repo: "LivekitScribe" } as WsMessage);
    expect(spy).toHaveBeenCalledWith(expect.objectContaining({ queryKey: ["repos"] }));
  });

  it("dedupes a redelivered step by (thread, seq, role) instead of double-rendering", async () => {
    await openRun();
    send({ type: "step", event: ev(1) });
    send({ type: "step", event: ev(1) });
    expect(useRuns.getState().events).toHaveLength(2);
  });

  it("keeps a user message and an agent event on the same seq as separate rows", async () => {
    await openRun();
    send({ type: "step", event: ev(1, "message", { text: "hi", role: "user" }) });
    send({ type: "step", event: ev(1, "message", { text: "answer", role: "agent" }) });
    const events = useRuns.getState().events;
    expect(events).toHaveLength(3);
    expect(events.filter((e) => e.seq === 1)).toHaveLength(2);
  });

  it("inserts an out-of-order event before the higher-seq same-thread row", async () => {
    await openRun();
    send({ type: "step", event: ev(1) });
    // ev(3) opens a gap → catch-up fetch fires synchronously; mock it empty first.
    apiMock.get.mockImplementation((path: string) =>
      path.startsWith("/runs/r1/events?") ? Promise.resolve([]) : Promise.reject(new Error(path)),
    );
    send({ type: "step", event: ev(3) });
    send({ type: "step", event: ev(2) });
    const seqs = useRuns.getState().events.map((e) => e.seq);
    expect(seqs).toEqual([0, 1, 2, 3]);
  });

  it("detects a seq gap and fetches the missed events for that thread", async () => {
    await openRun();
    send({ type: "step", event: ev(1) });
    const missed = [ev(2), ev(3)];
    apiMock.get.mockImplementation((path: string) => {
      if (path === "/runs/r1/events?thread_id=l1&after_seq=1") return Promise.resolve(missed);
      return Promise.reject(new Error(`unexpected GET ${path}`));
    });
    send({ type: "step", event: ev(4) });
    await vi.waitFor(() => expect(useRuns.getState().events.map((e) => e.seq)).toEqual([0, 1, 2, 3, 4]));
  });

  it("a stored event supersedes its live delta bubble (incl. command→test_run upgrade)", async () => {
    await openRun();
    send({ type: "delta", delta: { run_id: "r1", thread_id: "l1", kind: "command", text: "pytest -q" } });
    send({ type: "delta", delta: { run_id: "r1", thread_id: "l1", kind: "thinking", text: "…" } });
    expect(useRuns.getState().deltas).toHaveLength(2);
    send({ type: "step", event: ev(1, "test_run") });
    const deltas = useRuns.getState().deltas;
    expect(deltas).toHaveLength(1);
    expect(deltas[0].kind).toBe("thinking");
  });

  it("clears live deltas when the run goes terminal", async () => {
    await openRun();
    send({ type: "delta", delta: { run_id: "r1", thread_id: "l1", kind: "thinking", text: "…" } });
    send({ type: "run_stage", stage: "failed", available_actions: [] });
    expect(useRuns.getState().deltas).toEqual([]);
    expect(useRuns.getState().current?.stage).toBe("failed");
  });

  it("clears live deltas when the socket drops on a terminal run", async () => {
    await openRun();
    send({ type: "delta", delta: { run_id: "r1", thread_id: "l1", kind: "thinking", text: "…" } });
    send({ type: "run_stage", stage: "completed", available_actions: [] });
    sockets[0].onclose!();
    expect(useRuns.getState().deltas).toEqual([]);
  });

  it("resyncs on reconnect: refetches run, threads, events and merges without dupes", async () => {
    await openRun();
    send({ type: "step", event: ev(1) });
    sockets[0].onopen!(); // first open — NOT a resync
    expect(apiMock.get.mock.calls.filter(([p]) => p === "/runs/r1/events")).toHaveLength(1);

    // Drop, then reconnect with the server having moved on.
    vi.useFakeTimers();
    sockets[0].onclose!();
    await vi.advanceTimersByTimeAsync(1000); // past the first jitter window
    expect(sockets.length).toBe(2);
    apiMock.get.mockImplementation((path: string) => {
      if (path === "/runs/r1") return Promise.resolve({ ...run, stage: "completed" });
      if (path === "/runs/r1/threads") return Promise.resolve([{ ...thread, status: "completed" }]);
      if (path === "/runs/r1/events") return Promise.resolve([ev(0), ev(1), ev(2)]);
      return Promise.reject(new Error(`unexpected GET ${path}`));
    });
    sockets[1].onopen!();
    await vi.advanceTimersByTimeAsync(0); // flush the resync's fetches
    vi.useRealTimers();
    const s = useRuns.getState();
    expect(s.events.map((e) => e.seq)).toEqual([0, 1, 2]);
    expect(s.current?.stage).toBe("completed");
    expect(s.threads[0].status).toBe("completed");
  });

  it("applies a thread_status for a known thread and tolerates unknown statuses", async () => {
    await openRun();
    send({ type: "thread_status", thread_id: "l1", status: "input_required" });
    expect(useRuns.getState().threads[0].status).toBe("input_required");
    send({ type: "thread_status", thread_id: "ghost", status: "running" });
    expect(useRuns.getState().threads).toHaveLength(1);
  });
});
