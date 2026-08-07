/** Console parity: the card kinds that were missing — todo-checklist,
 *  compaction, ⚠ warning, ◆ recap — plus the dedicated approval card. Shared
 *  by the live EventStream and the Feed so both render LIVE StepEvents
 *  identically. Locked tokens only (bg-warn, blue-bright, hairline…). */

import { CodeView } from "../CodeView";

export interface TaskArtifact {
  id: string;
  content: string;
  scope?: string;
  acceptance?: string;
}

const BODY_TEXT = "font-mono text-[11.5px] leading-[1.5] text-ink-primary";

/** todo-checklist: the live update_tasks two-artifact state as checkboxes. */
export function TodoChecklist({ detail }: { detail: Record<string, unknown> }) {
  const tasks = (detail.tasks ?? {}) as { artifact?: TaskArtifact[]; tracker?: Record<string, string> };
  const artifact = tasks.artifact ?? [];
  const tracker = tasks.tracker ?? {};
  if (artifact.length === 0) return null;
  return (
    <ul className="mt-1 space-y-s1" data-testid="todo-checklist">
      {artifact.map((t) => {
        const status = tracker[t.id] ?? "pending";
        const mark = status === "completed" ? "☑" : status === "in_progress" ? "▸" : "☐";
        return (
          <li key={t.id} className="flex gap-s2 font-mono text-[11.5px] leading-[1.5]">
            <span className={status === "completed" ? "text-green-bright" : "text-ink-faint"}>{mark}</span>
            <span className={status === "completed" ? "text-ink-faint line-through" : "text-ink-primary"}>
              {t.content}
            </span>
          </li>
        );
      })}
    </ul>
  );
}

/** compaction card: what the compaction did, with token deltas. */
export function CompactionCard({ detail }: { detail: Record<string, unknown> }) {
  const num = (k: string) => (typeof detail[k] === "number" ? (detail[k] as number) : null);
  const parts = [
    num("pruned") != null && `pruned ${num("pruned")}`,
    num("summarized") != null && `summarized ${num("summarized")}`,
    num("kept") != null && `kept ${num("kept")}`,
  ].filter(Boolean);
  const tokens =
    num("before_tokens") != null && num("after_tokens") != null
      ? `${num("before_tokens")} → ${num("after_tokens")} tokens`
      : null;
  return (
    <div className={`mt-1 ${BODY_TEXT}`} data-testid="compaction-card">
      {parts.join(" · ")}
      {tokens && <span className="text-ink-faint"> ({tokens})</span>}
      {detail.forced === true && <span className="text-ink-faint"> — forced</span>}
    </div>
  );
}

/** ⚠ warning: watchdog/critic/budget signals, amber-railed. */
export function WarningCard({ detail, title }: { detail: Record<string, unknown>; title: string }) {
  return (
    <div className={`mt-1 ${BODY_TEXT}`} data-testid="warning-card">
      <span className="text-warn">⚠</span> {String(detail.detail ?? title)}
    </div>
  );
}

/** ◆ recap block: goal-stage progress recaps. */
export function RecapCard({ detail }: { detail: Record<string, unknown> }) {
  return (
    <div
      className="mt-1 rounded-md border border-hairline bg-bg-module px-s3 py-s2 font-mono text-[11.5px] leading-[1.5] text-ink-primary"
      data-testid="recap-card"
    >
      <span className="text-blue-bright">◆</span> {String(detail.summary ?? "")}
      {Array.isArray(detail.blockers) && detail.blockers.length > 0 && (
        <div className="mt-s1 text-warn">blockers: {(detail.blockers as string[]).join("; ")}</div>
      )}
    </div>
  );
}

/** What the human is actually approving: a shell command gets the terminal
 *  treatment, a file edit/write gets its verbatim preview, anything else gets
 *  pretty-printed JSON — never the compact `{"command": …}` wire dump.
 *  `cmd` rides alongside `command`: models habitually emit other harnesses'
 *  key, and pre-normalization transcripts have it persisted in the payload. */
function ApprovalBody({ detail }: { detail: Record<string, unknown> }) {
  const args = (detail.args ?? {}) as Record<string, unknown>;
  const command = [args.command, args.cmd].find((v): v is string => typeof v === "string") ?? null;
  const preview = typeof detail.preview === "string" ? detail.preview : null;
  if (command) {
    return (
      <div className="mt-s1" data-testid="approval-command">
        <CodeView code={command} lang="bash" />
      </div>
    );
  }
  if (preview) {
    return (
      <pre className="mt-s1 whitespace-pre-wrap break-words font-mono text-[11.5px] text-ink-secondary">
        {preview}
      </pre>
    );
  }
  if (Object.keys(args).length > 0) {
    return (
      <div className="mt-s1">
        <CodeView code={JSON.stringify(args, null, 2)} lang="json" />
      </div>
    );
  }
  return null;
}

/** approval card / decision: VERBATIM tool + args, action_id pairing, and a
 *  destructive badge — the gate's one-line justification is never paraphrased. */
export function ApprovalCard({ detail, title }: { detail: Record<string, unknown>; title: string }) {
  const isDecision = detail.kind === "approval_decision";
  const verdict = typeof detail.decision === "string" ? detail.decision : null;
  return (
    <div
      className="mt-1 rounded-md border border-warn bg-bg-module px-s3 py-s2"
      data-testid="approval-card"
      data-action-id={String(detail.action_id ?? "")}
    >
      <div className="font-mono text-[12px] text-ink-primary">
        {isDecision ? (
          <span className={verdict === "deny" ? "text-danger-bright" : "text-green-bright"}>
            {verdict ?? title}
          </span>
        ) : (
          <span className="text-warn">approval requested</span>
        )}
        {!isDecision && typeof detail.tool === "string" && (
          <span className="ml-s2 text-ink-faint">{detail.tool}</span>
        )}
        {detail.destructive === true && <span className="ml-s2 text-danger-bright">destructive</span>}
        {detail.edited === true && <span className="ml-s2 text-ink-faint">edited</span>}
      </div>
      {!isDecision && <ApprovalBody detail={detail} />}
      {isDecision && detail.reason != null && (
        <div className="mt-s1 font-mono text-[11.5px] text-ink-faint">{String(detail.reason)}</div>
      )}
    </div>
  );
}
