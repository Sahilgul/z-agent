import { create } from "zustand";
import { api, setUnauthorizedHandler } from "../lib/api";
import type { Me } from "../types";

interface SessionState {
  me: Me | null;
  booted: boolean;
  boot: () => Promise<void>;
  login: (username: string, pin: string) => Promise<void>;
  logout: () => Promise<void>;
}

export const useSession = create<SessionState>((set) => ({
  me: null,
  booted: false,
  boot: async () => {
    try {
      const me = await api.get<Me>("/auth/me");
      set({ me, booted: true });
    } catch {
      set({ me: null, booted: true });
    }
  },
  login: async (username, pin) => {
    await api.post("/auth/login", { username, pin });
    const me = await api.get<Me>("/auth/me");
    set({ me });
  },
  logout: async () => {
    try {
      await api.post("/auth/logout");
    } finally {
      set({ me: null });
    }
  },
}));

setUnauthorizedHandler(() => useSession.setState({ me: null }));
