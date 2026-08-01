import { useState } from "react";
import { Button } from "@/components/ui/button";
import { useSession } from "../../stores/session";

/** Internal-team gate: username + PIN, lockout handled server-side. */
export function LoginScreen() {
  const login = useSession((s) => s.login);
  const [username, setUsername] = useState("");
  const [pin, setPin] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      await login(username.trim(), pin);
    } catch (err) {
      setError(err instanceof Error ? err.message : "login failed");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="flex h-full items-center justify-center">
      <div className="flex w-[420px] max-w-[calc(100vw-32px)] flex-col">
        <div className="flex items-center gap-s3">
          <span className="font-display text-[30px] font-semibold leading-none text-ok-bright">⌁</span>
          <span className="font-display text-[28px] font-semibold tracking-[0.01em] text-ink-primary">zagent</span>
          <span className="size-[9px] rounded-full bg-ok-bright shadow-led" aria-hidden="true" />
        </div>
        <div className="mb-s6 ml-[2px] mt-s2 text-[14px] text-ink-secondary">the rack runs the fleet</div>

        <form
          onSubmit={submit}
          className="flex flex-col gap-s3 rounded-lg border border-hairline bg-bg-panel px-s8 pb-s7 pt-s8 shadow-overlay"
        >
          <div className="mb-s2 font-mono text-[10.5px] uppercase tracking-[0.12em] text-ink-faint">
            sign in — internal team
          </div>
          <label htmlFor="login-username" className="font-mono text-[10.5px] uppercase tracking-[0.09em] text-ink-faint">
            username
          </label>
          <input
            id="login-username"
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            autoComplete="username"
            autoFocus
            className="h-11 rounded-md border border-hairline bg-jack px-s4 font-mono text-[15px] text-ink-primary shadow-[inset_0_2px_6px_rgba(0,0,0,0.5)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue focus-visible:ring-offset-1 focus-visible:ring-offset-jack"
          />
          <label htmlFor="login-pin" className="font-mono text-[10.5px] uppercase tracking-[0.09em] text-ink-faint">
            pin
          </label>
          <input
            id="login-pin"
            type="password"
            value={pin}
            onChange={(e) => setPin(e.target.value)}
            autoComplete="current-password"
            className="h-11 rounded-md border border-hairline bg-jack px-s4 font-mono text-[15px] text-ink-primary shadow-[inset_0_2px_6px_rgba(0,0,0,0.5)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue focus-visible:ring-offset-1 focus-visible:ring-offset-jack"
          />
          {error && <div className="text-[13px] text-danger-bright">{error}</div>}
          <Button type="submit" className="mt-s2 h-11 justify-center font-mono text-[14px]" disabled={busy || !username || !pin}>
            <span className="size-2 rounded-full bg-current shadow-led" aria-hidden="true" />
            {busy ? "signing in…" : "sign in"}
          </Button>
        </form>

        <div className="mt-s5 flex justify-center gap-s2 font-mono text-[11px] tracking-[0.04em] text-ink-faint">
          <span>username + PIN</span>
          <span className="text-hairline">·</span>
          <span>codes shown once</span>
          <span className="text-hairline">·</span>
          <span>© Boston Health AI</span>
        </div>
      </div>
    </div>
  );
}
