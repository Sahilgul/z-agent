import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Button } from "@/components/ui/button";
import { toast } from "@/components/ui/sonner";
import { ApiError, api } from "../lib/api";
import { parseIso } from "../lib/time";
import { hasSubscription, pushSupported, subscribeToPush, unsubscribeFromPush } from "../lib/push";
import { qk } from "../lib/queryKeys";
import type { Approval } from "../types";
import { CodeView } from "./CodeView";

/** Push opt-in: the ask appears ONLY after the user's first AwaitingYou
 *  moment — never on landing. */
function PushOptIn() {
  // W10-#8: "on" exposes the unsubscribe toggle — opting in was one tap,
  // opting out used to be impossible from the UI.
  const [state, setState] = useState<"hidden" | "ask" | "done" | "on">("hidden");
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    if (!pushSupported()) return;
    void hasSubscription()
      .then((subbed) => setState(subbed ? "on" : "ask"))
      .catch(() => setState("hidden"));
  }, []);
  if (state === "on") {
    return (
      <div
        data-testid="push-on"
        className="mb-s3 flex flex-wrap items-center gap-s3 rounded-md border border-hairline bg-bg-module px-s3 py-s2"
      >
        <span className="min-w-[220px] flex-1 font-mono text-[11.5px] text-ink-faint">
          push on — approvals reach your phone
        </span>
        <Button
          variant="ghost"
          size="sm"
          className="font-mono"
          disabled={busy}
          onClick={() => {
            setBusy(true);
            void unsubscribeFromPush()
              .then((ok) => setState(ok ? "hidden" : "on"))
              .catch(() => setState("on"))
              .finally(() => setBusy(false));
          }}
        >
          {busy ? "disabling…" : "disable"}
        </Button>
      </div>
    );
  }
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
        disabled={busy}
        onClick={() => {
          setBusy(true);
          // W10-#5: a thrown subscribe (offline, denied permission) used to
          // reject uncaught and stick the button. Any failure hides the ask.
          void subscribeToPush()
            .then((ok) => setState(ok ? "done" : "hidden"))
            .catch(() => setState("hidden"))
            .finally(() => setBusy(false));
        }}
      >
        {busy ? "enabling…" : "enable push"}
      </Button>
      <Button variant="ghost" size="sm" className="font-mono" onClick={() => setState("hidden")}>
        not now
      </Button>
    </div>
  );
}

/** Minutes left before the worker denies on its own — the card is not an open
 *  invitation, so say so rather than letting it silently stop working. Takes
 *  `now` so a ticking caller re-renders the countdown instead of leaving it
 *  frozen at the last poll (W4-M2). */
function expiryLabel(expiresAt: string | null | undefined, now: number): string | null {
  if (!expiresAt) return null;
  const ms = parseIso(expiresAt) - now;
  if (Number.isNaN(ms)) return null;
  if (ms <= 0) return "expired — denied";
  const mins = Math.ceil(ms / 60_000);
  return `auto-denies in ${mins}m`;
}

/** W-H4 / W4-L2: can this card be ALWAYS-allowed? The engine marks cards
 *  `always_allowable`; cards that predate the field fall back to the
 *  `destructive` flag; a card missing BOTH is a legacy unknown and must be
 *  treated as destructive (never auto-allowed) rather than silently
 *  offerable for blanket trust. */
function alwaysAllowable(payload: Record<string, unknown>): boolean {
  if (typeof payload.always_allowable === "boolean") return payload.always_allowable;
  if (typeof payload.destructive === "boolean") return !payload.destructive;
  return false; // legacy card, unknown blast radius
}

/** The shell command a card asks about, whichever payload shape it arrived
 *  in (engine {args.command}, legacy {command} / {cmd}). */
export function cardCommand(payload: Record<string, unknown>): string | null {
  const args = (payload.args ?? {}) as Record<string, unknown>;
  return typeof args.command === "string" ? args.command
    : typeof payload.command === "string" ? payload.command
    : typeof payload.cmd === "string" ? payload.cmd
    : null;
}

/** The thing awaiting a decision, rendered clean: a shell command gets the
 *  terminal treatment (no `{"command": …}` wire dump), a verbatim preview
 *  shows as-is, anything else falls back to pretty JSON. Handles both the
 *  engine payload ({tool, args, preview}) and legacy {cmd} cards. */
function ApprovalPayload({ payload }: { payload: Record<string, unknown> }) {
  const command = cardCommand(payload);
  if (command) {
    return (
      <div data-testid="approval-command">
        <CodeView code={command} lang="bash" />
      </div>
    );
  }
  if (typeof payload.preview === "string" && payload.preview) {
    return (
      <pre className="max-h-[140px] overflow-auto rounded-md bg-jack p-s3 font-mono text-[11px] leading-[1.5] whitespace-pre-wrap break-words text-ink-primary">
        {payload.preview}
      </pre>
    );
  }
  return (
    <pre className="max-h-[140px] overflow-auto rounded-md bg-jack p-s3 font-mono text-[11px] leading-[1.5] text-ink-primary">
      {JSON.stringify(payload, null, 2).slice(0, 600)}
    </pre>
  );
}

type Decision = "allow_once" | "always_allow" | "edited_allow" | "deny";

interface DecideVars {
  id: string;
  decision: Decision;
  editedArgs?: Record<string, unknown>;
}

/** One card — extracted so the edit affordance (W-H6) holds per-card state. */
function ApprovalCard({
  a,
  focused,
  now,
  deciding,
  onDecide,
}: {
  a: Approval;
  focused: boolean;
  now: number;
  deciding: boolean;
  onDecide: (v: DecideVars) => void;
}) {
  const payload = a.payload ?? {};
  const command = cardCommand(payload);
  const [editing, setEditing] = useState(false);
  const [editedCommand, setEditedCommand] = useState("");
  const canAlwaysAllow = alwaysAllowable(payload);

  return (
    <article
      id={`approval-${a.id}`}
      className={`mb-s2 rounded-md border bg-bg-panel p-s3 ${
        focused ? "border-blue ring-1 ring-blue/40" : "border-hairline"
      }`}
    >
      <div className="mb-s2 flex items-center justify-between">
        <span className="font-mono text-[12.5px] font-semibold text-blue-bright">
          {a.kind}
          {(payload.destructive === true || !canAlwaysAllow) && (
            <span className="ml-s2 text-danger-bright">destructive</span>
          )}
        </span>
        <span className="font-mono text-[10.5px] text-ink-faint">
          {a.thread_id ? `thread ${a.thread_id.slice(0, 8)}` : "run"}
          {expiryLabel(a.expires_at, now) ? ` · ${expiryLabel(a.expires_at, now)}` : ""}
        </span>
      </div>
      {editing && command !== null ? (
        <textarea
          value={editedCommand}
          onChange={(e) => setEditedCommand(e.target.value)}
          aria-label="edited command"
          rows={Math.min(6, editedCommand.split("\n").length + 1)}
          className="w-full rounded-md bg-jack p-s3 font-mono text-[11px] leading-[1.5] text-ink-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue"
        />
      ) : (
        <ApprovalPayload payload={payload} />
      )}
      <div className="mt-s3 flex flex-wrap items-center gap-s2">
        {editing ? (
          <>
            <Button
              size="sm"
              className="font-mono"
              disabled={deciding || !editedCommand.trim() || editedCommand.trim() === command?.trim()}
              onClick={() => onDecide({ id: a.id, decision: "edited_allow", editedArgs: { command: editedCommand } })}
            >
              {deciding ? "sending…" : "allow edited"}
            </Button>
            <Button variant="ghost" size="sm" className="font-mono" disabled={deciding} onClick={() => setEditing(false)}>
              cancel
            </Button>
          </>
        ) : (
          <>
            <Button
              size="sm"
              className="font-mono"
              disabled={deciding}
              onClick={() => onDecide({ id: a.id, decision: "allow_once" })}
            >
              allow once
            </Button>
            {canAlwaysAllow ? (
              <Button
                variant="outline"
                size="sm"
                className="font-mono"
                disabled={deciding}
                onClick={() => onDecide({ id: a.id, decision: "always_allow" })}
              >
                always allow
              </Button>
            ) : (
              <span className="font-mono text-[10.5px] text-ink-faint" title="This action is destructive — the engine never auto-allows it">
                never auto-allowed
              </span>
            )}
            {/* W-H6: the safety valve — let the human trim a risky command
                instead of an all-or-nothing allow/deny. */}
            {command !== null && (
              <Button
                variant="outline"
                size="sm"
                className="font-mono"
                disabled={deciding}
                onClick={() => {
                  setEditedCommand(command);
                  setEditing(true);
                }}
              >
                edit
              </Button>
            )}
            <Button
              variant="destructive"
              size="sm"
              className="font-mono"
              disabled={deciding}
              onClick={() => onDecide({ id: a.id, decision: "deny" })}
            >
              deny
            </Button>
          </>
        )}
      </div>
    </article>
  );
}

/** Tool-permission asks for the open run, docked in the session above the
 *  composer: gated autonomy stops the thread until you allow or deny, so the
 *  decision belongs where you are already watching. Optimistic w/ rollback. */
export function ApprovalQueue({ runId, focusCardId }: { runId: string; focusCardId?: string | null }) {
  const qc = useQueryClient();
  const { data: approvals = [] } = useQuery({
    queryKey: [...qk.approvals, runId],
    queryFn: () => api.get<Approval[]>(`/approvals?run_id=${encodeURIComponent(runId)}`),
    refetchInterval: 15_000,
  });

  // W-B5: a push-notification deep link (?run=…&card=…) scrolls the target
  // card into view once the approvals fetch lands (the card may not exist
  // on first render — it polls at 15s, so wait for it to APPEAR).
  useEffect(() => {
    if (!focusCardId) return;
    if (!approvals.some((a) => a.id === focusCardId)) return;
    document.getElementById(`approval-${focusCardId}`)?.scrollIntoView({ behavior: "smooth", block: "center" });
  }, [focusCardId, approvals]);

  // W4-M2: the countdown must TICK between polls — a card fetched with 2m
  // left used to read "auto-denies in 2m" for a quarter hour.
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 15_000);
    return () => clearInterval(t);
  }, []);

  const key = [...qk.approvals, runId];
  const decide = useMutation({
    mutationFn: ({ id, decision, editedArgs }: DecideVars) =>
      api.post<{ ok: boolean; decision?: string }>(`/approvals/${id}/decide`, {
        decision,
        ...(editedArgs ? { edited_args: editedArgs } : {}),
      }),
    onMutate: async ({ id }) => {
      await qc.cancelQueries({ queryKey: key });
      const prev = qc.getQueryData<Approval[]>(key);
      qc.setQueryData(key, (prev ?? []).filter((a) => a.id !== id));
      return { prev };
    },
    onSuccess: (data, vars) => {
      // W4-M2: the API echoes the RECORDED decision — a stale card decided
      // after the worker's BLPOP gave up comes back "timeout", not what was
      // clicked. Say so, or the human believes they allowed something dead.
      if (data?.decision && data.decision !== vars.decision) {
        toast.warning(`card already settled — recorded as "${data.decision}"`, {
          description: data.decision === "timeout"
            ? "the worker stopped waiting and denied on its own"
            : undefined,
        });
      }
    },
    onError: (err, vars, ctx) => {
      // H-59: don't blindly restore the stale `prev` snapshot — approvals
      // that arrived during the failed mutation (refetch/WS) would be
      // clobbered and vanish until the next refetch. Re-insert only the
      // failed approval into the CURRENT cache, preserving any new ones.
      const cur = qc.getQueryData<Approval[]>(key) ?? [];
      const failed = (ctx?.prev ?? []).find((a) => a.id === vars.id);
      if (failed && !cur.some((a) => a.id === vars.id)) {
        qc.setQueryData(key, [...cur, failed]);
      }
      // W4-L1: a 409 here is the cross-device race — someone (you, on
      // another screen) already decided this card. Explain, don't dead-end.
      if (err instanceof ApiError && err.status === 409) {
        toast.warning("already decided elsewhere", { description: err.message });
      } else {
        toast.error("decision failed", { description: err instanceof Error ? err.message : undefined });
      }
    },
    onSettled: () => void qc.invalidateQueries({ queryKey: key }),
  });

  // W4-M3: knowledge-draft cards route to the knowledge inbox — they never
  // sat on the worker's tool BLPOP, so deciding them HERE answered nothing.
  const cards = approvals.filter((a) => a.kind !== "knowledge");
  if (cards.length === 0) return null;

  return (
    <div
      data-testid="approval-queue"
      className="border-t border-warn/40 bg-warn-soft px-s4 py-s3"
      role="region"
      aria-label="approvals waiting on you"
    >
      <div className="text-micro mb-s2 text-warn-bright">waiting on you</div>
      <PushOptIn />
      {cards.map((a) => (
        <ApprovalCard
          key={a.id}
          a={a}
          focused={focusCardId === a.id}
          now={now}
          deciding={decide.isPending && decide.variables?.id === a.id}
          onDecide={(v) => decide.mutate(v)}
        />
      ))}
    </div>
  );
}
