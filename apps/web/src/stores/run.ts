import { create } from "zustand";
import { toast } from "@/components/ui/sonner";
import { api } from "../lib/api";
import { qk } from "../lib/queryKeys";
import { queryClient } from "../lib/queryClient";
import { RunSocket } from "../lib/ws";
import type { Thread, Run, StepEvent, WsMessage } from "../types";

interface Delta {
  thread_id: string;
  kind: string;
  text: string;
}

interface RunState {
  runs: Run[];
  runsLoaded: boolean;
  current: Run | null;
  threads: Thread[];
  events: StepEvent[];
  deltas: Delta[];
  socketConnected: boolean;
  loadRuns: () => Promise<void>;
  openRun: (runId: string) => Promise<void>;
  closeRun: () => void;
  refreshLanes: () => Promise<void>;
  sendIntent: (intent: string, opts?: { threadId?: string; text?: string; confirmed?: boolean; payload?: Record<string, unknown> }) => Promise<Record<string, unknown>>;
  createRun: (body: { mode: string; task: string; repo?: string; fanout?: number }) => Promise<Run>;
}

let socket: RunSocket | null = null;
// H-60: openRun is async (awaits 3 fetches before opening a socket), so two
// rapid calls race: the loser overwrites the winner's socket without
// closing it (leak) and the loser's fetches then clobber the winner's
// state (stale-socket corruption). A generation token lets the late
// resolver detect it lost and bail without touching the socket or store.
let openRunGen = 0;

export const useRuns = create<RunState>((set, get) => ({
  runs: [],
  runsLoaded: false,
  current: null,
  threads: [],
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
    const myGen = ++openRunGen;
    socket?.close();
    socket = null;
    try {
      const [run, threads, events] = await Promise.all([
        api.get<Run>(`/runs/${runId}`),
        api.get<Thread[]>(`/runs/${runId}/threads`),
        api.get<StepEvent[]>(`/runs/${runId}/events`),
      ]);
      // A newer openRun (or closeRun) won the race — discard this result
      // and leave the socket/store to the winner.
      if (myGen !== openRunGen) return;
      set({ current: run, threads, events, deltas: [] });

      socket = new RunSocket(
        runId,
        (msg: WsMessage) => {
          const s = get();
          // Drop messages from a socket that lost the race before it closed.
          if (myGen !== openRunGen) return;
          if (msg.type === "step") {
            // The worker emits a delta stream AND a stored event for the same
            // block. Once the event lands it supersedes its live bubble —
            // keeping both is what rendered the answer twice.
            set({
              events: [...s.events, msg.event],
              deltas: s.deltas.filter(
                (d) => !(d.thread_id === msg.event.thread_id && d.kind === msg.event.kind),
              ),
            });
          } else if (msg.type === "delta") {
            set({ deltas: [...s.deltas, msg.delta] });
          } else if (msg.type === "thread_status") {
            set({
              threads: s.threads.map((l) =>
                l.id === msg.thread_id ? { ...l, status: msg.status as Thread["status"] } : l,
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
            // threads move with stage transitions — refresh silently
            void api.get<Thread[]>(`/runs/${runId}/threads`).then((threads) => {
              if (myGen === openRunGen) set({ threads });
            });
          }
        },
        (connected) => {
          if (myGen === openRunGen) set({ socketConnected: connected });
        },
      );
      socket.connect();
    } catch (err) {
      if (myGen !== openRunGen) throw err;
      toast.error("couldn't open run", {
        description: err instanceof Error ? err.message : "run may not exist",
      });
      throw err;
    }
  },

  closeRun: () => {
    openRunGen++; // cancel any in-flight openRun so it can't resurrect a socket
    socket?.close();
    socket = null;
    set({ current: null, threads: [], events: [], deltas: [], socketConnected: false });
  },

  // Lanes carry heartbeat_at, which the watchdog reads against wall time —
  // it must be re-polled or the UI judges liveness from a frozen snapshot.
  refreshLanes: async () => {
    const run = get().current;
    if (!run) return;
    try {
      const threads = await api.get<Thread[]>(`/runs/${run.id}/threads`);
      // H-61: if the user switched runs while this poll was in flight, the
      // fetched threads belong to the OLD run — writing them would clobber
      // the new run's threads with stale data. Only commit if we're still
      // on the same run.
      if (get().current?.id === run.id) set({ threads });
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
        thread_id: opts.threadId ?? null,
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
