import type { Screen } from "../types";

/** Single source of truth for screen → URL path. Used by SideRail,
 *  CommandPalette, MobileTabBar, and the lazy route table in router.tsx.
 *  The console lives under /app — `/` is the public landing page. */
export const CONSOLE_HOME = "/app";

export const SCREEN_PATHS: Record<Screen, string> = {
  sessions: CONSOLE_HOME,
  knowledge: `${CONSOLE_HOME}/knowledge`,
  ideas: `${CONSOLE_HOME}/ideas`,
  proposals: `${CONSOLE_HOME}/patrol`,
  dashboard: `${CONSOLE_HOME}/costs`,
  repos: `${CONSOLE_HOME}/repos`,
  team: `${CONSOLE_HOME}/team`,
};
