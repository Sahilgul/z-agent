import { useEffect } from "react";
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

export default function App() {
  const { me, booted, boot, logout } = useSession();
  const { screen, setScreen } = useUi();

  useEffect(() => {
    void boot();
  }, [boot]);

  if (!booted) {
    return <div className="boot faint mono">warming the bay…</div>;
  }
  if (!me) return <LoginScreen />;

  return (
    <div className="shell">
      <nav className="topbar">
        <span className="wordmark mono">
          <span className="led" />
          zagent
        </span>
        <div className="topnav">
          <button className={`tn mono ${screen === "inbox" ? "on" : ""}`} onClick={() => setScreen("inbox")}>
            inbox
          </button>
          <button className={`tn mono ${screen === "monitor" ? "on" : ""}`} onClick={() => setScreen("monitor")}>
            monitor
          </button>
          <button className={`tn mono ${screen === "approvals" ? "on" : ""}`} onClick={() => setScreen("approvals")}>
            approvals
          </button>
          <button className={`tn mono ${screen === "knowledge" ? "on" : ""}`} onClick={() => setScreen("knowledge")}>
            knowledge
          </button>
          <button className={`tn mono ${screen === "ideas" ? "on" : ""}`} onClick={() => setScreen("ideas")}>
            ideas
          </button>
          <button className={`tn mono ${screen === "proposals" ? "on" : ""}`} onClick={() => setScreen("proposals")}>
            patrol
          </button>
          <button className={`tn mono ${screen === "dashboard" ? "on" : ""}`} onClick={() => setScreen("dashboard")}>
            costs
          </button>
          <button className={`tn mono ${screen === "repos" ? "on" : ""}`} onClick={() => setScreen("repos")}>
            repos
          </button>
          {me.role === "admin" && (
            <button className={`tn mono ${screen === "team" ? "on" : ""}`} onClick={() => setScreen("team")}>
              team
            </button>
          )}
        </div>
        <div className="topme">
          <span className="mono faint">{me.display_name}</span>
          <button className="btn btn-mono btn-ghost" onClick={() => void logout()}>
            sign out
          </button>
        </div>
      </nav>
      <main className="shell-main">
        {screen === "inbox" && <InboxScreen />}
        {screen === "monitor" && <MonitorScreen />}
        {screen === "approvals" && <ApprovalsScreen />}
        {screen === "knowledge" && <KnowledgeScreen />}
        {screen === "ideas" && <IdeasScreen />}
        {screen === "proposals" && <ProposalsScreen />}
        {screen === "dashboard" && <DashboardScreen />}
        {screen === "repos" && <ReposScreen />}
        {screen === "team" && <TeamScreen />}
      </main>
      <style>{`
        .boot { height: 100%; display: flex; align-items: center; justify-content: center; font-size: 13px; }
        .shell { height: 100%; display: flex; flex-direction: column; }
        .topbar {
          display: flex; align-items: center; justify-content: space-between;
          height: 52px; padding: 0 18px; border-bottom: 1px solid var(--hairline);
          background: color-mix(in srgb, var(--bg-base) 92%, transparent);
        }
        .wordmark { display: flex; align-items: center; gap: 9px; font-weight: 600; font-size: 13.5px; letter-spacing: .03em; }
        .topnav { display: flex; gap: 4px; }
        .tn { background: none; border: none; color: var(--ink-secondary); font-size: 12px; padding: 7px 13px; cursor: pointer; border-radius: var(--radius); }
        .tn:hover { color: var(--ink-primary); }
        .tn.on { color: var(--green-bright); background: var(--bg-module); }
        .topme { display: flex; align-items: center; gap: 12px; font-size: 12px; }
        .shell-main { flex: 1; min-height: 0; }
      `}</style>
    </div>
  );
}
