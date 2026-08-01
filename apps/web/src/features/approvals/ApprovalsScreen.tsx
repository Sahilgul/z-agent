import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Button } from "@/components/ui/button";
import { EmptyState } from "@/components/ui/empty-state";
import { PageHead } from "@/components/ui/page-head";
import { Skeleton } from "@/components/ui/skeleton";
import { api } from "../../lib/api";
import { hasSubscription, pushSupported, subscribeToPush } from "../../lib/push";
import { qk } from "../../lib/queryKeys";
import type { Approval } from "../../types";

/** Push opt-in: the ask appears ONLY here — after the user's first
 *  AwaitingYou moment — never on landing. */
function PushOptIn() {
  const [state, setState] = useState<"hidden" | "ask" | "done">("hidden");
  useEffect(() => {
    if (!pushSupported()) return;
    void hasSubscription().then((subbed) => setState(subbed ? "hidden" : "ask"));
  }, []);
  if (state !== "ask") return null;
  return (
    <div
      data-testid="push-ask"
      className="mb-s4 flex flex-wrap items-center gap-s3 rounded-lg border border-hairline bg-bg-panel px-s4 py-s3 shadow-card"
    >
      <span className="min-w-[220px] flex-1 text-[12.5px] text-ink-secondary">
        approve from your phone — notifications deep-link straight to each card
      </span>
      <Button size="sm" className="font-mono" onClick={() => void subscribeToPush().then((ok) => setState(ok ? "done" : "hidden"))}>
        enable push
      </Button>
      <Button variant="ghost" size="sm" className="font-mono" onClick={() => setState("hidden")}>
        not now
      </Button>
    </div>
  );
}

/** Approval inbox: pending tool-permission asks as decision cards — the
 *  tool-call JSON sits in a jack well; allow/deny are the only accent
 *  moves. Decisions are optimistic with rollback. */
export function ApprovalsScreen() {
  const qc = useQueryClient();
  const { data: approvals = [], isLoading } = useQuery({
    queryKey: qk.approvals,
    queryFn: () => api.get<Approval[]>("/approvals"),
  });

  const decide = useMutation({
    mutationFn: ({ id, decision }: { id: string; decision: "allow_once" | "deny" }) =>
      api.post(`/approvals/${id}/decide`, { decision }),
    onMutate: async ({ id }) => {
      await qc.cancelQueries({ queryKey: qk.approvals });
      const prev = qc.getQueryData<Approval[]>(qk.approvals);
      qc.setQueryData(qk.approvals, (prev ?? []).filter((a) => a.id !== id));
      return { prev };
    },
    onError: (_e, _v, ctx) => qc.setQueryData(qk.approvals, ctx?.prev),
    onSettled: () => void qc.invalidateQueries({ queryKey: qk.approvals }),
  });

  return (
    <div className="mx-auto h-full max-w-[760px] overflow-y-auto px-s8 py-s6">
      <PageHead title="approval inbox" sub="gated autonomy — tool calls waiting on you" />

      {approvals.length > 0 && <PushOptIn />}

      {isLoading ? (
        <div className="flex flex-col gap-s3" aria-label="loading approvals">
          {[0, 1].map((i) => (
            <div key={i} className="rounded-lg border border-hairline bg-bg-panel p-s4 shadow-card">
              <Skeleton className="mb-s2 h-3 w-40 rounded-sm" />
              <Skeleton className="mb-s3 h-4 w-24 rounded-sm" />
              <Skeleton className="h-24 w-full rounded-sm" />
            </div>
          ))}
        </div>
      ) : approvals.length === 0 ? (
        <EmptyState hint="nothing waiting on you" />
      ) : (
        approvals.map((a) => (
          <article key={a.id} className="mb-s3 rounded-lg border border-hairline bg-bg-panel p-s4 shadow-card">
            <div className="mb-s1 font-mono text-[10.5px] text-ink-faint">
              run {a.run_id.slice(0, 8)} · lane {a.lane_id.slice(0, 8)}
            </div>
            <div className="mb-s2 font-mono text-[13px] font-semibold text-blue-bright">{a.tool}</div>
            <pre className="max-h-[180px] overflow-auto rounded-md bg-jack p-s3 font-mono text-[11px] leading-[1.5] text-ink-primary">
              {JSON.stringify(a.input, null, 2).slice(0, 600)}
            </pre>
            <div className="mt-s3 flex gap-s2">
              <Button size="sm" className="font-mono" onClick={() => decide.mutate({ id: a.id, decision: "allow_once" })}>
                allow once
              </Button>
              <Button
                variant="destructive"
                size="sm"
                className="font-mono"
                onClick={() => decide.mutate({ id: a.id, decision: "deny" })}
              >
                deny
              </Button>
            </div>
          </article>
        ))
      )}
    </div>
  );
}
