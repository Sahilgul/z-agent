import type { Screen } from "../types";

/** Single source of truth for screen → URL path. Used by SideRail,
 *  CommandPalette, MobileTabBar, and the lazy route table in App.tsx. */
export const SCREEN_PATHS: Record<Screen, string> = {
  sessions: "/",
  knowledge: "/knowledge",
  ideas: "/ideas",
  proposals: "/patrol",
  dashboard: "/costs",
  repos: "/repos",
  team: "/team",
};
