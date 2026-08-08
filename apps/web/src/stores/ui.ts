import { create } from "zustand";

/** Overlay stack: thread trace / plan / PR overlays open at 80% over the live
 *  session — the session never unmounts, so a closing overlay reveals a
 *  stream that kept flowing underneath. Screen routing lives in react-router;
 *  this store keeps only overlay modal state. */
export type Overlay =
  | { kind: "thread"; threadId: string }
  | { kind: "plan" }
  | { kind: "pr" };

/** Closing a tab hides it from the strip — it never deletes the run, which
 *  stays reachable from history. Persisted so the strip survives a reload. */
const CLOSED_KEY = "collegium.closed-tabs";

function loadClosed(): string[] {
  try {
    const raw = JSON.parse(localStorage.getItem(CLOSED_KEY) ?? "[]");
    return Array.isArray(raw) ? raw.filter((v): v is string => typeof v === "string") : [];
  } catch {
    return [];
  }
}

function saveClosed(ids: string[]) {
  try {
    localStorage.setItem(CLOSED_KEY, JSON.stringify(ids));
  } catch {
    /* private mode — the strip just won't survive a reload */
  }
}

/** User setting: the default model for swarm/subagent lanes (goal
 *  explorers, swarm slices). Persisted; sent as swarm_model on run
 *  creation so every subagent lane spawns on it regardless of the
 *  composer's lane selection. Null = follow the lane/default model. */
const SWARM_MODEL_KEY = "collegium.swarm-model";

function loadSwarmModel(): string | null {
  try {
    const raw = localStorage.getItem(SWARM_MODEL_KEY);
    return raw && typeof raw === "string" ? raw : null;
  } catch {
    return null;
  }
}

interface UiState {
  overlays: Overlay[];
  pushOverlay: (o: Overlay) => void;
  popOverlay: () => void;
  closedTabs: string[];
  closeTab: (runId: string) => void;
  reopenTab: (runId: string) => void;
  settingsOpen: boolean;
  setSettingsOpen: (open: boolean) => void;
  swarmModel: string | null;
  setSwarmModel: (alias: string | null) => void;
}

export const useUi = create<UiState>((set) => ({
  closedTabs: loadClosed(),
  closeTab: (runId) =>
    set((s) => {
      const next = s.closedTabs.includes(runId) ? s.closedTabs : [...s.closedTabs, runId];
      saveClosed(next);
      return { closedTabs: next };
    }),
  reopenTab: (runId) =>
    set((s) => {
      const next = s.closedTabs.filter((id) => id !== runId);
      saveClosed(next);
      return { closedTabs: next };
    }),
  overlays: [],
  pushOverlay: (o) => set((s) => ({ overlays: [...s.overlays, o] })),
  popOverlay: () => set((s) => ({ overlays: s.overlays.slice(0, -1) })),
  settingsOpen: false,
  setSettingsOpen: (open) => set({ settingsOpen: open }),
  swarmModel: loadSwarmModel(),
  setSwarmModel: (alias) => {
    try {
      if (alias) localStorage.setItem(SWARM_MODEL_KEY, alias);
      else localStorage.removeItem(SWARM_MODEL_KEY);
    } catch {
      /* private mode — the setting just won't survive a reload */
    }
    set({ swarmModel: alias });
  },
}));
