import type { LucideIcon } from "lucide-react";
import { NavLink } from "react-router-dom";
import {
  BookOpenIcon,
  ChartColumnIcon,
  CircleDollarSignIcon,
  FolderGit2Icon,
  InboxIcon,
  LightbulbIcon,
  LogOutIcon,
  RadarIcon,
  SettingsIcon,
  UsersIcon,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { useSession } from "../stores/session";
import { useRuns } from "../stores/run";
import { useUi } from "../stores/ui";
import { api } from "../lib/api";
import { toast } from "@/components/ui/sonner";
import { SettingsPanel } from "./SettingsPanel";
import { SCREEN_PATHS } from "../lib/routes";
import type { Screen } from "../types";

interface NavItem {
  screen: Screen;
  label: string;
  icon: LucideIcon;
  adminOnly?: boolean;
}

interface NavGroup {
  label: string;
  items: NavItem[];
}

const GROUPS: NavGroup[] = [
  {
    label: "operate",
    items: [
      { screen: "sessions", label: "sessions", icon: InboxIcon },
    ],
  },
  {
    label: "intelligence",
    items: [
      { screen: "knowledge", label: "knowledge", icon: BookOpenIcon },
      { screen: "ideas", label: "ideas", icon: LightbulbIcon },
      { screen: "proposals", label: "patrol", icon: RadarIcon },
    ],
  },
  {
    label: "fleet",
    items: [
      { screen: "dashboard", label: "costs", icon: CircleDollarSignIcon },
      { screen: "repos", label: "repos", icon: FolderGit2Icon },
      { screen: "team", label: "team", icon: UsersIcon, adminOnly: true },
    ],
  },
];

function RailButton({ item }: { item: NavItem }) {
  const to = SCREEN_PATHS[item.screen];
  return (
    <Tooltip>
      <TooltipTrigger
        render={
          <NavLink
            to={to}
            end={item.screen === "sessions"}
            aria-current="page"
            className={({ isActive }) =>
              cn(
                "flex w-full items-center gap-s3 rounded-md px-s3 py-[7px] font-mono text-[12px] font-semibold transition-colors duration-fast",
                "max-[1100px]:justify-center max-[1100px]:px-0",
                isActive
                  ? "bg-bg-module text-green-bright"
                  : "text-ink-secondary hover:bg-bg-module/60 hover:text-ink-primary",
              )
            }
          >
            <item.icon className="size-4 shrink-0" aria-hidden="true" />
            <span className="max-[1100px]:hidden">{item.label}</span>
          </NavLink>
        }
      />
      <TooltipContent side="right">{item.label}</TooltipContent>
    </Tooltip>
  );
}

/** Left rail (DESIGN.md): grouped nav + status footer — chrome as
 *  instrumentation. 224px, collapses to a 56px icon rail under 1100px.
 *  NavLink gives aria-current="page" and active styling for free. */
export function SideRail() {
  const { me, logout } = useSession();
  const socketConnected = useRuns((s) => s.socketConnected);
  const setSettingsOpen = useUi((s) => s.setSettingsOpen);
  const isAdmin = me?.role === "admin";

  // Usage: open the LiteLLM proxy UI (usage/spend dashboards) on the VM in a
  // new tab. The URL comes from the backend — the browser can't resolve the
  // compose-internal gateway address.
  const openUsage = async () => {
    try {
      const { url } = await api.get<{ url: string }>("/team/gateway-ui");
      window.open(url, "_blank", "noopener,noreferrer");
    } catch (err) {
      toast.error("couldn't resolve the gateway UI URL", {
        description: err instanceof Error ? err.message : undefined,
      });
    }
  };

  return (
    <nav
      aria-label="primary"
      className={cn(
        "z-rail flex h-full w-rail flex-none flex-col border-r border-hairline bg-bg-panel",
        "max-[1100px]:w-rail-compact max-[700px]:hidden",
      )}
    >
      <div className="flex h-[52px] items-center gap-s2 border-b border-hairline px-s4 max-[1100px]:justify-center max-[1100px]:px-0">
        <span className="led" aria-hidden="true" />
        <span className="font-mono text-[13.5px] font-semibold tracking-[0.03em] max-[1100px]:hidden">collegium</span>
      </div>

      <div className="flex-1 overflow-y-auto px-s2 py-s4 max-[1100px]:px-s1">
        {GROUPS.map((g) => {
          const items = g.items.filter((i) => !i.adminOnly || isAdmin);
          if (items.length === 0) return null;
          return (
            <div key={g.label} className="mb-s5">
              <div className="text-micro mb-s2 px-s3 text-ink-faint max-[1100px]:hidden">{g.label}</div>
              <div className="flex flex-col gap-[2px]">
                {items.map((i) => (
                  <RailButton key={i.screen} item={i} />
                ))}
              </div>
            </div>
          );
        })}
      </div>

      <div className="border-t border-hairline p-s3 max-[1100px]:p-s2">
        <div className="flex items-center gap-s2 px-s1 pb-s2 font-mono text-[10.5px] max-[1100px]:justify-center max-[1100px]:px-0">
          <span className={socketConnected ? "led" : "led led--off"} aria-hidden="true" />
          <span className={socketConnected ? "text-green-bright" : "text-ink-faint max-[1100px]:hidden"}>
            {socketConnected ? "live" : "idle"}
          </span>
        </div>
        <div className="flex items-center gap-s2 max-[1100px]:flex-col">
          <span className="min-w-0 flex-1 truncate font-mono text-[11px] text-ink-secondary max-[1100px]:hidden">
            {me?.display_name}
          </span>
          {isAdmin && (
            <button
              type="button"
              onClick={() => void openUsage()}
              title="usage — LLM proxy dashboards"
              aria-label="usage"
              className="rounded-md p-1.5 text-ink-faint transition-colors duration-fast hover:bg-bg-module hover:text-ink-primary"
            >
              <ChartColumnIcon className="size-4" aria-hidden="true" />
            </button>
          )}
          <button
            type="button"
            onClick={() => setSettingsOpen(true)}
            title="settings"
            aria-label="settings"
            className="rounded-md p-1.5 text-ink-faint transition-colors duration-fast hover:bg-bg-module hover:text-ink-primary"
          >
            <SettingsIcon className="size-4" aria-hidden="true" />
          </button>
          <button
            type="button"
            onClick={() => void logout()}
            title="sign out"
            aria-label="sign out"
            className="rounded-md p-1.5 text-ink-faint transition-colors duration-fast hover:bg-bg-module hover:text-ink-primary"
          >
            <LogOutIcon className="size-4" aria-hidden="true" />
          </button>
        </div>
      </div>
      <SettingsPanel />
    </nav>
  );
}
