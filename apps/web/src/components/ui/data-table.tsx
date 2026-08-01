import type { ReactNode } from "react";
import { cn } from "@/lib/utils";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Skeleton } from "@/components/ui/skeleton";

export interface Column<T> {
  key: string;
  header: ReactNode;
  numeric?: boolean;
  className?: string;
  render: (row: T) => ReactNode;
}

/** Stripe-density ledger table: 36px rows, sticky mono-caps header,
 *  right-aligned tabular numerics, skeleton rows that match the shape. */
export function DataTable<T>({
  columns,
  rows,
  rowKey,
  loading = false,
  skeletonRows = 6,
  empty,
  onRowClick,
  rowTestId,
  className,
}: {
  columns: Column<T>[];
  rows: T[];
  rowKey: (row: T) => string | number;
  loading?: boolean;
  skeletonRows?: number;
  empty?: ReactNode;
  onRowClick?: (row: T) => void;
  rowTestId?: (row: T) => string;
  className?: string;
}) {
  if (!loading && rows.length === 0 && empty) return <>{empty}</>;

  return (
    <div className={cn("rounded-lg border border-hairline bg-bg-panel shadow-card", className)}>
      <Table>
        <TableHeader>
          <TableRow className="border-b border-hairline hover:bg-transparent">
            {columns.map((c) => (
              <TableHead
                key={c.key}
                className={cn(
                  "sticky top-0 z-sticky h-auto bg-bg-panel px-s3 pb-s2 pt-s3 text-micro text-ink-faint",
                  c.numeric && "text-right",
                  c.className,
                )}
              >
                {c.header}
              </TableHead>
            ))}
          </TableRow>
        </TableHeader>
        <TableBody>
          {loading
            ? Array.from({ length: skeletonRows }, (_, i) => (
                <TableRow key={`sk-${i}`} className="border-b border-hairline/45 hover:bg-transparent">
                  {columns.map((c) => (
                    <TableCell key={c.key} className="h-row-md px-s3">
                      <Skeleton className="h-3.5 w-full max-w-[140px] rounded-sm" />
                    </TableCell>
                  ))}
                </TableRow>
              ))
            : rows.map((r) => (
                <TableRow
                  key={rowKey(r)}
                  data-testid={rowTestId?.(r)}
                  onClick={onRowClick ? () => onRowClick(r) : undefined}
                  className={cn(
                    "border-b border-hairline/45 last:border-0 hover:bg-bg-module/55",
                    onRowClick && "cursor-pointer",
                  )}
                >
                  {columns.map((c) => (
                    <TableCell
                      key={c.key}
                      className={cn(
                        "h-row-md px-s3",
                        c.numeric && "text-right font-mono tabular",
                        c.className,
                      )}
                    >
                      {c.render(r)}
                    </TableCell>
                  ))}
                </TableRow>
              ))}
        </TableBody>
      </Table>
    </div>
  );
}
