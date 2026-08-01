import { useState } from "react";
import { useSession } from "../../stores/session";

/** Internal-team gate (§7a): username + PIN, lockout handled server-side. */
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
    <div className="login-wrap">
      <div className="login-stage">
        <div className="login-brandrow">
          <span className="login-glyph">⌁</span>
          <span className="login-word">zagent</span>
          <span className="led login-led" />
          <span className="login-knobs">
            <span className="knob" />
            <span className="knob" />
          </span>
        </div>
        <div className="login-tag">the rack runs the fleet</div>

        <form className="login-panel" onSubmit={submit}>
          <div className="login-panel-label mono">sign in — internal team</div>
          <label className="mono faint">username</label>
          <input
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            autoComplete="username"
            autoFocus
          />
          <label className="mono faint">pin</label>
          <input
            type="password"
            value={pin}
            onChange={(e) => setPin(e.target.value)}
            autoComplete="current-password"
          />
          {error && <div className="login-error">{error}</div>}
          <button className="btn btn-primary login-btn" disabled={busy || !username || !pin}>
            <span className="login-btn-dot" />
            {busy ? "signing in…" : "sign in"}
          </button>
        </form>

        <div className="login-hints mono">
          <span>username + PIN</span>
          <span className="login-hint-sep">·</span>
          <span>codes shown once</span>
          <span className="login-hint-sep">·</span>
          <span>© Boston Health AI</span>
        </div>
      </div>
      <style>{`
        .login-wrap { height: 100%; display: flex; align-items: center; justify-content: center; }
        .login-stage { width: 460px; max-width: calc(100vw - 32px); display: flex; flex-direction: column; }
        .login-brandrow { display: flex; align-items: center; gap: 12px; }
        .login-glyph { font-family: var(--font-display); font-size: 34px; color: var(--green-bright); line-height: 1; }
        .login-word { font-family: var(--font-display); font-size: 32px; font-weight: 600; letter-spacing: .01em; }
        .login-led { background: var(--green-bright); color: var(--green-bright); width: 9px; height: 9px; }
        .login-knobs { margin-left: auto; display: flex; gap: 10px; }
        .knob {
          width: 26px; height: 26px; border-radius: 50%;
          background: radial-gradient(circle at 35% 30%, var(--bg-module), var(--jack) 75%);
          border: 1px solid var(--hairline);
          box-shadow: inset 0 2px 4px rgba(0,0,0,.55), 0 1px 0 rgba(255,255,255,.05);
          position: relative;
        }
        .knob::after {
          content: ""; position: absolute; left: 50%; top: 3px; width: 2px; height: 8px;
          background: var(--ink-faint); border-radius: 1px; transform: translateX(-50%) rotate(24deg);
          transform-origin: bottom center;
        }
        .login-tag { color: var(--ink-secondary); font-size: 14.5px; margin: 8px 0 26px 2px; }
        .login-panel {
          background: var(--bg-panel); border: 1px solid var(--hairline);
          border-radius: 12px; padding: 34px 36px 30px;
          display: flex; flex-direction: column; gap: 12px;
          box-shadow: 0 18px 50px rgba(0,0,0,.35);
        }
        .login-panel-label {
          font-size: 11px; text-transform: uppercase; letter-spacing: .12em;
          color: var(--ink-faint); margin-bottom: 10px;
        }
        .login-panel label { font-size: 11px; text-transform: uppercase; letter-spacing: .09em; }
        .login-panel input {
          background: var(--jack); border: 1px solid var(--hairline); border-radius: var(--radius);
          color: var(--ink-primary); padding: 14px 16px; font-family: var(--font-mono); font-size: 16px;
          box-shadow: inset 0 3px 8px rgba(0,0,0,.5);
        }
        .login-error { color: var(--danger); font-size: 13.5px; }
        .login-btn {
          justify-content: center; margin-top: 10px; padding: 14px;
          font-size: 15.5px; display: flex; align-items: center; gap: 9px;
        }
        .login-btn-dot {
          width: 8px; height: 8px; border-radius: 50%;
          background: currentColor; box-shadow: 0 0 6px 1px currentColor;
        }
        .login-hints {
          display: flex; justify-content: center; gap: 10px; margin-top: 22px;
          font-size: 11.5px; color: var(--ink-faint); letter-spacing: .04em;
        }
        .login-hint-sep { color: var(--hairline); }
      `}</style>
    </div>
  );
}
