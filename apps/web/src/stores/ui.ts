import { create } from "zustand";

/** Overlay stack: lane trace / plan / PR overlays open at 80% over the live
 *  session — the session never unmounts, so a closing overlay reveals a
 *  stream that kept flowing underneath. Screen routing lives in react-router;
 *  this store keeps only overlay modal state. */
export type Overlay =
  | { kind: "lane"; laneId: string }
  | { kind: "plan" }
  | { kind: "pr" };

interface UiState {
  overlays: Overlay[];
  pushOverlay: (o: Overlay) => void;
  popOverlay: () => void;
}

export const useUi = create<UiState>((set) => ({
  overlays: [],
  pushOverlay: (o) => set((s) => ({ overlays: [...s.overlays, o] })),
  popOverlay: () => set((s) => ({ overlays: s.overlays.slice(0, -1) })),
}));
