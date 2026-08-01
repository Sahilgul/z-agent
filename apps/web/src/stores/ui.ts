import { create } from "zustand";

/** Overlay stack (§1 monitor): lane trace / plan / PR overlays open at 80%
 *  over the live monitor — the monitor never unmounts, so a closing overlay
 *  reveals a stream that kept flowing underneath. */
export type Overlay =
  | { kind: "lane"; laneId: string }
  | { kind: "plan" }
  | { kind: "pr" };

export type Screen = "inbox" | "monitor" | "approvals" | "knowledge" | "ideas" | "proposals" | "dashboard" | "repos" | "team";

interface UiState {
  screen: Screen;
  overlays: Overlay[];
  setScreen: (s: Screen) => void;
  pushOverlay: (o: Overlay) => void;
  popOverlay: () => void;
}

export const useUi = create<UiState>((set) => ({
  screen: "inbox",
  overlays: [],
  setScreen: (screen) => set({ screen }),
  pushOverlay: (o) => set((s) => ({ overlays: [...s.overlays, o] })),
  popOverlay: () => set((s) => ({ overlays: s.overlays.slice(0, -1) })),
}));
