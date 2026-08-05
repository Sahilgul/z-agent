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

interface IdeaSummary {
  consensus: string;
  disagreements: string[];
  recommendation: string;
  open_questions: string[];
}

interface IdeaComment {
  id: number;
  author_type: string;
  author_name: string;
  body: string;
}

interface IdeaThread {
  id: number;
  title: string;
  status: string;
  comment_count: number;
  summary: IdeaSummary | null;
}

interface IdeaDetail extends IdeaThread {
  comments: IdeaComment[];
}

/** Deliberation: idea threads where the voices argue. The question sits on
 *  a raised row; opening it fetches the record — lead synthesis pinned
 *  above the raw voices, counsel wearing the 11th-member badge. */
export function IdeasScreen() {
  const qc = useQueryClient();
  const [openId, setOpenId] = useState<number | null>(null);
  const [comment, setComment] = useState("");

  const { data: threads = [], isLoading } = useQuery({
    queryKey: qk.ideas,
    queryFn: () => api.get<IdeaThread[]>("/ideas"),
  });

  const { data: detail } = useQuery({
    queryKey: qk.ideaThread(String(openId)),
    queryFn: () => api.get<IdeaDetail>(`/ideas/${openId}`),
    enabled: openId !== null,
  });

  const send = useMutation({
    mutationFn: ({ id, kind }: { id: number; kind: "ask-counsel" | "promote" | "comment" }) =>
      // The comment route is plural (/comments); the other two match their kind.
      api.post(
        `/ideas/${id}/${kind === "comment" ? "comments" : kind}`,
        kind === "comment" ? { body: comment } : {},
      ),
    // M-84: only clear the comment on SUCCESS — the old onSettled ran on
    // error too, so a failed POST silently wiped the user's text. Keep the
    // invalidation in onSettled (refreshing on success or error is harmless)
    // and surface the failure so the user knows their text was NOT sent.
    onSuccess: () => setComment(""),
    onError: (err) => {
      toast.error("comment failed", {
        description: err instanceof Error ? err.message : undefined,
      });
    },
    onSettled: () => {
      void qc.invalidateQueries({ queryKey: qk.ideas });
      if (openId !== null) void qc.invalidateQueries({ queryKey: qk.ideaThread(String(openId)) });
    },
  });

  return (
    <div className="mx-auto h-full max-w-[860px] overflow-y-auto px-s8 py-s6">
      <PageHead title="deliberation" sub="ideas the voices argue before anyone commits" />

      {isLoading ? (
        <div className="flex flex-col gap-s2" aria-label="loading ideas">
          {[0, 1, 2].map((i) => (
            <Skeleton key={i} className="h-12 w-full rounded-lg" />
          ))}
        </div>
      ) : threads.length === 0 ? (
        <EmptyState hint="no open threads" />
      ) : (
        threads.map((t) => (
          <div key={t.id} className="mb-s2">
            <button
              type="button"
              onClick={() => setOpenId(openId === t.id ? null : t.id)}
              className="flex w-full flex-wrap items-center justify-between gap-s2 rounded-lg border border-hairline bg-bg-panel px-s4 py-s3 text-left shadow-card transition-colors duration-fast ease-default hover:border-hairline-bright"
            >
              <span className="text-[13px] font-medium text-ink-primary">{t.title}</span>
              <span className="font-mono text-[10.5px] text-ink-faint">
                {t.comment_count} voices · {t.status}
              </span>
            </button>

            {openId === t.id && detail && (
              <div className="mt-s1 rounded-lg border border-hairline bg-bg-panel p-s4 shadow-card animate-enter">
                {detail.summary && (
                  <div className="mb-s3 border-l-[3px] border-blue bg-bg-raised px-s3 py-s2">
                    <div className="mb-s1 font-mono text-[10px] uppercase tracking-[0.07em] text-ink-faint">
                      lead synthesis · all voices
                    </div>
                    <div className="text-[12.5px] leading-[1.55] text-ink-primary">{detail.summary.consensus}</div>
                    <Badge className="mt-s2 rounded-full border-transparent bg-ok-soft px-2.5 py-0.5 font-mono text-[10px] font-normal text-ok-bright">
                      {detail.summary.recommendation}
                    </Badge>
                  </div>
                )}
                {detail.comments.map((c) =>
                  c.author_type === "agent" ? (
                    <div key={c.id} className="mb-s2 border-l-[3px] border-blue-soft px-s3 py-s2">
                      <div className="mb-s1 font-mono text-[10px] uppercase tracking-[0.07em] text-ink-faint">
                        counsel · 11th member
                      </div>
                      <div className="text-[12.5px] leading-[1.55] text-ink-primary">{c.body}</div>
                    </div>
                  ) : (
                    <div key={c.id} className="mb-s2 border-l-[3px] border-hairline px-s3 py-s2">
                      <div className="mb-s1 font-mono text-[10px] uppercase tracking-[0.07em] text-ink-faint">
                        {c.author_name}
                      </div>
                      <div className="text-[12.5px] leading-[1.55] text-ink-primary">{c.body}</div>
                    </div>
                  )
                )}
                <div className="mt-s3 flex flex-wrap gap-s2">
                  <Button
                    variant="secondary"
                    size="sm"
                    className="font-mono"
                    onClick={() => send.mutate({ id: t.id, kind: "ask-counsel" })}
                  >
                    ask counsel
                  </Button>
                  <Button size="sm" className="font-mono" onClick={() => send.mutate({ id: t.id, kind: "promote" })}>
                    promote to plan
                  </Button>
                </div>
                <div className="mt-s3 flex gap-s2">
                  <input
                    value={comment}
                    onChange={(e) => setComment(e.target.value)}
                    placeholder="add a human note"
                    aria-label="comment"
                    className="h-8 flex-1 rounded-md border border-hairline bg-bg-raised px-s3 text-[13px] text-ink-primary placeholder:text-ink-faint focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue focus-visible:ring-offset-1 focus-visible:ring-offset-jack"
                  />
                  <Button
                    variant="secondary"
                    size="sm"
                    className="font-mono"
                    disabled={!comment.trim()}
                    onClick={() => send.mutate({ id: t.id, kind: "comment" })}
                  >
                    comment
                  </Button>
                </div>
              </div>
            )}
          </div>
        ))
      )}
    </div>
  );
}
