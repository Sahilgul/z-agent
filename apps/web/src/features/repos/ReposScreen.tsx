import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../../lib/api";

export interface Repo {
  id: number;
  name: string;
  integration_branch: string;
  status: string;
  status_detail: string;
  last_fetch_head: string | null;
}

const SETTLED = ["ready", "ready-no-map", "error"];

function StatusTag({ status }: { status: string }) {
  const cls =
    status === "ready" ? "st-completed" :
    status === "error" ? "st-failed" : "st-running";
  return <span className={`tag mono ${cls}`}>{status}</span>;
}

/** The repo rack (plan §1b): registry as data, onboarding state machine on
 *  each card. Add-Repo fetches branches from the remote — never free-typed. */
export function ReposScreen() {
  const [repos, setRepos] = useState<Repo[]>([]);
  const [adding, setAdding] = useState(false);
  const [name, setName] = useState("");
  const [branches, setBranches] = useState<string[] | null>(null);
  const [branch, setBranch] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const timer = useRef<ReturnType<typeof setInterval> | null>(null);

  const load = useCallback(async () => {
    try {
      const data = await api.get<Repo[]>("/repos");
      setRepos(data);
      return data;
    } catch {
      return [] as Repo[];
    }
  }, []);

  useEffect(() => {
    void load().then((data) => {
      // poll only while any repo is mid-onboarding
      if (data.some((r) => !SETTLED.includes(r.status))) {
        timer.current = setInterval(() => void load(), 4000);
      }
    });
    return () => {
      if (timer.current) clearInterval(timer.current);
    };
  }, [load]);

  async function fetchBranches() {
    setBusy(true);
    setError("");
    setBranches(null);
    try {
      const data = await api.get<{ branches: string[] }>(
        `/repos/remote-branches?name=${encodeURIComponent(name.trim())}`
      );
      setBranches(data.branches);
      setBranch(data.branches[0] ?? "");
    } catch (e) {
      setError(e instanceof Error ? e.message : "could not reach remote");
    } finally {
      setBusy(false);
    }
  }

  async function addRepo() {
    setBusy(true);
    setError("");
    try {
      await api.post("/repos", { name: name.trim(), integration_branch: branch });
      setAdding(false);
      setName("");
      setBranches(null);
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : "add failed");
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="repos-screen">
      <header className="screen-head">
        <h1>repo rack</h1>
        <p className="muted">{repos.length} repos registered — the fleet the agents can touch</p>
        <button className="btn btn-mono" onClick={() => setAdding(!adding)}>
          {adding ? "cancel" : "+ add repo"}
        </button>
      </header>

      {adding && (
        <div className="card add-form" data-testid="add-repo-form">
          <div className="field">
            <label className="mono">ADO repo name</label>
            <input
              value={name}
              onChange={(e) => { setName(e.target.value); setBranches(null); }}
              placeholder="e.g. Billing-Engine"
            />
          </div>
          <button
            className="btn btn-mono btn-ghost"
            disabled={!name.trim() || busy}
            onClick={() => void fetchBranches()}
          >
            {busy && !branches ? "checking remote…" : "fetch branches"}
          </button>
          {branches && (
            <>
              <div className="field">
                <label className="mono">integration branch (from remote)</label>
                <select value={branch} onChange={(e) => setBranch(e.target.value)}>
                  {branches.map((b) => <option key={b} value={b}>{b}</option>)}
                </select>
              </div>
              <button className="btn btn-primary" disabled={!branch || busy} onClick={() => void addRepo()}>
                register &amp; onboard
              </button>
            </>
          )}
          {error && <p className="err">{error}</p>}
        </div>
      )}

      <div className="card-grid">
        {repos.map((repo) => (
          <article className="card" key={repo.id} data-testid={`repo-${repo.name}`}>
            <header className="card-head">
              <strong className="card-title mono">{repo.name}</strong>
              <StatusTag status={repo.status} />
            </header>
            <div className="card-body">
              <span className="chip mono">{repo.integration_branch}</span>
              {repo.status_detail && <p className="muted">{repo.status_detail}</p>}
              <p className="faint mono">
                {repo.last_fetch_head ? `HEAD ${repo.last_fetch_head.slice(0, 7)}` : "not fetched yet"}
              </p>
            </div>
          </article>
        ))}
      </div>
      <style>{`
        .repos-screen { max-width: 860px; margin: 0 auto; padding: 22px 18px; overflow-y: auto; height: 100%; }
        .add-form { padding: 16px; margin-bottom: 16px; display: flex; flex-direction: column; gap: 10px; max-width: 480px; }
        .field label { display: block; font-size: 10.5px; text-transform: uppercase; letter-spacing: .06em; color: var(--ink-faint); margin-bottom: 5px; }
        .field input, .field select { width: 100%; background: var(--jack); border: 1px solid var(--hairline); border-radius: var(--radius); color: var(--ink-primary); padding: 8px 10px; font-size: 13px; }
      `}</style>
    </section>
  );
}
