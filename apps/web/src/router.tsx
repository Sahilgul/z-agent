import { lazy, Suspense, useEffect, type ReactNode } from "react";
import { createBrowserRouter, Navigate, Outlet, useLocation } from "react-router-dom";
import { Skeleton } from "@/components/ui/skeleton";
import { ErrorBoundary } from "./components/ErrorBoundary";
import { CommandPalette } from "./components/CommandPalette";
import { MobileTabBar } from "./components/MobileTabBar";
import { SideRail } from "./components/SideRail";
import { CONSOLE_HOME } from "./lib/routes";
import { useSession } from "./stores/session";

// Code-split per route — the data router wraps navigation in startTransition,
// so lazy routes can suspend without the "suspended while responding to
// synchronous input" error the legacy BrowserRouter hit.
const LoginScreen = lazy(() =>
  import("./features/login/LoginScreen").then((m) => ({ default: m.LoginScreen })),
);
const LandingScreen = lazy(() =>
  import("./features/landing/LandingScreen").then((m) => ({ default: m.LandingScreen })),
);
const SessionsScreen = lazy(() =>
  import("./features/sessions/SessionsScreen").then((m) => ({ default: m.SessionsScreen })),
);
const KnowledgeScreen = lazy(() =>
  import("./features/knowledge/KnowledgeScreen").then((m) => ({ default: m.KnowledgeScreen })),
);
const IdeasScreen = lazy(() =>
  import("./features/ideas/IdeasScreen").then((m) => ({ default: m.IdeasScreen })),
);
const ProposalsScreen = lazy(() =>
  import("./features/proposals/ProposalsScreen").then((m) => ({ default: m.ProposalsScreen })),
);
const DashboardScreen = lazy(() =>
  import("./features/dashboard/DashboardScreen").then((m) => ({ default: m.DashboardScreen })),
);
const ReposScreen = lazy(() =>
  import("./features/repos/ReposScreen").then((m) => ({ default: m.ReposScreen })),
);
const TeamScreen = lazy(() =>
  import("./features/team/TeamScreen").then((m) => ({ default: m.TeamScreen })),
);

function WarmingScreen() {
  return (
    <div className="flex h-full items-center justify-center font-mono text-[13px] text-ink-faint">
      warming the bay…
    </div>
  );
}

function ScreenSkeleton() {
  return (
    <div className="mx-auto h-full max-w-canvas px-s8 py-s6">
      <div className="mb-s6 flex flex-col gap-s2">
        <Skeleton className="h-7 w-64 rounded-sm" />
        <Skeleton className="h-3 w-80 rounded-sm" />
      </div>
      <div className="flex flex-col gap-s3">
        {[0, 1, 2, 3].map((i) => (
          <Skeleton key={i} className="h-16 w-full rounded-lg" />
        ))}
      </div>
    </div>
  );
}

function SkipLink() {
  return (
    <a
      href="#main"
      className="sr-only z-toast focus:not-sr-only focus:fixed focus:left-s4 focus:top-s4 focus:rounded-md focus:border focus:border-hairline focus:bg-bg-panel focus:px-s4 focus:py-s2 focus:font-mono focus:text-[12.5px] focus:text-ink-primary focus:shadow-pop"
    >
      skip to content
    </a>
  );
}

function AdminRoute({ children }: { children: ReactNode }) {
  const me = useSession((s) => s.me);
  if (me?.role !== "admin") return <Navigate to={CONSOLE_HOME} replace />;
  return <>{children}</>;
}

/** Authed shell: rail + outlet + mobile tabs + ⌘K. Rendered by RootLayout
 *  once a session exists. The Suspense boundary here catches lazy route
 *  suspensions inside the transition the data router already started. */
function AuthedShell() {
  // C-18: key the error boundary to the current pathname so a screen error
  // resets when the user navigates away — otherwise one crash traps the
  // whole session until a full reload.
  const location = useLocation();
  return (
    <>
      <SkipLink />
      <div className="flex h-full">
        <SideRail />
        <div className="flex min-w-0 flex-1 flex-col">
          <main id="main" className="min-h-0 flex-1 overflow-hidden max-[700px]:pb-[56px]">
            <ErrorBoundary resetKey={location.pathname}>
              <Suspense fallback={<ScreenSkeleton />}>
                <Outlet />
              </Suspense>
            </ErrorBoundary>
          </main>
          <MobileTabBar />
        </div>
      </div>
      <CommandPalette />
    </>
  );
}

/** Root layout: boots the session, then gates on auth. Renders the shell
 *  when authed, redirects to /login when not. */
function RootLayout() {
  const { me, booted, boot } = useSession();
  useEffect(() => {
    void boot();
  }, [boot]);
  if (!booted) return <WarmingScreen />;
  if (!me) return <Navigate to="/login" replace />;
  return <AuthedShell />;
}

/** Login route: bounces authed users into the console. Also boots the session
 *  (RootLayout doesn't mount at /login, so boot must run here too —
 *  otherwise a refresh on /login hangs on the warming screen forever). */
function LoginRoute() {
  const { me, booted, boot } = useSession();
  useEffect(() => {
    void boot();
  }, [boot]);
  if (!booted) return <WarmingScreen />;
  if (me) return <Navigate to={CONSOLE_HOME} replace />;
  return (
    <ErrorBoundary>
      <Suspense fallback={<WarmingScreen />}>
        <LoginScreen />
      </Suspense>
    </ErrorBoundary>
  );
}

/** Landing route: public brochure at `/`. Boots the session in the background
 *  so the primary CTA can flip between "sign in" and "open console" — but
 *  never gates or redirects; the page must paint instantly and stay
 *  reachable while signed in. */
function LandingRoute() {
  const boot = useSession((s) => s.boot);
  useEffect(() => {
    void boot();
  }, [boot]);
  return (
    <ErrorBoundary>
      <Suspense fallback={<WarmingScreen />}>
        <LandingScreen />
      </Suspense>
    </ErrorBoundary>
  );
}

export const router = createBrowserRouter([
  { path: "/", element: <LandingRoute /> },
  {
    path: CONSOLE_HOME,
    element: <RootLayout />,
    children: [
      { index: true, element: <SessionsScreen /> },
      { path: "knowledge", element: <KnowledgeScreen /> },
      { path: "ideas", element: <IdeasScreen /> },
      { path: "patrol", element: <ProposalsScreen /> },
      { path: "costs", element: <DashboardScreen /> },
      { path: "repos", element: <ReposScreen /> },
      {
        path: "team",
        element: (
          <AdminRoute>
            <TeamScreen />
          </AdminRoute>
        ),
      },
    ],
  },
  { path: "/login", element: <LoginRoute /> },
  { path: "*", element: <Navigate to="/" replace /> },
]);
