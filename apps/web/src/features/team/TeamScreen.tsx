import { useCallback, useEffect, useState } from "react";
import { api } from "../../lib/api";

interface TeamUser {
  id: number;
  username: string;
  display_name: string;
  role: string;
  status: string;
  ado_email: string | null;
  ado_bound: boolean;
}

interface Stats {
  total_runs: number;
  runs_by_stage: Record<string, number>;
  runs_by_mode: Record<string, number>;
  total_cost_usd: number;
}

/** Admin team settings (plan §1b): provisioning via one-time setup codes
 *  shown ONCE; deactivate never deletes; stats are metadata-only. The route
 *  itself is admin-gated server-side — this screen renders the 403 as text. */
export function TeamScreen() {
  const [users, setUsers] = useState<TeamUser[]>([]);
  const [stats, setStats] = useState<Stats | null>(null);
  const [username, setUsername] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [adoEmail, setAdoEmail] = useState("");
  const [oneTimeCode, setOneTimeCode] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const [error, setError] = useState("");
  const [denied, setDenied] = useState(false);

  const load = useCallback(async () => {
    try {
      const [u, s] = await Promise.all([
        api.get<TeamUser[]>("/team/users"),
        api.get<Stats>("/team/stats"),
      ]);
      setUsers(u);
      setStats(s);
    } catch (e) {
      if (e instanceof Error && /403|forbidden|admin/i.test(e.message)) setDenied(true);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  async function addUser() {
    setError("");
    try {
      const data = await api.post<{ setup_code: string }>("/team/users", {
        username: username.trim(),
        display_name: displayName.trim(),
        ado_email: adoEmail.trim(),
      });
      setOneTimeCode(data.setup_code);
      setCopied(false);
      setUsername("");
      setDisplayName("");
      setAdoEmail("");
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : "add failed");
    }
  }

  async function regen(id: number) {
    const data = await api.post<{ setup_code: string }>(`/team/users/${id}/regenerate-code`, {});
    setOneTimeCode(data.setup_code);
    setCopied(false);
  }

  async function deactivate(id: number) {
    await api.post(`/team/users/${id}/deactivate`, {});
    await load();
  }

  if (denied) {
    return (
      <section className="team-screen">
        <p className="muted" data-testid="team-denied">admin only — ask an admin to open team settings</p>
      </section>
    );
  }

  return (
    <section className="team-screen">
      <header className="screen-head">
        <h1>team</h1>
        <p className="muted">admin · metadata-only stats · codes shown once</p>
      </header>

      {oneTimeCode && (
        <div className="code-banner" data-testid="one-time-code">
          <div className="code-label mono">one-time setup code — send via Slack, never shown again</div>
          <div className="code-row">
            <code className="mono">{oneTimeCode}</code>
            <button
              className="btn btn-mono"
              onClick={() => void navigator.clipboard.writeText(oneTimeCode).then(() => setCopied(true))}
            >
              {copied ? "copied ✓" : "copy"}
            </button>
            <button className="btn btn-mono btn-ghost" onClick={() => setOneTimeCode(null)}>dismiss</button>
          </div>
        </div>
      )}

      <div className="card add-form">
        <div className="code-label mono">add teammate</div>
        <div className="add-grid">
          <input value={username} onChange={(e) => setUsername(e.target.value)} placeholder="username" />
          <input value={displayName} onChange={(e) => setDisplayName(e.target.value)} placeholder="display name" />
          <input value={adoEmail} onChange={(e) => setAdoEmail(e.target.value)} placeholder="ADO email (identity binding)" />
          <button className="btn btn-primary" disabled={!username.trim()} onClick={() => void addUser()}>
            add
          </button>
        </div>
        {error && <p className="err">{error}</p>}
      </div>

      <table className="team-table">
        <thead>
          <tr className="mono">
            <th>user</th><th>role</th><th>status</th><th>ADO</th><th />
          </tr>
        </thead>
        <tbody>
          {users.map((u) => (
            <tr key={u.id} data-testid={`user-${u.username}`}>
              <td>
                <div className="u-name">{u.display_name || u.username}</div>
                <div className="faint mono">{u.username}</div>
              </td>
              <td className="mono">{u.role}</td>
              <td>
                <span className={`tag mono ${u.status === "active" ? "st-completed" : u.status === "pending" ? "st-running" : "st-failed"}`}>
                  {u.status}
                </span>
              </td>
              <td className="mono">{u.ado_bound ? "bound" : u.ado_email ?? "—"}</td>
              <td className="u-actions">
                {u.status !== "deactivated" && (
                  <>
                    <button className="btn btn-mono btn-ghost" onClick={() => void regen(u.id)}>new code</button>
                    <button className="btn btn-mono btn-danger" onClick={() => void deactivate(u.id)}>deactivate</button>
                  </>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      {stats && (
        <div className="stats-row mono" data-testid="team-stats">
          <span>runs: <b>{stats.total_runs}</b></span>
          <span>cost: <b>${stats.total_cost_usd.toFixed(2)}</b></span>
          <span>modes: <b>{Object.entries(stats.runs_by_mode).map(([k, v]) => `${k}×${v}`).join(" ") || "—"}</b></span>
        </div>
      )}
      <style>{`
        .team-screen { max-width: 780px; margin: 0 auto; padding: 22px 18px; overflow-y: auto; height: 100%; }
        .code-banner { background: color-mix(in srgb, var(--green) 12%, var(--bg-module)); border: 1px solid var(--green); border-radius: var(--radius); padding: 14px 16px; margin-bottom: 16px; }
        .code-label { font-size: 10.5px; text-transform: uppercase; letter-spacing: .08em; color: var(--green-bright); margin-bottom: 8px; }
        .code-row { display: flex; gap: 10px; align-items: center; }
        .code-row code { font-size: 16px; letter-spacing: .1em; }
        .add-form { padding: 16px; margin-bottom: 18px; }
        .add-grid { display: grid; grid-template-columns: 1fr 1fr 1fr auto; gap: 10px; }
        .add-grid input { background: var(--jack); border: 1px solid var(--hairline); border-radius: var(--radius); color: var(--ink-primary); padding: 8px 10px; font-size: 13px; }
        .team-table { width: 100%; border-collapse: collapse; font-size: 13px; margin-bottom: 22px; }
        .team-table th { text-align: left; padding: 6px 8px; border-bottom: 1px solid var(--hairline); font-size: 10.5px; text-transform: uppercase; letter-spacing: .06em; color: var(--ink-faint); }
        .team-table td { padding: 9px 8px; border-bottom: 1px solid var(--hairline); }
        .u-name { font-weight: 600; }
        .u-actions { text-align: right; display: flex; gap: 6px; justify-content: flex-end; }
        .stats-row { display: flex; gap: 26px; font-size: 12px; color: var(--ink-secondary); }
        .stats-row b { color: var(--ink-primary); }
      `}</style>
    </section>
  );
}
