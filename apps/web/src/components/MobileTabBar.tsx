import { NavLink } from "react-router-dom";
import {
  BookOpenIcon,
  CircleDollarSignIcon,
  InboxIcon,
  LightbulbIcon,
  RadarIcon,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { SCREEN_PATHS } from "../lib/routes";

/** Mobile bottom tab bar (Linear/Notion mobile pattern) — replaces the
 *  dropdown MobileNav. Five primary destinations as icon+label tabs,
 *  shown only under 700px. NavLink gives aria-current="page" for free. */
const TABS = [
  { to: SCREEN_PATHS.sessions, label: "sessions", icon: InboxIcon, end: true },
  { to: SCREEN_PATHS.knowledge, label: "knowledge", icon: BookOpenIcon, end: false },
  { to: SCREEN_PATHS.ideas, label: "ideas", icon: LightbulbIcon, end: false },
  { to: SCREEN_PATHS.proposals, label: "patrol", icon: RadarIcon, end: false },
  { to: SCREEN_PATHS.dashboard, label: "costs", icon: CircleDollarSignIcon, end: false },
] as const;

export function MobileTabBar() {
  return (
    <nav
      aria-label="mobile primary"
      className="z-rail hidden max-[700px]:flex max-[700px]:h-[56px] max-[700px]:flex-none max-[700px]:border-t max-[700px]:border-hairline max-[700px]:bg-bg-panel"
    >
      {TABS.map((tab) => (
        <NavLink
          key={tab.to}
          to={tab.to}
          end={tab.end}
          className={({ isActive }) =>
            cn(
              "flex flex-1 flex-col items-center justify-center gap-[3px] font-mono text-[10px] tracking-[0.02em] transition-colors duration-fast",
              isActive ? "text-green-bright" : "text-ink-faint hover:text-ink-secondary",
            )
          }
        >
          <tab.icon className="size-5" aria-hidden="true" />
          <span>{tab.label}</span>
        </NavLink>
      ))}
    </nav>
  );
}
