import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Button } from "@/components/ui/button";
import { api } from "../lib/api";
import { hasSubscription, pushSupported, subscribeToPush } from "../lib/push";
import { qk } from "../lib/queryKeys";
import type { Approval } from "../types";

/** Push opt-in: the ask appears ONLY after the user's first AwaitingYou
 *  moment — never on landing. */
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
      className="mb-s3 flex flex-wrap items-center gap-s3 rounded-md border border-hairline bg-bg-module px-s3 py-s2"
    >
      <span className="min-w-[220px] flex-1 text-[12.5px] text-ink-secondary">
        approve from your phone — notifications deep-link straight to each card
      </span>
      <Button
        size="sm"
        className="font-mono"
        onClick={() => void subscribeToPush().then((ok) => setState(ok ? "done" : "hidden"))}
      >
        enable push
      </Button>
      <Button variant="ghost" size="sm" className="font-mono" onClick={() => setState("hidden")}>
        not now
      </Button>
    </div>
  );
}

/** Tool-permission asks for the open run, docked in the session above the
 *  composer: gated autonomy stops the thread until you allow or deny, so the
 *  decision belongs where you are already watching. Optimistic w/ rollback. */
/** Minutes left before the worker denies on its own — the card is not an open
 *  invitation, so say so rather than letting it silently stop working. */
function expiryLabel(expiresAt: string | null | undefined): string | null {
  if (!expiresAt) return null;
  const ms = new Date(expiresAt).getTime() - Date.now();
  if (Number.isNaN(ms)) return null;
  if (ms <= 0) return "expired — denied";
  const mins = Math.ceil(ms / 60_000);
  return `auto-denies in ${mins}m`;
}

export function ApprovalQueue({ runId }: { runId: string }) {
  const qc = useQueryClient();
  const { data: approvals = [] } = useQuery({
    queryKey: [...qk.approvals, runId],
    queryFn: () => api.get<Approval[]>(`/approvals?run_id=${encodeURIComponent(runId)}`),
    refetchInterval: 15_000,
  });

  const key = [...qk.approvals, runId];
  const decide = useMutation({
    mutationFn: ({ id, decision }: { id: string; decision: "allow_once" | "always_allow" | "deny" }) =>
      api.post(`/approvals/${id}/decide`, { decision }),
    onMutate: async ({ id }) => {
      await qc.cancelQueries({ queryKey: key });
      const prev = qc.getQueryData<Approval[]>(key);
      qc.setQueryData(key, (prev ?? []).filter((a) => a.id !== id));
      return { prev };
    },
    onError: (_e, vars, ctx) => {
      // H-59: don't blindly restore the stale `prev` snapshot — approvals
      // that arrived during the failed mutation (refetch/WS) would be
      // clobbered and vanish until the next refetch. Re-insert only the
      // failed approval into the CURRENT cache, preserving any new ones.
      const cur = qc.getQueryData<Approval[]>(key) ?? [];
      const failed = (ctx?.prev ?? []).find((a) => a.id === vars.id);
      if (failed && !cur.some((a) => a.id === vars.id)) {
        qc.setQueryData(key, [...cur, failed]);
      }
    },
    onSettled: () => void qc.invalidateQueries({ queryKey: key }),
  });

  if (approvals.length === 0) return null;

  return (
    <div
      data-testid="approval-queue"
      className="border-t border-warn/40 bg-warn-soft px-s4 py-s3"
      role="region"
      aria-label="approvals waiting on you"
    >
      <div className="text-micro mb-s2 text-warn-bright">waiting on you</div>
      <PushOptIn />
      {approvals.map((a) => (
        <article key={a.id} className="mb-s2 rounded-md border border-hairline bg-bg-panel p-s3">
          <div className="mb-s2 flex items-center justify-between">
            <span className="font-mono text-[12.5px] font-semibold text-blue-bright">{a.kind}</span>
            <span className="font-mono text-[10.5px] text-ink-faint">
              {a.thread_id ? `thread ${a.thread_id.slice(0, 8)}` : "run"}
              {expiryLabel(a.expires_at) ? ` · ${expiryLabel(a.expires_at)}` : ""}
            </span>
          </div>
          <pre className="max-h-[140px] overflow-auto rounded-md bg-jack p-s3 font-mono text-[11px] leading-[1.5] text-ink-primary">
            {JSON.stringify(a.payload, null, 2).slice(0, 600)}
          </pre>
          <div className="mt-s3 flex gap-s2">
            <Button size="sm" className="font-mono" onClick={() => decide.mutate({ id: a.id, decision: "allow_once" })}>
              allow once
            </Button>
            <Button
              variant="outline"
              size="sm"
              className="font-mono"
              onClick={() => decide.mutate({ id: a.id, decision: "always_allow" })}
            >
              always allow
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
      ))}
    </div>
  );
}
