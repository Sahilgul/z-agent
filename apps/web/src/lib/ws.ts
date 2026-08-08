/** Per-run WebSocket with capped backoff reconnect.
 *  Cookie-authenticated — same-origin, no token handling here.
 *
 *  Reconnect contract (W-H2): fanout is ephemeral — the backend relay keeps
 *  no replay buffer and its slow-consumer eviction design states "the client
 *  resyncs on reconnect" (backend/app/events/relay.py, app/ws/events.py).
 *  So after any drop, `onReconnect` fires before the connected flag flips —
 *  the store uses it to refetch the run, its threads, and any missed events
 *  (`?after_seq=`), plus invalidate approvals.
 *
 *  Terminal close codes: 4401 = session expired/revoked (treat like a REST
 *  401 — notify the unauthorized handler, never retry); 4404 = foreign run
 *  (no retry). Anything else retries with jittered exponential backoff. */

import type { WsMessage } from "../types";

type Handler = (msg: WsMessage) => void;
type StateHandler = (connected: boolean) => void;

const MAX_BACKOFF_MS = 10_000;

export class RunSocket {
  private ws: WebSocket | null = null;
  private attempts = 0;
  private closedByUs = false;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;

  constructor(
    private runId: string,
    private onMessage: Handler,
    private onState?: StateHandler,
    private onReconnect?: () => void,
    private onUnauthorized?: () => void,
  ) {}

  connect(): void {
    this.closedByUs = false;
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${proto}://${location.host}/ws/runs/${this.runId}`);
    this.ws = ws;

    ws.onopen = () => {
      const isReconnect = this.attempts > 0;
      this.attempts = 0;
      if (isReconnect) this.onReconnect?.();
      this.onState?.(true);
    };
    ws.onmessage = (e) => {
      try {
        this.onMessage(JSON.parse(e.data as string) as WsMessage);
      } catch {
        /* a malformed frame must not kill the stream */
      }
    };
    ws.onclose = (e) => {
      this.onState?.(false);
      if (e?.code === 4401) {
        // Session is dead — retrying forever just churns reconnects against
        // a logged-out cookie. Surface it exactly like a REST 401.
        this.onUnauthorized?.();
        return;
      }
      if (e?.code === 4404) return; // foreign/gone run — nothing to retry
      if (!this.closedByUs) this.scheduleReconnect();
    };
    // L-40: close the instance's canonical socket reference, not the
    // captured local `ws` — if this.ws was reassigned (a reconnect or an
    // explicit close()) the local would close a stale socket while the
    // live one kept this.ws inconsistent. Closing this.ws keeps the
    // instance state and the close action on the same reference.
    ws.onerror = () => this.ws?.close();
  }

  private scheduleReconnect(): void {
    // Jittered backoff: after a backend restart every client used to
    // reconnect in lockstep at identical delays.
    const base = Math.min(MAX_BACKOFF_MS, 500 * 2 ** this.attempts);
    const backoff = Math.round(base * (0.5 + Math.random() * 0.5));
    this.attempts += 1;
    this.reconnectTimer = setTimeout(() => this.connect(), backoff);
  }

  close(): void {
    this.closedByUs = true;
    if (this.reconnectTimer) clearTimeout(this.reconnectTimer);
    this.ws?.close();
    this.ws = null;
  }
}
