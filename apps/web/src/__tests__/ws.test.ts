import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { RunSocket } from "../lib/ws";

// G-25/G-26: RunSocket's reconnect backoff + close-during-pending-timer were
// untested. jsdom has no WebSocket, so install a minimal fake that records
// every instance and exposes the handler setters the implementation wires up.
// Drive the reconnect path with fake timers so the backoff schedule is
// observable without real time. The implementation reads the global
// `WebSocket` symbol at connect() call time, so installing the fake in
// beforeEach (before connect()) is sufficient.
//
// W-H2 update: backoff is now JITTERED (base * [0.5, 1.0]) so restarted
// backends don't get a reconnect thundering herd — assertions check the
// [base/2, base] window instead of an exact delay. Also covers the terminal
// close codes (4401 = unauthorized, 4404 = foreign run) and the resync hook.

type FakeWs = {
  url: string;
  onopen: ((e?: unknown) => void) | null;
  onmessage: ((e: { data: string }) => void) | null;
  onclose: ((e?: { code?: number }) => void) | null;
  onerror: ((e?: unknown) => void) | null;
  close: ReturnType<typeof vi.fn>;
};

let created: FakeWs[];
let WebSocketCtor: ReturnType<typeof vi.fn>;

function installFakeWebSocket() {
  created = [];
  WebSocketCtor = vi.fn((url: string) => {
    const ws: FakeWs = {
      url,
      onopen: null,
      onmessage: null,
      onclose: null,
      onerror: null,
      close: vi.fn(),
    };
    created.push(ws);
    return ws;
  });
  // The implementation reads the global `WebSocket` symbol at call time.
  (globalThis as unknown as { WebSocket: unknown }).WebSocket = WebSocketCtor;
  return { created, WebSocketCtor };
}

describe("RunSocket reconnect", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    installFakeWebSocket();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("uses capped exponential backoff and resets attempts on open (G-25)", () => {
    const onState = vi.fn();
    const sock = new RunSocket("r1", () => {}, onState);
    sock.connect();
    expect(created.length).toBe(1);

    // --- escalation: repeated closes WITHOUT an open double the backoff ---
    // First close (not by us) schedules a reconnect in [250, 500]ms.
    created[0].onclose!();
    expect(onState).toHaveBeenLastCalledWith(false);
    vi.advanceTimersByTime(249);
    expect(created.length).toBe(1);          // not before the jitter window opens
    vi.advanceTimersByTime(251);
    expect(created.length).toBe(2);          // fired by the end of the window

    // Second close (no open in between) → window [500, 1000]ms.
    created[1].onclose!();
    vi.advanceTimersByTime(499);
    expect(created.length).toBe(2);
    vi.advanceTimersByTime(501);
    expect(created.length).toBe(3);

    // Drive the backoff up to the cap (10_000ms). Each close without an open
    // doubles the delay until it hits MAX_BACKOFF_MS.
    created[2].onclose!();   // [1000, 2000]ms
    vi.advanceTimersByTime(2000);
    expect(created.length).toBe(4);
    created[3].onclose!();   // [2000, 4000]ms
    vi.advanceTimersByTime(4000);
    expect(created.length).toBe(5);
    created[4].onclose!();   // [4000, 8000]ms
    vi.advanceTimersByTime(8000);
    expect(created.length).toBe(6);
    created[5].onclose!();   // min(10000, 16000) → [5000, 10000]ms (capped)
    vi.advanceTimersByTime(10000);
    expect(created.length).toBe(7);
    // The cap holds: the next window is still ≤10000ms, not 20000ms.
    created[6].onclose!();
    vi.advanceTimersByTime(4999);
    expect(created.length).toBe(7);          // not before the window opens
    vi.advanceTimersByTime(5001);
    expect(created.length).toBe(8);          // fired by 10000ms

    // --- reset: an open zeroes attempts, so the next close is ≤500ms again ---
    created[7].onopen!();
    expect(onState).toHaveBeenLastCalledWith(true);
    created[7].onclose!();                   // attempts reset → [250, 500]ms
    vi.advanceTimersByTime(249);
    expect(created.length).toBe(8);
    vi.advanceTimersByTime(251);
    expect(created.length).toBe(9);          // fired (reset worked)

    sock.close();
  });

  it("close() cancels a pending reconnect timer (G-26)", () => {
    const sock = new RunSocket("r1", () => {}, () => {});
    sock.connect();
    expect(created.length).toBe(1);
    const first = created[0];

    // A close not initiated by us schedules a reconnect within 500ms.
    first.onclose!();
    // Before the timer fires, the user tears down the socket.
    sock.close();
    expect(first.close).toHaveBeenCalled();
    // Advance well past the scheduled window — the reconnect must NOT fire.
    vi.advanceTimersByTime(5000);
    expect(created.length).toBe(1);
  });

  it("fires onReconnect only after a drop, never on the first open (W-H2)", () => {
    const onReconnect = vi.fn();
    const sock = new RunSocket("r1", () => {}, () => {}, onReconnect);
    sock.connect();
    created[0].onopen!();
    expect(onReconnect).not.toHaveBeenCalled(); // first connect is not a resync

    created[0].onclose!();
    vi.advanceTimersByTime(1000); // past the first jitter window
    expect(created.length).toBe(2);
    created[1].onopen!();
    expect(onReconnect).toHaveBeenCalledTimes(1);
    sock.close();
  });

  it("4401 closes are terminal and notify the unauthorized handler", () => {
    const onUnauthorized = vi.fn();
    const sock = new RunSocket("r1", () => {}, () => {}, undefined, onUnauthorized);
    sock.connect();
    created[0].onclose!({ code: 4401 });
    expect(onUnauthorized).toHaveBeenCalledTimes(1);
    vi.advanceTimersByTime(60000);
    expect(created.length).toBe(1); // never retries a dead session
    sock.close();
  });

  it("4404 closes do not retry (foreign/gone run)", () => {
    const onUnauthorized = vi.fn();
    const sock = new RunSocket("r1", () => {}, () => {}, undefined, onUnauthorized);
    sock.connect();
    created[0].onclose!({ code: 4404 });
    expect(onUnauthorized).not.toHaveBeenCalled();
    vi.advanceTimersByTime(60000);
    expect(created.length).toBe(1);
    sock.close();
  });
});
