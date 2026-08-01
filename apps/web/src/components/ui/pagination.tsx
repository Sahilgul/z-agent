import { ChevronLeftIcon, ChevronRightIcon } from "lucide-react";
import { Button } from "@/components/ui/button";

/** Server-side-feeling pagination chrome — prev/next + mono page readout. */
export function Pagination({
  page,
  pages,
  onPage,
  className,
}: {
  page: number;
  pages: number;
  onPage: (page: number) => void;
  className?: string;
}) {
  if (pages <= 1) return null;
  return (
    <div className={className ?? "mt-s3 flex items-center justify-end gap-s2"}>
      <Button
        variant="outline"
        size="icon-sm"
        title="previous page"
        aria-label="previous page"
        disabled={page <= 1}
        onClick={() => onPage(page - 1)}
      >
        <ChevronLeftIcon />
      </Button>
      <span className="font-mono text-[11.5px] tabular text-ink-faint">
        {page} / {pages}
      </span>
      <Button
        variant="outline"
        size="icon-sm"
        title="next page"
        aria-label="next page"
        disabled={page >= pages}
        onClick={() => onPage(page + 1)}
      >
        <ChevronRightIcon />
      </Button>
    </div>
  );
}
