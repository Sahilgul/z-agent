import { create } from "zustand";
import { toast } from "@/components/ui/sonner";
import { api } from "../lib/api";
import { qk } from "../lib/queryKeys";
import { queryClient } from "../lib/queryClient";
import { RunSocket } from "../lib/ws";
import type { Lane, Run, StepEvent, WsMessage } from "../types";

interface Delta {
  lane_id: string;
  kind: string;
  text: string;
}

interface RunState {
  runs: Run[];
  runsLoaded: boolean;
  current: Run | null;
  lanes: Lane[];
  events: StepEvent[];
  deltas: Delta[];
  socketConnected: boolean;
  loadRuns: () => Promise<void>;
  openRun: (runId: string) => Promise<void>;
  closeRun: () => void;
  refreshLanes: () => Promise<void>;
  sendIntent: (intent: string, opts?: { laneId?: string; text?: string; confirmed?: boolean; payload?: Record<string, unknown> }) => Promise<Record<string, unknown>>;
  createRun: (body: { mode: string; task: string; repo?: string; fanout?: number }) => Promise<Run>;
}

let socket: RunSocket | null = null;

export const useRuns = create<RunState>((set, get) => ({
  runs: [],
  runsLoaded: false,
  current: null,
  lanes: [],
  events: [],
  deltas: [],
  socketConnected: false,

  loadRuns: async () => {
    try {
      const runs = await api.get<Run[]>("/runs");
      set({ runs, runsLoaded: true });
    } catch {
      set({ runsLoaded: true });
    }
  },

  openRun: async (runId) => {
    socket?.close();
    try {
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
            // The worker emits a delta stream AND a stored event for the same
            // block. Once the event lands it supersedes its live bubble —
            // keeping both is what rendered the answer twice.
            set({
              events: [...s.events, msg.event],
              deltas: s.deltas.filter(
                (d) => !(d.lane_id === msg.event.lane_id && d.kind === msg.event.kind),
              ),
            });
          } else if (msg.type === "delta") {
            set({ deltas: [...s.deltas, msg.delta] });
          } else if (msg.type === "lane_status") {
            set({
              lanes: s.lanes.map((l) =>
                l.id === msg.lane_id ? { ...l, status: msg.status as Lane["status"] } : l,
              ),
            });
          } else if (msg.type === "approval_card" || msg.type === "approval_resolved") {
            // Cards arrive and expire on the run socket; refetching is what makes
            // them appear (and vanish) immediately rather than on the next poll.
            void queryClient.invalidateQueries({ queryKey: [...qk.approvals, runId] });
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
    } catch (err) {
      toast.error("couldn't open run", {
        description: err instanceof Error ? err.message : "run may not exist",
      });
      throw err;
    }
  },

  closeRun: () => {
    socket?.close();
    socket = null;
    set({ current: null, lanes: [], events: [], deltas: [], socketConnected: false });
  },

  // Lanes carry heartbeat_at, which the watchdog reads against wall time —
  // it must be re-polled or the UI judges liveness from a frozen snapshot.
  refreshLanes: async () => {
    const run = get().current;
    if (!run) return;
    try {
      const lanes = await api.get<Lane[]>(`/runs/${run.id}/lanes`);
      set({ lanes });
    } catch {
      /* a failed poll just leaves the previous snapshot in place */
    }
  },

  sendIntent: async (intent, opts = {}) => {
    const run = get().current;
    if (!run) throw new Error("no open run");
    try {
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
    } catch (err) {
      toast.error(`${intent} failed`, {
        description: err instanceof Error ? err.message : undefined,
      });
      throw err;
    }
  },

  createRun: async (body) => {
    try {
      const run = await api.post<Run>("/runs", body);
      await get().loadRuns();
      // No success toast: the screen already swaps to the live session, so a
      // popup only repeats what you can see — over the send button, no less.
      return run;
    } catch (err) {
      toast.error("couldn't start run", {
        description: err instanceof Error ? err.message : undefined,
      });
      throw err;
    }
  },
}));
