/** Per-run WebSocket with capped backoff reconnect.
 *  Cookie-authenticated — same-origin, no token handling here. */

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
  ) {}

  connect(): void {
    this.closedByUs = false;
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${proto}://${location.host}/ws/runs/${this.runId}`);
    this.ws = ws;

    ws.onopen = () => {
      this.attempts = 0;
      this.onState?.(true);
    };
    ws.onmessage = (e) => {
      try {
        this.onMessage(JSON.parse(e.data as string) as WsMessage);
      } catch {
        /* a malformed frame must not kill the stream */
      }
    };
    ws.onclose = () => {
      this.onState?.(false);
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
    const backoff = Math.min(MAX_BACKOFF_MS, 500 * 2 ** this.attempts);
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
