import { useEffect, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "@/components/ui/sonner";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { PageHead } from "@/components/ui/page-head";
import { api } from "../../lib/api";
import { qk } from "../../lib/queryKeys";
import { useSession } from "../../stores/session";

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

const STATUS_TONE: Record<string, string> = {
  active: "border-transparent bg-ok-soft text-ok-bright",
  pending: "border-transparent bg-blue-soft text-blue-bright",
  deactivated: "border-transparent bg-danger-soft text-danger-bright",
};

/** Admin team settings: provisioning via one-time setup codes shown ONCE;
 *  deactivate never deletes; stats are metadata-only. The route itself is
 *  admin-gated server-side — this screen renders the 403 as text. */
export function TeamScreen() {
  const qc = useQueryClient();
  const [username, setUsername] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [adoEmail, setAdoEmail] = useState("");
  const [oneTimeCode, setOneTimeCode] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const [error, setError] = useState("");
  const [denied, setDenied] = useState(false);

  const { data, error: queryError } = useQuery({
    queryKey: qk.team,
    queryFn: async () => {
      const [users, stats] = await Promise.all([
        api.get<TeamUser[]>("/team/users"),
        api.get<Stats>("/team/stats"),
      ]);
      return { users, stats };
    },
    retry: false,
  });

  // M-86: detect the admin-gate 403 from the query's OWN error. The old
  // code did a redundant second fetch in a useEffect (double-hit
  // /team/users on mount) and ignored the useQuery error entirely — so a
  // 403 on /team/stats alone would never set `denied`. Now a 403 on either
  // admin-gated endpoint renders the 403 as text, not a crash.
  useEffect(() => {
    if (queryError instanceof Error && /403|forbidden|admin/i.test(queryError.message)) {
      setDenied(true);
    } else {
      setDenied(false);
    }
  }, [queryError]);

  const users = data?.users ?? [];
  const stats = data?.stats ?? null;

  async function addUser() {
    setError("");
    try {
      const res = await api.post<{ setup_code: string }>("/team/users", {
        username: username.trim(),
        display_name: displayName.trim(),
        ado_email: adoEmail.trim(),
      });
      setOneTimeCode(res.setup_code);
      setCopied(false);
      setUsername("");
      setDisplayName("");
      setAdoEmail("");
      await qc.invalidateQueries({ queryKey: qk.team });
    } catch (e) {
      setError(e instanceof Error ? e.message : "add failed");
    }
  }

  async function regen(id: number) {
    // W-H16: was an uncaught promise — a 4xx/5xx died silently and the admin
    // stared at an unchanged table.
    try {
      const res = await api.post<{ setup_code: string }>(`/team/users/${id}/regenerate-code`, {});
      setOneTimeCode(res.setup_code);
      setCopied(false);
    } catch (e) {
      toast.error("code regeneration failed", { description: e instanceof Error ? e.message : undefined });
    }
  }

  const me = useSession((s) => s.me);
  const [confirmingId, setConfirmingId] = useState<number | null>(null);
  const [busyId, setBusyId] = useState<number | null>(null);

  async function deactivate(id: number) {
    // W-H16: confirm + busy-guard + toast on failure. Deactivation is
    // reversible by an admin but revokes access instantly — no bare click.
    setBusyId(id);
    try {
      await api.post(`/team/users/${id}/deactivate`, {});
      toast.success("teammate deactivated", { description: "their setup code and sessions are revoked" });
      setConfirmingId(null);
      await qc.invalidateQueries({ queryKey: qk.team });
    } catch (e) {
      toast.error("deactivate failed", { description: e instanceof Error ? e.message : undefined });
    } finally {
      setBusyId(null);
    }
  }

  if (denied) {
    return (
      <div className="mx-auto h-full max-w-[780px] overflow-y-auto px-s8 py-s6">
        <p className="text-[13px] text-ink-secondary" data-testid="team-denied">
          admin only — ask an admin to open team settings
        </p>
      </div>
    );
  }

  return (
    <div className="mx-auto h-full max-w-[780px] overflow-y-auto px-s8 py-s6">
      <PageHead title="team" sub="admin · metadata-only stats · codes shown once" />

      {oneTimeCode && (
        <div
          data-testid="one-time-code"
          className="mb-s4 rounded-lg border border-ok bg-ok-soft p-s4 animate-enter"
        >
          <div className="mb-s2 font-mono text-[10px] uppercase tracking-[0.08em] text-ok-bright">
            one-time setup code — send via Slack, never shown again
          </div>
          <div className="flex items-center gap-s3">
            <code className="font-mono text-[15px] tracking-[0.1em] text-ink-primary">{oneTimeCode}</code>
            <Button
              variant="secondary"
              size="sm"
              className="font-mono"
              onClick={() => void navigator.clipboard.writeText(oneTimeCode).then(() => setCopied(true))}
            >
              {copied ? "copied ✓" : "copy"}
            </Button>
            <Button variant="ghost" size="sm" className="font-mono" onClick={() => setOneTimeCode(null)}>
              dismiss
            </Button>
          </div>
        </div>
      )}

      <div className="mb-s5 rounded-lg border border-hairline bg-bg-panel p-s4 shadow-card">
        <div className="mb-s3 font-mono text-[10px] uppercase tracking-[0.08em] text-ink-faint">add teammate</div>
        <div className="grid grid-cols-1 gap-s2 sm:grid-cols-[1fr_1fr_1.4fr_auto]">
          <Input value={username} onChange={(e) => setUsername(e.target.value)} placeholder="username" aria-label="username" />
          <Input value={displayName} onChange={(e) => setDisplayName(e.target.value)} placeholder="display name" aria-label="display name" />
          <Input value={adoEmail} onChange={(e) => setAdoEmail(e.target.value)} placeholder="ADO email (identity binding)" aria-label="ADO email" />
          <Button size="sm" className="h-8 font-mono" disabled={!username.trim()} onClick={() => void addUser()}>
            add
          </Button>
        </div>
        {error && <p className="mt-s2 text-[12.5px] text-danger-bright">{error}</p>}
      </div>

      <table className="mb-s6 w-full border-collapse text-[13px]">
        <thead>
          <tr className="font-mono text-[10px] uppercase tracking-[0.06em] text-ink-faint">
            <th className="border-b border-hairline py-s2 pl-0 pr-s2 text-left font-normal">user</th>
            <th className="border-b border-hairline px-s2 py-s2 text-left font-normal">role</th>
            <th className="border-b border-hairline px-s2 py-s2 text-left font-normal">status</th>
            <th className="border-b border-hairline px-s2 py-s2 text-left font-normal">ADO</th>
            <th className="border-b border-hairline px-s2 py-s2" />
          </tr>
        </thead>
        <tbody>
          {users.map((u) => (
            <tr key={u.id} data-testid={`user-${u.username}`}>
              <td className="border-b border-hairline py-s2 pl-0 pr-s2">
                <div className="font-semibold text-ink-primary">{u.display_name || u.username}</div>
                <div className="font-mono text-[10.5px] text-ink-faint">{u.username}</div>
              </td>
              <td className="border-b border-hairline px-s2 py-s2 font-mono text-[11.5px] text-ink-secondary">{u.role}</td>
              <td className="border-b border-hairline px-s2 py-s2">
                <Badge
                  variant="outline"
                  className={`rounded-full px-2.5 py-0.5 font-mono text-[10px] font-normal ${STATUS_TONE[u.status] ?? "border-hairline text-ink-faint"}`}
                >
                  {u.status}
                </Badge>
              </td>
              <td className="border-b border-hairline px-s2 py-s2 font-mono text-[11.5px] text-ink-secondary">
                {u.ado_bound ? "bound" : u.ado_email ?? "—"}
              </td>
              <td className="border-b border-hairline px-s2 py-s2">
                {u.status !== "deactivated" && (
                  <div className="flex justify-end gap-s2">
                    <Button variant="ghost" size="sm" className="font-mono" onClick={() => void regen(u.id)}>
                      new code
                    </Button>
                    {u.id === me?.id ? (
                      // W-H16: never offer self-deactivation — the backend
                      // 422s it and locking out the last admin is a
                      // recover-only-from-db state.
                      <span
                        className="self-center font-mono text-[10.5px] text-ink-faint"
                        title="you can't deactivate your own account"
                      >
                        you
                      </span>
                    ) : confirmingId === u.id ? (
                      <>
                        <Button
                          variant="destructive"
                          size="sm"
                          className="font-mono"
                          disabled={busyId === u.id}
                          onClick={() => void deactivate(u.id)}
                        >
                          {busyId === u.id ? "working…" : "confirm?"}
                        </Button>
                        <Button variant="ghost" size="sm" className="font-mono" onClick={() => setConfirmingId(null)}>
                          keep
                        </Button>
                      </>
                    ) : (
                      <Button
                        variant="destructive"
                        size="sm"
                        className="font-mono"
                        onClick={() => setConfirmingId(u.id)}
                      >
                        deactivate
                      </Button>
                    )}
                  </div>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      {stats && (
        <div className="flex gap-s6 font-mono text-[11.5px] text-ink-secondary" data-testid="team-stats">
          <span>
            runs: <b className="text-ink-primary">{stats.total_runs}</b>
          </span>
          <span>
            cost: <b className="text-ink-primary">${stats.total_cost_usd.toFixed(2)}</b>
          </span>
          <span>
            modes:{" "}
            <b className="text-ink-primary">
              {Object.entries(stats.runs_by_mode)
                .map(([k, v]) => `${k}×${v}`)
                .join(" ") || "—"}
            </b>
          </span>
        </div>
      )}
    </div>
  );
}
