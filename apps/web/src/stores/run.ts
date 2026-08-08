import { create } from "zustand";
import { toast } from "@/components/ui/sonner";
import { api } from "../lib/api";
import { qk } from "../lib/queryKeys";
import { queryClient } from "../lib/queryClient";
import { RunSocket } from "../lib/ws";
import { useSession } from "./session";
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
  sendIntent: (intent: string | null, opts?: { threadId?: string; text?: string; confirmed?: boolean; payload?: Record<string, unknown> }) => Promise<Record<string, unknown>>;
  createRun: (body: { mode: string; task: string; repo?: string; fanout?: number; models?: string[]; reasoning?: Record<string, string>; images?: string[]; swarm_model?: string; idempotency_key?: string }) => Promise<Run>;
}

let socket: RunSocket | null = null;
// H-60: openRun is async (awaits 3 fetches before opening a socket), so two
// rapid calls race: the loser overwrites the winner's socket without
// closing it (leak) and the loser's fetches then clobber the winner's
// state (stale-socket corruption). A generation token lets the late
// resolver detect it lost and bail without touching the socket or store.
let openRunGen = 0;

// ---------------------------------------------------------------- event ingest
/** Identity of a stored event for upsert: (thread, seq) collides between a
 *  user-authored and an agent-authored message on the same turn, so role is
 *  part of the key (mirrors foldStream's React keys). */
function eventKey(e: StepEvent): string {
  return `${e.thread_id}:${e.seq}:${typeof e.detail?.role === "string" ? e.detail.role : "agent"}`;
}

/** Insert keeping per-thread seq monotonic: an out-of-order or refetched
 *  event slots before the first same-thread event with a higher seq instead
 *  of rendering at the tail; exact (thread, seq, role) hits replace in place
 *  (redelivery / resync overlap must never double-render). */
function upsertEvent(events: StepEvent[], e: StepEvent): StepEvent[] {
  const key = eventKey(e);
  const dup = events.findIndex((x) => eventKey(x) === key);
  if (dup >= 0) {
    if (events[dup] === e) return events;
    const next = events.slice();
    next[dup] = e;
    return next;
  }
  const higher = events.findIndex((x) => x.thread_id === e.thread_id && x.seq > e.seq);
  if (higher >= 0) {
    const next = events.slice();
    next.splice(higher, 0, e);
    return next;
  }
  return [...events, e];
}

function maxSeqForThread(events: StepEvent[], threadId: string): number {
  let max = -1;
  for (const e of events) if (e.thread_id === threadId && e.seq > max) max = e.seq;
  return max;
}

/** W-H11: page the replay endpoint until a short page — the server's
 *  per-request cap (500) must never truncate a long run's tail. The
 *  `after_seq` cursor is the max seq seen (the backend filters seq > it). */
const EVENTS_PAGE_CAP = 500;
async function fetchAllEvents(runId: string): Promise<StepEvent[]> {
  const all: StepEvent[] = [];
  let after: number | null = null;
  for (;;) {
    const page: StepEvent[] = await api.get<StepEvent[]>(
      after === null
        ? `/runs/${runId}/events`
        : `/runs/${runId}/events?after_seq=${after}`,
    );
    all.push(...page);
    if (page.length < EVENTS_PAGE_CAP) return all;
    after = page.reduce((m: number, e: StepEvent) => Math.max(m, e.seq), after ?? -1);
  }
}

/** The SDK normalize path upgrades the stored event to test_run while the
 *  live delta kept its pre-upgrade kind — exact-match clearing would strand
 *  a "typing…" bubble forever. */
function deltaMatchesEvent(d: Delta, e: StepEvent): boolean {
  if (d.thread_id !== e.thread_id) return false;
  return d.kind === e.kind || (e.kind === "test_run" && d.kind === "command");
}

const TERMINAL_STAGES = new Set(["completed", "failed", "abandoned", "interrupted"]);

// In-flight per-thread gap catch-ups — a burst of out-of-order events must
// not fan out into N overlapping fetches.
const pendingCatchups = new Set<string>();

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
        // W-H11: the replay endpoint caps at 500 rows — a long run used to
        // lose its tail (the socket then gap-catches per thread, but the
        // OPEN looked truncated). Page after_seq until a short page.
        fetchAllEvents(runId),
      ]);
      // A newer openRun (or closeRun) won the race — discard this result
      // and leave the socket/store to the winner.
      if (myGen !== openRunGen) return;
      set({ current: run, threads, events, deltas: [] });

      const catchup = (threadId: string) => {
        if (pendingCatchups.has(threadId)) return;
        pendingCatchups.add(threadId);
        const after = maxSeqForThread(get().events, threadId);
        void api
          .get<StepEvent[]>(`/runs/${runId}/events?thread_id=${encodeURIComponent(threadId)}&after_seq=${after}`)
          .then((missed) => {
            if (myGen !== openRunGen) return;
            set((state) => ({ events: missed.reduce(upsertEvent, state.events) }));
          })
          .catch(() => { /* a failed catch-up just leaves the gap until resync */ })
          .finally(() => pendingCatchups.delete(threadId));
      };

      const resync = async () => {
        // W-H2: fanout is ephemeral — after any drop, refetch everything the
        // socket would have delivered (run row, threads, missed events) and
        // re-arm the approval queue.
        try {
          const [run, threads, replay] = await Promise.all([
            api.get<Run>(`/runs/${runId}`),
            api.get<Thread[]>(`/runs/${runId}/threads`),
            fetchAllEvents(runId),
          ]);
          if (myGen !== openRunGen) return;
          set((state) => ({
            current: run,
            threads,
            events: replay.reduce(upsertEvent, state.events),
            deltas: TERMINAL_STAGES.has(run.stage) ? [] : state.deltas,
          }));
          void queryClient.invalidateQueries({ queryKey: [...qk.approvals, runId] });
        } catch {
          /* resync failure leaves the poll/heartbeat paths to self-heal */
        }
      };

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
            if (msg.event.schema_version !== undefined && msg.event.schema_version !== 1) {
              console.warn("step event with unknown schema_version", msg.event.schema_version);
            }
            const priorMax = maxSeqForThread(s.events, msg.event.thread_id);
            if (priorMax >= 0 && msg.event.seq > priorMax + 1) catchup(msg.event.thread_id);
            set({
              events: upsertEvent(s.events, msg.event),
              deltas: s.deltas.filter((d) => !deltaMatchesEvent(d, msg.event)),
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
            // M-80: use the functional updater so we read the LATEST
            // state.current, not the snapshot captured at the top of this
            // onMessage (s.current). A concurrent sendIntent -> set({current:
            // fresh}) in the same tick would otherwise be clobbered by
            // ...s.current (stale) spread here, losing the fresh run data.
            set((state) =>
              state.current
                ? {
                    current: {
                      ...state.current,
                      stage: msg.stage,
                      available_actions: msg.available_actions,
                    },
                    // A terminal stage means no stored event is still coming —
                    // any live "typing…" bubble would otherwise linger forever.
                    deltas: TERMINAL_STAGES.has(msg.stage) ? [] : state.deltas,
                  }
                : {},
            );
            // threads move with stage transitions — refresh silently
            void api.get<Thread[]>(`/runs/${runId}/threads`).then((threads) => {
              if (myGen === openRunGen) set({ threads });
            });
            // W3-M7: the inbox lists this run's stage/cost too — a stage
            // change (incl. a background run dying) must refresh the list or
            // the tab strip lies until the next manual load.
            void get().loadRuns();
          } else if (msg.type === "note") {
            // L-22: a run-scoped informational note (e.g. swarm capped a
            // fanout request). Surface it as a transient toast so the UI
            // actually "says so" — the old misuse of thread_status was
            // silently dropped because no thread matched the fake id.
            toast(msg.text);
          } else if (msg.type === "repo_added") {
            // W1-L1 / W9-L10: onboarding finished while a run was open —
            // refresh the rack (and the composer mention list, same query).
            void queryClient.invalidateQueries({ queryKey: qk.repos });
            toast(`repo ready: ${msg.repo}`);
          } else {
            console.warn("unknown WS message type", (msg as { type?: unknown }).type);
          }
        },
        (connected) => {
          if (myGen !== openRunGen) return;
          set({ socketConnected: connected });
          // Socket dropped on a terminal run: no stored event will ever land
          // to supersede the live bubbles.
          if (!connected && TERMINAL_STAGES.has(get().current?.stage ?? "")) {
            set({ deltas: [] });
          }
        },
        () => void resync(),
        () => useSession.setState({ me: null }),
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
        // W6-M10: free-typed composer text goes intent-LESS so the backend's
        // classify_text runs against the CURRENT legal move set — pre-typing
        // "send_message" skipped classification entirely, so "approve the
        // plan" typed at the awaiting_user gate never reached approve_plan.
        ...(intent === null ? {} : { intent }),
        source: opts.text ? "text" : "button",
        thread_id: opts.threadId ?? null,
        text: opts.text ?? null,
        confirmed: opts.confirmed ?? false,
        payload: opts.payload ?? {},
      });
      const fresh = await api.get<Run>(`/runs/${run.id}`);
      // M-81: if the user switched runs while this intent was in flight,
      // the fetched `fresh` belongs to the OLD run — writing it would
      // clobber the new run's current with stale data. Only commit if
      // we're still on the same run (mirrors the H-61 guard in refreshLanes).
      if (get().current?.id === run.id) set({ current: fresh });
      return res;
    } catch (err) {
      toast.error(intent === null ? "send failed" : `${intent} failed`, {
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
