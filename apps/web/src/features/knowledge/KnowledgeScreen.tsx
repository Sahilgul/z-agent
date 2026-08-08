import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { EmptyState } from "@/components/ui/empty-state";
import { PageHead } from "@/components/ui/page-head";
import { Skeleton } from "@/components/ui/skeleton";
import { toast } from "@/components/ui/sonner";
import { api } from "../../lib/api";
import { qk } from "../../lib/queryKeys";

interface KnowledgeEntry {
  id: number;
  content: string;
  trigger_description: string | null;
  scope: string;
  repo: string | null;
  status: string;
  source_run_id: string | null;
  proposed_scope: string | null;
}

interface RepoRow {
  id: number;
  name: string;
  status?: string;
}

function ScopeBadge({ entry }: { entry: KnowledgeEntry }) {
  return (
    <Badge variant="outline" className="rounded-full border-hairline px-2.5 py-0.5 font-mono text-[10px] font-normal text-ink-secondary">
      {entry.scope} · {entry.status}
    </Badge>
  );
}

/** Draft inbox card: the PHI checkpoint — a human decides where the
 *  distilled lesson may live (user / global / repo) before it's shared. */
function DraftCard({ entry, repos }: { entry: KnowledgeEntry; repos: RepoRow[] }) {
  const qc = useQueryClient();
  // W9-M5: default the selector to what the distiller/proposer suggested
  // (was a flat "global" every time, silently overriding the suggestion).
  const proposed = entry.proposed_scope && ["global", "user", "repo"].includes(entry.proposed_scope)
    ? entry.proposed_scope
    : "global";
  const [scope, setScope] = useState(proposed);
  const [repo, setRepo] = useState(entry.repo ?? "");

  const approve = useMutation({
    mutationFn: () => api.post(`/knowledge/${entry.id}/approve`, { scope, repo: scope === "repo" ? repo : null }),
    // M-85: surface failures (was no error UI) so the user knows the
    // approve didn't land and can retry instead of assuming it worked.
    onError: (err) => {
      toast.error("approve failed", {
        description: err instanceof Error ? err.message : undefined,
      });
    },
    onSettled: () => {
      void qc.invalidateQueries({ queryKey: qk.knowledgeDrafts });
      void qc.invalidateQueries({ queryKey: qk.knowledge() });
    },
  });

  // W9-M4: the missing reject path — the endpoint existed, the UI didn't.
  const reject = useMutation({
    mutationFn: () => api.post(`/knowledge/${entry.id}/reject`, {}),
    onError: (err) => {
      toast.error("reject failed", {
        description: err instanceof Error ? err.message : undefined,
      });
    },
    onSettled: () => {
      void qc.invalidateQueries({ queryKey: qk.knowledgeDrafts });
      void qc.invalidateQueries({ queryKey: qk.knowledge() });
    },
  });

  return (
    <article className="rounded-lg border border-warn/40 bg-bg-panel p-s4 shadow-card">
      <div className="mb-s1 font-mono text-[10px] uppercase tracking-[0.08em] text-warn-bright">
        PHI checkpoint — review before sharing
      </div>
      <div className="text-[13px] font-semibold leading-[1.4] text-ink-primary">{entry.content}</div>
      {entry.trigger_description && (
        <div className="mt-s1 text-[12px] text-ink-secondary">trigger: {entry.trigger_description}</div>
      )}
      {entry.source_run_id && (
        <div className="mt-s1 font-mono text-[10.5px] text-ink-faint">
          distilled from run {entry.source_run_id.slice(0, 9)}
        </div>
      )}
      <div className="mt-s3 flex flex-wrap items-center gap-s2">
        <select
          value={scope}
          onChange={(e) => setScope(e.target.value)}
          aria-label="share scope"
          className="h-8 rounded-md border border-hairline bg-bg-raised px-s3 text-[12.5px] text-ink-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue focus-visible:ring-offset-1 focus-visible:ring-offset-jack"
        >
          <option value="global">share: global</option>
          <option value="user">share: user</option>
          <option value="repo">share: repo</option>
        </select>
        {scope === "repo" && (
          // W9-M6: picker over the registry, not free text — a typo'd name
          // black-holed the lesson (it retrieved for a repo string no run
          // ever carries). The backend re-validates the same list.
          <select
            value={repo}
            onChange={(e) => setRepo(e.target.value)}
            aria-label="repo name"
            className="h-8 rounded-md border border-hairline bg-bg-raised px-s3 text-[12.5px] text-ink-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue focus-visible:ring-offset-1 focus-visible:ring-offset-jack"
          >
            <option value="">pick a repo…</option>
            {repos.map((r) => (
              <option key={r.id} value={r.name}>
                {r.name}
              </option>
            ))}
          </select>
        )}
        <Button
          size="sm"
          className="font-mono"
          // M-85: guard against double-submit while the POST is in flight
          // (was no isPending check, so a second click sent a duplicate).
          disabled={(scope === "repo" && !repo.trim()) || approve.isPending || reject.isPending}
          onClick={() => approve.mutate()}
        >
          {approve.isPending ? "approving…" : "approve"}
        </Button>
        <Button
          variant="destructive"
          size="sm"
          className="font-mono"
          disabled={approve.isPending || reject.isPending}
          onClick={() => reject.mutate()}
        >
          {reject.isPending ? "rejecting…" : "reject"}
        </Button>
      </div>
    </article>
  );
}

/** Knowledge: a draft inbox (PHI checkpoint) above the shared corpus.
 *  Own drafts appear in both — the inbox is where you decide their scope. */
export function KnowledgeScreen() {
  const { data: drafts = [], isLoading: loadingDrafts, error: draftsError } = useQuery({
    queryKey: qk.knowledgeDrafts,
    queryFn: () => api.get<KnowledgeEntry[]>("/knowledge/pending"),
  });
  const { data: corpus = [], isLoading: loadingCorpus, error: corpusError } = useQuery({
    queryKey: qk.knowledge(),
    queryFn: () => api.get<KnowledgeEntry[]>("/knowledge"),
  });
  // W9-M6: the repo-scope picker is backed by the registry query.
  const { data: repos = [] } = useQuery({
    queryKey: qk.repos,
    queryFn: () => api.get<RepoRow[]>("/repos"),
  });

  return (
    <div className="mx-auto h-full max-w-[860px] overflow-y-auto px-s8 py-s6">
      <PageHead title="knowledge" sub="distilled lessons the run carries forward" />

      {loadingDrafts ? (
        <Skeleton className="mb-s6 h-32 w-full rounded-lg" />
      ) : draftsError ? (
        // W9-L16: a failed inbox fetch used to render as a silently EMPTY
        // inbox — indistinguishable from "nothing to review".
        <p role="alert" className="mb-s6 font-mono text-[12.5px] text-danger-bright">
          drafts failed to load — {draftsError instanceof Error ? draftsError.message : "unknown error"}
        </p>
      ) : (
        drafts.length > 0 && (
          <div className="mb-s6 flex flex-col gap-s3">
            {drafts.map((d) => (
              <DraftCard key={d.id} entry={d} repos={repos} />
            ))}
          </div>
        )
      )}

      <div className="text-micro mb-s3 text-ink-faint">shared corpus</div>
      {loadingCorpus ? (
        <div className="flex flex-col gap-s3" aria-label="loading knowledge">
          {[0, 1].map((i) => (
            <Skeleton key={i} className="h-20 w-full rounded-lg" />
          ))}
        </div>
      ) : corpusError ? (
        <p role="alert" className="font-mono text-[12.5px] text-danger-bright">
          corpus failed to load — {corpusError instanceof Error ? corpusError.message : "unknown error"}
        </p>
      ) : corpus.length === 0 ? (
        <EmptyState hint="nothing distilled yet" />
      ) : (
        corpus.map((e) => (
          <article key={e.id} className="mb-s3 rounded-lg border border-hairline bg-bg-panel p-s4 shadow-card">
            <div className="text-[13px] font-semibold leading-[1.4] text-ink-primary">{e.content}</div>
            {e.trigger_description && (
              <div className="mt-s1 text-[12px] text-ink-secondary">trigger: {e.trigger_description}</div>
            )}
            <div className="mt-s2">
              <ScopeBadge entry={e} />
            </div>
          </article>
        ))
      )}
    </div>
  );
}
