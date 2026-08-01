import { create } from "zustand";
import { api } from "../lib/api";
import { RunSocket } from "../lib/ws";
import type { Lane, Run, StepEvent, WsMessage } from "../types";

interface Delta {
  lane_id: string;
  kind: string;
  text: string;
}

interface RunState {
  runs: Run[];
  current: Run | null;
  lanes: Lane[];
  events: StepEvent[];
  deltas: Delta[];
  socketConnected: boolean;
  loadRuns: () => Promise<void>;
  openRun: (runId: string) => Promise<void>;
  closeRun: () => void;
  sendIntent: (intent: string, opts?: { laneId?: string; text?: string; confirmed?: boolean; payload?: Record<string, unknown> }) => Promise<Record<string, unknown>>;
  createRun: (body: { mode: string; task: string; repo?: string; fanout?: number }) => Promise<Run>;
}

let socket: RunSocket | null = null;

export const useRuns = create<RunState>((set, get) => ({
  runs: [],
  current: null,
  lanes: [],
  events: [],
  deltas: [],
  socketConnected: false,

  loadRuns: async () => {
    const runs = await api.get<Run[]>("/runs");
    set({ runs });
  },

  openRun: async (runId) => {
    socket?.close();
    const [run, lanes, events] = await Promise.all([
      api.get<Run>(`/runs/${runId}`),
      api.get<Lane[]>(`/runs/${runId}/lanes`),
      api.get<StepEvent[]>(`/runs/${runId}/events`),
    ]);
    set({ current: run, lanes, events, deltas: [] });

    socket = new RunSocket(
      runId,
      (msg: WsMessage) => {
        const s = get();
        if (msg.type === "step") {
          set({ events: [...s.events, msg.event] });
        } else if (msg.type === "delta") {
          set({ deltas: [...s.deltas, msg] });
        } else if (msg.type === "lane_status") {
          set({
            lanes: s.lanes.map((l) =>
              l.id === msg.lane_id ? { ...l, status: msg.status as Lane["status"] } : l,
            ),
          });
        } else if (msg.type === "run_stage" && s.current) {
          set({
            current: { ...s.current, stage: msg.stage, available_actions: msg.available_actions },
          });
          // lanes move with stage transitions — refresh silently
          void api.get<Lane[]>(`/runs/${runId}/lanes`).then((lanes) => set({ lanes }));
        }
      },
      (connected) => set({ socketConnected: connected }),
    );
    socket.connect();
  },

  closeRun: () => {
    socket?.close();
    socket = null;
    set({ current: null, lanes: [], events: [], deltas: [], socketConnected: false });
  },

  sendIntent: async (intent, opts = {}) => {
    const run = get().current;
    if (!run) throw new Error("no open run");
    const res = await api.post<Record<string, unknown>>(`/runs/${run.id}/intent`, {
      intent,
      source: opts.text ? "text" : "button",
      lane_id: opts.laneId ?? null,
      text: opts.text ?? null,
      confirmed: opts.confirmed ?? false,
      payload: opts.payload ?? {},
    });
    const fresh = await api.get<Run>(`/runs/${run.id}`);
    set({ current: fresh });
    return res;
  },

  createRun: async (body) => {
    const run = await api.post<Run>("/runs", body);
    await get().loadRuns();
    return run;
  },
}));
