import { useEffect, useState } from "react";
import { MenuIcon } from "lucide-react";
import { TooltipProvider } from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";
import { SideRail } from "./components/SideRail";
import { useSession } from "./stores/session";
import { useUi } from "./stores/ui";
import { LoginScreen } from "./features/login/LoginScreen";
import { InboxScreen } from "./features/inbox/InboxScreen";
import { MonitorScreen } from "./features/monitor/MonitorScreen";
import { ApprovalsScreen } from "./features/approvals/ApprovalsScreen";
import { KnowledgeScreen } from "./features/knowledge/KnowledgeScreen";
import { IdeasScreen } from "./features/ideas/IdeasScreen";
import { ProposalsScreen } from "./features/proposals/ProposalsScreen";
import { DashboardScreen } from "./features/dashboard/DashboardScreen";
import { ReposScreen } from "./features/repos/ReposScreen";
import { TeamScreen } from "./features/team/TeamScreen";

const MOBILE_NAV = [
  { screen: "inbox", label: "inbox" },
  { screen: "monitor", label: "monitor" },
  { screen: "approvals", label: "approvals" },
  { screen: "knowledge", label: "knowledge" },
  { screen: "ideas", label: "ideas" },
  { screen: "proposals", label: "patrol" },
  { screen: "dashboard", label: "costs" },
  { screen: "repos", label: "repos" },
  { screen: "team", label: "team", adminOnly: true },
] as const;

/** Top bar + overflow menu under 700px (Capacitor path); the rail owns
 *  navigation at every wider viewport. */
function MobileNav() {
  const { screen, setScreen } = useUi();
  const me = useSession((s) => s.me);
  const [open, setOpen] = useState(false);
  const items = MOBILE_NAV.filter((i) => !("adminOnly" in i && i.adminOnly) || me?.role === "admin");

  return (
    <div className="hidden max-[700px]:flex max-[700px]:flex-col">
      <div className="flex h-[52px] flex-none items-center justify-between border-b border-hairline bg-bg-panel px-s4">
        <span className="flex items-center gap-s2 font-mono text-[13.5px] font-semibold tracking-[0.03em]">
          <span className="led" aria-hidden="true" />
          zagent
        </span>
        <button
          type="button"
          aria-expanded={open}
          aria-label="open navigation"
          title="menu"
          onClick={() => setOpen((v) => !v)}
          className="rounded-md p-1.5 text-ink-secondary transition-colors duration-fast hover:bg-bg-module hover:text-ink-primary"
        >
          <MenuIcon className="size-5" aria-hidden="true" />
        </button>
      </div>
      {open && (
        <div className="border-b border-hairline bg-bg-panel p-s2 shadow-pop">
          {items.map((i) => (
            <button
              key={i.screen}
              type="button"
              onClick={() => {
                setScreen(i.screen);
                setOpen(false);
              }}
              className={cn(
                "block w-full rounded-md px-s3 py-2 text-left font-mono text-[12.5px] font-semibold",
                screen === i.screen ? "bg-bg-module text-green-bright" : "text-ink-secondary",
              )}
            >
              {i.label}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

export default function App() {
  const { me, booted, boot } = useSession();
  const screen = useUi((s) => s.screen);

  useEffect(() => {
    void boot();
  }, [boot]);

  if (!booted) {
    return (
      <div className="flex h-full items-center justify-center font-mono text-[13px] text-ink-faint">
        warming the bay…
      </div>
    );
  }
  if (!me) return <LoginScreen />;

  return (
    <TooltipProvider delay={350}>
      <div className="flex h-full">
        <SideRail />
        <div className="flex min-w-0 flex-1 flex-col">
          <MobileNav />
          <main className="min-h-0 flex-1">
            <div key={screen} className="animate-enter h-full">
              {screen === "inbox" && <InboxScreen />}
              {screen === "monitor" && <MonitorScreen />}
              {screen === "approvals" && <ApprovalsScreen />}
              {screen === "knowledge" && <KnowledgeScreen />}
              {screen === "ideas" && <IdeasScreen />}
              {screen === "proposals" && <ProposalsScreen />}
              {screen === "dashboard" && <DashboardScreen />}
              {screen === "repos" && <ReposScreen />}
              {screen === "team" && <TeamScreen />}
            </div>
          </main>
        </div>
      </div>
    </TooltipProvider>
  );
}
