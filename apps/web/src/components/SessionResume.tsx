import { useQuery } from "@tanstack/react-query";
import { DownloadIcon, PlayIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { api } from "../lib/api";
import { qk } from "../lib/queryKeys";
import type { ResumableLane, Run } from "../types";

/** Continue a past session. The backend keeps each lane's SDK session volume
 *  for 30 days; after that the run is replay-only, so the button appears only
 *  while `resumable` is true. Resume restores the CONVERSATION — un-pushed
 *  file changes are gone by design (workspaces are shredded). */
export function SessionResume({
  run,
  working,
  onResumed,
}: {
  run: Run;
  working: boolean;
  onResumed: (runId: string) => void;
}) {
  const { data } = useQuery({
    queryKey: qk.resumable(run.id),
    queryFn: () => api.get<{ lanes: ResumableLane[] }>(`/sessions/${run.id}/resumable`),
    enabled: !working,
    retry: false,
  });

  const resumable = (data?.lanes ?? []).some((l) => l.resumable);

  const resume = async () => {
    const res = await api.post<{ run_id: string }>(`/sessions/${run.id}/resume`, {});
    onResumed(res.run_id);
  };

  return (
    <div className="flex flex-wrap items-center gap-s2 border-t border-hairline px-s4 py-2.5">
      {!working &&
        (resumable ? (
          <Button size="sm" className="font-mono" onClick={() => void resume()}>
            <PlayIcon aria-hidden="true" />
            continue session
          </Button>
        ) : (
          <span className="font-mono text-[11px] text-ink-faint">
            replay-only — the session volume has expired
          </span>
        ))}
      <a
        href={`/sessions/${run.id}/transcript`}
        download={`${run.id}.jsonl`}
        className="ml-auto inline-flex items-center gap-s2 rounded-md px-s2 py-1 font-mono text-[11px] text-ink-faint transition-colors duration-fast hover:text-ink-primary"
      >
        <DownloadIcon className="size-3.5" aria-hidden="true" />
        transcript.jsonl
      </a>
    </div>
  );
}
