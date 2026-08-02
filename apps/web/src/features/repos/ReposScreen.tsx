import { useEffect, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { PageHead } from "@/components/ui/page-head";
import { StatusLamp } from "@/components/ui/status-lamp";
import { Tag } from "@/components/ui/tag";
import { api } from "../../lib/api";
import { qk } from "../../lib/queryKeys";

export interface Repo {
  id: number;
  name: string;
  integration_branch: string;
  status: string;
  status_detail: string;
  last_fetch_head: string | null;
}

const SETTLED = ["ready", "ready-no-map", "error"];

const selectClass =
  "h-8 w-full rounded-md border border-hairline bg-bg-raised px-s3 text-[13px] text-ink-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue focus-visible:ring-offset-1 focus-visible:ring-offset-jack";

function repoTone(status: string): "ok" | "warn" | "danger" | "info" {
  if (status === "ready" || status === "ready-no-map") return "ok";
  if (status === "error") return "danger";
  return "info";
}

/** The repo rack: registry as data, onboarding state machine on each card.
 *  Add-Repo fetches branches from the remote — never free-typed. */
export function ReposScreen() {
  const qc = useQueryClient();
  const [adding, setAdding] = useState(false);
  const [name, setName] = useState("");
  const [branches, setBranches] = useState<string[] | null>(null);
  const [branch, setBranch] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [editing, setEditing] = useState<number | null>(null);
  const timer = useRef<ReturnType<typeof setInterval> | null>(null);

  const { data: repos = [], refetch } = useQuery({
    queryKey: qk.repos,
    queryFn: () => api.get<Repo[]>("/repos").catch(() => [] as Repo[]),
  });

  // poll only while any repo is mid-onboarding
  useEffect(() => {
    if (repos.some((r) => !SETTLED.includes(r.status))) {
      timer.current = setInterval(() => void qc.invalidateQueries({ queryKey: qk.repos }), 4000);
    }
    return () => {
      if (timer.current) clearInterval(timer.current);
    };
  }, [repos, qc]);

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
      await refetch();
    } catch (e) {
      setError(e instanceof Error ? e.message : "add failed");
    } finally {
      setBusy(false);
    }
  }

  async function removeRepo(repo: Repo) {
    // Archiving shreds the golden clone — irreversible enough to confirm.
    if (!window.confirm(`remove ${repo.name}? the golden clone is deleted.`)) return;
    setError("");
    try {
      await api.post(`/repos/${repo.id}/archive`);
      await refetch();
    } catch (e) {
      setError(e instanceof Error ? e.message : "remove failed");
    }
  }

  return (
    <div className="mx-auto h-full max-w-[860px] overflow-y-auto px-s8 py-s6">
      <PageHead
        title="repo rack"
        sub={`${repos.length} repos registered — the fleet the agents can touch`}
        actions={
          <Button variant="secondary" size="sm" className="font-mono" onClick={() => setAdding(!adding)}>
            {adding ? "cancel" : "+ add repo"}
          </Button>
        }
      />

      {adding && (
        <div
          data-testid="add-repo-form"
          className="mb-s4 flex max-w-[480px] flex-col gap-s3 rounded-lg border border-hairline bg-bg-panel p-s4 shadow-card animate-enter"
        >
          <div>
            <label htmlFor="repo-name" className="mb-s1 block font-mono text-[10px] uppercase tracking-[0.06em] text-ink-faint">
              ADO repo name
            </label>
            <Input
              id="repo-name"
              value={name}
              onChange={(e) => {
                setName(e.target.value);
                setBranches(null);
              }}
              placeholder="e.g. Billing-Engine"
            />
          </div>
          <div>
            <Button
              variant="secondary"
              size="sm"
              className="font-mono"
              disabled={!name.trim() || busy}
              onClick={() => void fetchBranches()}
            >
              {busy && !branches ? "checking remote…" : "fetch branches"}
            </Button>
          </div>
          {branches && (
            <>
              <div>
                <label htmlFor="repo-branch" className="mb-s1 block font-mono text-[10px] uppercase tracking-[0.06em] text-ink-faint">
                  integration branch (from remote)
                </label>
                <select
                  id="repo-branch"
                  value={branch}
                  onChange={(e) => setBranch(e.target.value)}
                  className={selectClass}
                >
                  {branches.map((b) => (
                    <option key={b} value={b}>
                      {b}
                    </option>
                  ))}
                </select>
              </div>
              <div>
                <Button size="sm" className="font-mono" disabled={!branch || busy} onClick={() => void addRepo()}>
                  register &amp; onboard
                </Button>
              </div>
            </>
          )}
          {error && <p className="text-[12.5px] text-danger-bright">{error}</p>}
        </div>
      )}

      {!adding && error && <p className="mb-s3 text-[12.5px] text-danger-bright">{error}</p>}

      <div className="grid grid-cols-[repeat(auto-fill,minmax(240px,1fr))] gap-s3">
        {repos.map((repo) => (
          <article
            key={repo.id}
            data-testid={`repo-${repo.name}`}
            className="rounded-lg border border-hairline bg-bg-panel p-s4 shadow-card"
          >
            <header className="mb-s2 flex items-center justify-between gap-s2">
              <strong className="font-mono text-[13px] font-semibold text-ink-primary">{repo.name}</strong>
              <StatusLamp tone={repoTone(repo.status)} label={repo.status} />
            </header>
            <div className="flex flex-col gap-s2">
              {editing === repo.id ? (
                <RepoBranchEditor
                  repo={repo}
                  onDone={() => {
                    setEditing(null);
                    void refetch();
                  }}
                />
              ) : (
                <div className="flex items-center gap-s2">
                  <Tag>{repo.integration_branch}</Tag>
                  <button
                    type="button"
                    className="font-mono text-[10.5px] text-ink-faint underline-offset-2 hover:text-ink-primary hover:underline"
                    onClick={() => setEditing(repo.id)}
                  >
                    change
                  </button>
                </div>
              )}
              {repo.status_detail && <p className="text-[12px] leading-[1.5] text-ink-secondary">{repo.status_detail}</p>}
              <p className="font-mono text-[10.5px] text-ink-faint">
                {repo.last_fetch_head ? `HEAD ${repo.last_fetch_head.slice(0, 7)}` : "not fetched yet"}
              </p>
              <div>
                <button
                  type="button"
                  className="font-mono text-[10.5px] text-ink-faint underline-offset-2 hover:text-danger-bright hover:underline"
                  onClick={() => void removeRepo(repo)}
                >
                  remove
                </button>
              </div>
            </div>
          </article>
        ))}
      </div>
    </div>
  );
}

/** Branch switch on a registered repo. The list is fetched from the remote for
 *  the same reason the add form does it — a typo'd branch is an unclonable repo.
 *  The golden clone follows on the next fetcher pass (5 min). */
function RepoBranchEditor({ repo, onDone }: { repo: Repo; onDone: () => void }) {
  const [branches, setBranches] = useState<string[] | null>(null);
  const [branch, setBranch] = useState(repo.integration_branch);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let live = true;
    api
      .get<{ branches: string[] }>(`/repos/remote-branches?name=${encodeURIComponent(repo.name)}`)
      .then((d) => live && setBranches(d.branches))
      .catch((e: unknown) => live && setError(e instanceof Error ? e.message : "could not reach remote"));
    return () => {
      live = false;
    };
  }, [repo.name]);

  async function save() {
    setBusy(true);
    setError("");
    try {
      await api.patch(`/repos/${repo.id}`, {
        integration_branch: branch,
        audit_note: `switched from ${repo.integration_branch}`,
      });
      onDone();
    } catch (e) {
      setError(e instanceof Error ? e.message : "branch change failed");
      setBusy(false);
    }
  }

  return (
    <div className="flex flex-col gap-s2">
      {branches ? (
        <select
          aria-label="integration branch"
          value={branch}
          onChange={(e) => setBranch(e.target.value)}
          className={selectClass}
        >
          {branches.map((b) => (
            <option key={b} value={b}>
              {b}
            </option>
          ))}
        </select>
      ) : (
        <p className="font-mono text-[10.5px] text-ink-faint">loading branches…</p>
      )}
      <div className="flex gap-s2">
        <Button
          size="sm"
          className="font-mono"
          disabled={busy || !branches || branch === repo.integration_branch}
          onClick={() => void save()}
        >
          save
        </Button>
        <Button variant="secondary" size="sm" className="font-mono" onClick={onDone}>
          cancel
        </Button>
      </div>
      {error && <p className="text-[12px] text-danger-bright">{error}</p>}
    </div>
  );
}
