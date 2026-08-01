import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { EmptyState } from "@/components/ui/empty-state";
import { PageHead } from "@/components/ui/page-head";
import { Skeleton } from "@/components/ui/skeleton";
import { api } from "../../lib/api";
import { qk } from "../../lib/queryKeys";

export interface Proposal {
  id: number;
  source: "janitor" | "perfector";
  repo: string | null;
  title: string;
  body: string;
  evidence: string[];
  impact: string;
  confidence: string;
  rank_score: number;
  status: string;
  promoted_run_id: string | null;
  created_at: string | null;
}

const LEVEL_TONE: Record<string, string> = {
  high: "border-transparent bg-danger-soft text-danger-bright",
  medium: "border-transparent bg-warn-soft text-warn-bright",
  low: "border-hairline text-ink-faint",
};

function LevelTag({ label }: { label: string }) {
  const tone = LEVEL_TONE[label.split(" ").pop() ?? ""] ?? LEVEL_TONE.low;
  return (
    <Badge variant="outline" className={`rounded-full px-2.5 py-0.5 font-mono text-[10px] font-normal ${tone}`}>
      {label}
    </Badge>
  );
}

function SourceTag({ source }: { source: Proposal["source"] }) {
  return (
    <Badge
      variant="outline"
      className={`rounded-full px-2.5 py-0.5 font-mono text-[10px] font-normal ${
        source === "janitor"
          ? "border-transparent bg-blue-soft text-blue-bright"
          : "border-transparent bg-warn-soft text-warn-bright"
      }`}
    >
      {source}
    </Badge>
  );
}

/** Ranked proposals from the janitor and perfector patrols — accept promotes
 *  to a development run, dismiss teaches the flywheel. Decisions are
 *  optimistic with rollback. */
export function ProposalsScreen() {
  const qc = useQueryClient();
  const [showAll, setShowAll] = useState(false);

  const { data: items = [], isLoading } = useQuery({
    queryKey: qk.proposals(showAll),
    queryFn: () => api.get<{ items: Proposal[] }>(showAll ? "/proposals?status=" : "/proposals").then((d) => d.items),
  });

  const decide = useMutation({
    mutationFn: ({ id, action }: { id: number; action: "accept" | "dismiss" }) =>
      api.post(`/proposals/${id}/${action}`, {}),
    onMutate: async ({ id, action }) => {
      await qc.cancelQueries({ queryKey: qk.proposals(showAll) });
      const prev = qc.getQueryData<Proposal[]>(qk.proposals(showAll));
      const status = action === "accept" ? "accepted" : "dismissed";
      qc.setQueryData(
        qk.proposals(showAll),
        showAll ? (prev ?? []).map((p) => (p.id === id ? { ...p, status } : p)) : (prev ?? []).filter((p) => p.id !== id)
      );
      return { prev };
    },
    onError: (_e, _v, ctx) => qc.setQueryData(qk.proposals(showAll), ctx?.prev),
    onSettled: () => void qc.invalidateQueries({ queryKey: qk.proposals(showAll) }),
  });

  return (
    <div className="mx-auto h-full max-w-[940px] overflow-y-auto px-s8 py-s6">
      <PageHead title="patrol" sub="ranked proposals from the janitor and perfector — accept promotes to a run, dismiss teaches the flywheel" />

      <label className="mb-s4 inline-flex cursor-pointer items-center gap-s2 font-mono text-[11.5px] text-ink-secondary">
        <input
          type="checkbox"
          checked={showAll}
          onChange={(e) => setShowAll(e.target.checked)}
          className="size-4 accent-[var(--color-blue)]"
        />
        show decided
      </label>

      {isLoading ? (
        <div className="flex flex-col gap-s3" aria-label="loading proposals">
          {[0, 1].map((i) => (
            <div key={i} className="rounded-lg border border-hairline bg-bg-panel p-s4 shadow-card">
              <Skeleton className="mb-s2 h-4 w-1/2 rounded-sm" />
              <Skeleton className="mb-s2 h-3 w-full rounded-sm" />
              <Skeleton className="h-3 w-32 rounded-sm" />
            </div>
          ))}
        </div>
      ) : items.length === 0 ? (
        <EmptyState hint="no proposals — the patrols run on schedule" />
      ) : (
        items.map((item) => {
          const decided = item.status !== "proposed";
          return (
            <article
              key={item.id}
              data-testid={`proposal-${item.id}`}
              className="mb-s3 rounded-lg border border-hairline bg-bg-panel p-s4 shadow-card"
            >
              <header className="mb-s2 flex flex-wrap items-center gap-s2">
                <SourceTag source={item.source} />
                <strong className="min-w-[200px] flex-1 text-[14px] font-semibold text-ink-primary">{item.title}</strong>
                <span className="rounded-full border border-dashed border-hairline px-2.5 py-0.5 font-mono text-[10px] text-ink-faint">
                  score {item.rank_score}
                </span>
              </header>
              <p className="mb-s2 text-[13px] leading-[1.55] text-ink-secondary">{item.body}</p>
              <div className="mb-s2 flex flex-wrap gap-s2">
                <LevelTag label={`impact ${item.impact}`} />
                <LevelTag label={`confidence ${item.confidence}`} />
                {item.repo && (
                  <span className="rounded-full border border-dashed border-hairline px-2.5 py-0.5 font-mono text-[10px] text-ink-faint">
                    {item.repo}
                  </span>
                )}
              </div>
              <ul className="mb-s2 list-disc pl-s4 font-mono text-[11px] leading-[1.7] text-ink-faint">
                {item.evidence.map((e) => (
                  <li key={e}>{e}</li>
                ))}
              </ul>
              {!decided ? (
                <footer className="flex items-center gap-s2 border-t border-hairline pt-s3">
                  <Button size="sm" className="font-mono" onClick={() => decide.mutate({ id: item.id, action: "accept" })}>
                    accept
                  </Button>
                  <Button
                    variant="destructive"
                    size="sm"
                    className="font-mono"
                    onClick={() => decide.mutate({ id: item.id, action: "dismiss" })}
                  >
                    dismiss
                  </Button>
                </footer>
              ) : (
                <footer className="flex items-center gap-s2 border-t border-hairline pt-s3">
                  <Badge variant="outline" className="rounded-full border-hairline px-2.5 py-0.5 font-mono text-[10px] font-normal text-ink-secondary">
                    {item.status}
                  </Badge>
                  {item.promoted_run_id && (
                    <span className="rounded-full border border-dashed border-hairline px-2.5 py-0.5 font-mono text-[10px] text-ink-faint">
                      run {item.promoted_run_id.slice(0, 8)}
                    </span>
                  )}
                </footer>
              )}
            </article>
          );
        })
      )}
    </div>
  );
}
