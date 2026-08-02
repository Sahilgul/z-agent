import { create } from "zustand";

/** Overlay stack: lane trace / plan / PR overlays open at 80% over the live
 *  session — the session never unmounts, so a closing overlay reveals a
 *  stream that kept flowing underneath. Screen routing lives in react-router;
 *  this store keeps only overlay modal state. */
export type Overlay =
  | { kind: "lane"; laneId: string }
  | { kind: "plan" }
  | { kind: "pr" };

/** Closing a tab hides it from the strip — it never deletes the run, which
 *  stays reachable from history. Persisted so the strip survives a reload. */
const CLOSED_KEY = "zagent.closed-tabs";

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

interface UiState {
  overlays: Overlay[];
  pushOverlay: (o: Overlay) => void;
  popOverlay: () => void;
  closedTabs: string[];
  closeTab: (runId: string) => void;
  reopenTab: (runId: string) => void;
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
}));
