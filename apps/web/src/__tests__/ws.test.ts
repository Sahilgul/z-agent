import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { RunSocket } from "../lib/ws";

// G-25/G-26: RunSocket's reconnect backoff + close-during-pending-timer were
// untested. jsdom has no WebSocket, so install a minimal fake that records
// every instance and exposes the handler setters the implementation wires up.
// Drive the reconnect path with fake timers so the backoff schedule is
// observable without real time. The implementation reads the global
// `WebSocket` symbol at connect() call time, so installing the fake in
// beforeEach (before connect()) is sufficient.

type FakeWs = {
  url: string;
  onopen: ((e?: unknown) => void) | null;
  onmessage: ((e: { data: string }) => void) | null;
  onclose: ((e?: unknown) => void) | null;
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
    // First close (not by us) schedules a reconnect at 500 * 2**0 = 500ms.
    created[0].onclose!();
    expect(onState).toHaveBeenLastCalledWith(false);
    vi.advanceTimersByTime(499);
    expect(created.length).toBe(1);          // not yet
    vi.advanceTimersByTime(1);
    expect(created.length).toBe(2);          // 500ms fired

    // Second close (no open in between) → 500 * 2**1 = 1000ms.
    created[1].onclose!();
    vi.advanceTimersByTime(999);
    expect(created.length).toBe(2);
    vi.advanceTimersByTime(1);
    expect(created.length).toBe(3);          // 1000ms fired

    // Drive the backoff up to the cap (10_000ms). Each close without an open
    // doubles the delay until it hits MAX_BACKOFF_MS.
    created[2].onclose!();   // 500 * 2**2 = 2000ms
    vi.advanceTimersByTime(2000);
    expect(created.length).toBe(4);
    created[3].onclose!();   // 4000ms
    vi.advanceTimersByTime(4000);
    expect(created.length).toBe(5);
    created[4].onclose!();   // 8000ms
    vi.advanceTimersByTime(8000);
    expect(created.length).toBe(6);
    created[5].onclose!();   // min(10000, 16000) = 10000ms (capped)
    vi.advanceTimersByTime(10000);
    expect(created.length).toBe(7);
    // The cap holds: the next interval is still 10000ms, not 20000ms.
    created[6].onclose!();
    vi.advanceTimersByTime(9999);
    expect(created.length).toBe(7);          // not at 9999ms
    vi.advanceTimersByTime(1);
    expect(created.length).toBe(8);          // exactly 10000ms

    // --- reset: an open zeroes attempts, so the next close is 500ms again ---
    created[7].onopen!();
    expect(onState).toHaveBeenLastCalledWith(true);
    created[7].onclose!();                   // attempts reset → 500ms, not 10000ms
    vi.advanceTimersByTime(499);
    expect(created.length).toBe(8);
    vi.advanceTimersByTime(1);
    expect(created.length).toBe(9);          // 500ms fired (reset worked)

    sock.close();
  });

  it("close() cancels a pending reconnect timer (G-26)", () => {
    const sock = new RunSocket("r1", () => {}, () => {});
    sock.connect();
    expect(created.length).toBe(1);
    const first = created[0];

    // A close not initiated by us schedules a reconnect at 500ms.
    first.onclose!();
    // Before the timer fires, the user tears down the socket.
    sock.close();
    expect(first.close).toHaveBeenCalled();
    // Advance well past the scheduled 500ms — the reconnect must NOT fire.
    vi.advanceTimersByTime(5000);
    expect(created.length).toBe(1);
  });
});
