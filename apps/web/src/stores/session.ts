import { create } from "zustand";
import { toast } from "@/components/ui/sonner";
import { api, setUnauthorizedHandler } from "../lib/api";
import type { Me } from "../types";

interface SessionState {
  me: Me | null;
  booted: boolean;
  boot: () => Promise<void>;
  login: (username: string, pin: string, remember: boolean) => Promise<void>;
  logout: () => Promise<void>;
}

export const useSession = create<SessionState>((set, get) => ({
  me: null,
  booted: false,
  boot: async () => {
    // Idempotent: only run once per page load. Without this guard, navigating
    // to / after a dev-bypass login re-runs boot, which hits the down backend
    // and wipes `me` back to null — an infinite login→logout loop.
    if (get().booted) return;
    try {
      const me = await api.get<Me>("/auth/me");
      set({ me, booted: true });
    } catch {
      set({ me: null, booted: true });
    }
  },
  login: async (username, pin, remember) => {
    try {
      // M-82: wire the "remember me" checkbox through to the backend so it
      // actually changes cookie persistence (session vs 14-day). Was dead.
      await api.post("/auth/login", { username, pin, remember });
      const me = await api.get<Me>("/auth/me");
      set({ me });
      toast.success(`welcome back, ${me.display_name || me.username}`);
    } catch (err) {
      // Dev-server bypass: backend down → any credentials become a fake admin.
      if (!import.meta.env.DEV) {
        toast.error("sign in failed", {
          description: err instanceof Error ? err.message : undefined,
        });
        throw err;
      }
      set({
        me: { id: 0, username, display_name: username, role: "admin", must_change_pin: false },
      });
    }
  },
  logout: async () => {
    try {
      await api.post("/auth/logout");
    } finally {
      set({ me: null });
      toast("signed out");
    }
  },
}));

setUnauthorizedHandler(() => useSession.setState({ me: null }));
