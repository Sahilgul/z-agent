import { useEffect, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { BranchPicker } from "@/components/ui/branch-picker";
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

  // poll only while any repo is mid-onboarding. L-39: depend on the
  // derived boolean, not the `repos` array — every refetch produced a new
  // `repos` reference, tearing down and rebuilding the interval each
  // cycle (and resetting the 4s clock). The boolean only flips when the
  // polling need actually changes (unsettled↔all-settled).
  const polling = repos.some((r) => !SETTLED.includes(r.status));
  useEffect(() => {
    if (!polling) return;
    timer.current = setInterval(() => void qc.invalidateQueries({ queryKey: qk.repos }), 4000);
    return () => {
      if (timer.current) clearInterval(timer.current);
    };
  }, [polling, qc]);

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
    <div className="mx-auto h-full max-w-[1180px] overflow-y-auto px-s8 py-s6">
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
          className="mb-s4 rounded-lg border border-hairline bg-bg-panel p-s5 shadow-card animate-enter"
        >
          <div className="flex flex-wrap items-end gap-s3">
            <div className="min-w-[260px] flex-1">
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
            {branches && (
              <div className="min-w-[260px] flex-1">
                <label htmlFor="repo-branch" className="mb-s1 block font-mono text-[10px] uppercase tracking-[0.06em] text-ink-faint">
                  integration branch (from remote)
                </label>
                <BranchPicker id="repo-branch" branches={branches} value={branch} onChange={setBranch} />
              </div>
            )}
            {branches ? (
              <Button size="sm" className="font-mono" disabled={!branch || busy} onClick={() => void addRepo()}>
                register &amp; onboard
              </Button>
            ) : (
              <Button
                variant="secondary"
                size="sm"
                className="font-mono"
                disabled={!name.trim() || busy}
                onClick={() => void fetchBranches()}
              >
                {busy ? "checking remote…" : "fetch branches"}
              </Button>
            )}
          </div>
          {error && <p className="mt-s3 text-[12.5px] text-danger-bright">{error}</p>}
        </div>
      )}

      {!adding && error && <p className="mb-s3 text-[12.5px] text-danger-bright">{error}</p>}

      {/* A rack reads as stacked rails, not as a grid of loose tiles: one row per
          repo, columns aligned so branch and HEAD scan vertically down the list. */}
      <div className="overflow-hidden rounded-lg border border-hairline bg-bg-panel shadow-card">
        <div className="grid grid-cols-[minmax(0,1fr)_180px_150px_auto] items-center gap-s4 border-b border-hairline px-s5 py-s2 font-mono text-[10px] uppercase tracking-[0.06em] text-ink-faint">
          <span>repo</span>
          <span>integration branch</span>
          <span>head</span>
          <span className="w-[120px] text-right">actions</span>
        </div>
        {repos.map((repo) => (
          <article
            key={repo.id}
            data-testid={`repo-${repo.name}`}
            className="group grid grid-cols-[minmax(0,1fr)_180px_150px_auto] items-center gap-s4 border-b border-hairline px-s5 py-s3 last:border-b-0 hover:bg-bg-raised"
          >
            <div className="flex min-w-0 flex-col gap-s1">
              <div className="flex items-center gap-s3">
                <strong className="truncate font-mono text-[13px] font-semibold text-ink-primary">{repo.name}</strong>
                <StatusLamp tone={repoTone(repo.status)} label={repo.status} />
              </div>
              {repo.status_detail && (
                <p className="truncate text-[12px] leading-[1.5] text-ink-secondary">{repo.status_detail}</p>
              )}
            </div>

            {editing === repo.id ? (
              <div className="col-span-3">
                <RepoBranchEditor
                  repo={repo}
                  onDone={() => {
                    setEditing(null);
                    void refetch();
                  }}
                />
              </div>
            ) : (
              <>
                <div className="min-w-0">
                  <Tag>{repo.integration_branch}</Tag>
                </div>
                <p className="font-mono text-[10.5px] text-ink-faint">
                  {repo.last_fetch_head ? repo.last_fetch_head.slice(0, 7) : "not fetched yet"}
                </p>
                {/* Destructive and rare actions stay out of the way until hover,
                    so the resting state is just the data. */}
                <div className="flex w-[120px] justify-end gap-s3 opacity-0 transition-opacity focus-within:opacity-100 group-hover:opacity-100">
                  <button
                    type="button"
                    className="font-mono text-[10.5px] text-ink-faint underline-offset-2 hover:text-ink-primary hover:underline"
                    onClick={() => setEditing(repo.id)}
                  >
                    change
                  </button>
                  <button
                    type="button"
                    className="font-mono text-[10.5px] text-ink-faint underline-offset-2 hover:text-danger-bright hover:underline"
                    onClick={() => void removeRepo(repo)}
                  >
                    remove
                  </button>
                </div>
              </>
            )}
          </article>
        ))}
        {repos.length === 0 && (
          <p className="px-s5 py-s6 text-center text-[12.5px] text-ink-faint">
            no repos yet — add one to give the agents something to touch
          </p>
        )}
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
    <div className="flex items-center gap-s3">
      {branches ? (
        <BranchPicker branches={branches} value={branch} onChange={setBranch} className="w-[280px]" />
      ) : (
        <p className="font-mono text-[10.5px] text-ink-faint">loading branches…</p>
      )}
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
      {error && <p className="text-[12px] text-danger-bright">{error}</p>}
    </div>
  );
}
