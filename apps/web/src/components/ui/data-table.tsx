import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import {
  flexRender,
  getCoreRowModel,
  getSortedRowModel,
  useReactTable,
  type ColumnDef,
  type SortingState,
  type Table as TanstackTable,
  type VisibilityState,
} from "@tanstack/react-table";
import { ArrowDownIcon, ArrowUpIcon, ChevronDownIcon, ChevronsUpDownIcon } from "lucide-react";
import { cn } from "@/lib/utils";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Skeleton } from "@/components/ui/skeleton";
import { Button } from "@/components/ui/button";

export interface Column<T> {
  key: string;
  header: ReactNode;
  numeric?: boolean;
  sortable?: boolean;
  className?: string;
  /** Accessor for sorting. Falls back to the rendered string if absent. */
  sortAccessor?: (row: T) => string | number | null;
  render: (row: T) => ReactNode;
}

/** Stripe-density ledger table on TanStack Table: 36px rows, sticky mono-caps
 *  header, right-aligned tabular numerics, skeleton rows that match the
 *  shape. Adds click-header sort (+ shift for multi-sort), a column-visibility
 *  menu, and optional row selection — the capabilities the review flagged as
 *  2015-era. Visual contract is unchanged; TanStack is headless so the markup
 *  stays ours. */
export function DataTable<T>({
  columns,
  rows,
  rowKey,
  loading = false,
  skeletonRows = 6,
  empty,
  onRowClick,
  rowTestId,
  enableColumnVisibility = false,
  enableRowSelection = false,
  onSelectionChange,
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
  enableColumnVisibility?: boolean;
  enableRowSelection?: boolean;
  onSelectionChange?: (selectedRows: T[]) => void;
  className?: string;
}) {
  const [sorting, setSorting] = useState<SortingState>([]);
  const [visibility, setVisibility] = useState<VisibilityState>({});

  const tableColumns = useMemo<ColumnDef<T>[]>(() => {
    const selectable: ColumnDef<T>[] = enableRowSelection
      ? [
          {
            id: "__select",
            header: ({ table }) => (
              <input
                type="checkbox"
                aria-label="select all rows"
                checked={table.getIsAllRowsSelected()}
                ref={(el) => {
                  if (el) el.indeterminate = table.getIsSomeRowsSelected();
                }}
                onChange={table.getToggleAllRowsSelectedHandler()}
                // M-74: stop the checkbox click from bubbling to the row
                // onClick (double action: toggle + row click).
                onClick={(e) => e.stopPropagation()}
                className="size-3.5 accent-[var(--color-green)]"
              />
            ),
            cell: ({ row }) => (
              <input
                type="checkbox"
                aria-label="select row"
                checked={row.getIsSelected()}
                onChange={row.getToggleSelectedHandler()}
                // M-74: stop the checkbox click from bubbling to the row
                // onClick (double action: toggle + row click).
                onClick={(e) => e.stopPropagation()}
                className="size-3.5 accent-[var(--color-green)]"
              />
            ),
            enableSorting: false,
            size: 28,
          },
        ]
      : [];
    return [
      ...selectable,
      ...columns.map<ColumnDef<T>>((c) => ({
        id: c.key,
        header: () => <>{c.header}</>,
        cell: ({ row }) => <>{c.render(row.original)}</>,
        enableSorting: c.sortable ?? false,
        accessorFn: (row) =>
          c.sortAccessor
            ? c.sortAccessor(row)
            : (() => {
                const rendered = c.render(row);
                return typeof rendered === "string" || typeof rendered === "number"
                  ? rendered
                  : null;
              })(),
        meta: { numeric: c.numeric, className: c.className },
      })),
    ];
  }, [columns, enableRowSelection]);

  const table = useReactTable({
    data: rows,
    columns: tableColumns,
    state: { sorting, columnVisibility: visibility },
    onSortingChange: setSorting,
    onColumnVisibilityChange: setVisibility,
    getCoreRowModel: getCoreRowModel(),
    getSortedRowModel: getSortedRowModel(),
    enableRowSelection,
    getRowId: (row) => String(rowKey(row)),
    enableMultiSort: true,
    enableSortingRemoval: true,
  });

  // Surface selection changes to the consumer as the underlying rows.
  const rowSelection = table.getState().rowSelection;
  // M-73: an unmemoized onSelectionChange (new ref every render) made the
  // effect below fire every render -> parent setState -> re-render -> loop.
  // Store the callback in a ref and drop it from the dep array so the
  // effect fires only on actual selection changes (rowSelection), not on
  // callback identity churn.
  const onSelectionChangeRef = useRef(onSelectionChange);
  onSelectionChangeRef.current = onSelectionChange;
  useEffect(() => {
    if (!enableRowSelection || !onSelectionChangeRef.current) return;
    onSelectionChangeRef.current(table.getSelectedRowModel().rows.map((r) => r.original));
  }, [rowSelection, enableRowSelection, table]);

  if (!loading && rows.length === 0 && empty) return <>{empty}</>;

  return (
    <div className={cn("rounded-lg border border-hairline bg-bg-panel shadow-card", className)}>
      {enableColumnVisibility && (
        <div className="flex justify-end border-b border-hairline px-s3 py-s2">
          <ColumnVisibilityMenu table={table as TanstackTable<unknown>} />
        </div>
      )}
      <Table>
        <TableHeader>
          <TableRow className="border-b border-hairline hover:bg-transparent">
            {table.getHeaderGroups()[0].headers.map((header) => {
              const col = header.column;
              const meta = col.columnDef.meta as { numeric?: boolean; className?: string } | undefined;
              const numeric = meta?.numeric;
              const sorted = col.getIsSorted();
              const sortable = col.getCanSort();
              return (
                <TableHead
                  key={col.id}
                  style={header.getSize ? { width: header.getSize() } : undefined}
                  className={cn(
                    "sticky top-0 z-sticky h-auto bg-bg-panel px-s3 pb-s2 pt-s3 text-micro text-ink-faint",
                    numeric && "text-right",
                    sortable && "cursor-pointer select-none hover:text-ink-secondary",
                    meta?.className,
                  )}
                  aria-sort={sorted === "asc" ? "ascending" : sorted === "desc" ? "descending" : undefined}
                  onClick={sortable ? col.getToggleSortingHandler() : undefined}
                  onKeyDown={
                    sortable
                      ? (e) => {
                          if (e.key === "Enter" || e.key === " ") {
                            e.preventDefault();
                            col.getToggleSortingHandler()?.(e as unknown as MouseEvent);
                          }
                        }
                      : undefined
                  }
                  tabIndex={sortable ? 0 : undefined}
                >
                  <span className={cn("inline-flex items-center gap-s1", numeric && "justify-end")}>
                    {flexRender(col.columnDef.header, header.getContext())}
                    {sortable && (
                      <span className="text-ink-ghost" aria-hidden="true">
                        {sorted === "asc" ? (
                          <ArrowUpIcon className="size-3" />
                        ) : sorted === "desc" ? (
                          <ArrowDownIcon className="size-3" />
                        ) : (
                          <ChevronsUpDownIcon className="size-3" />
                        )}
                      </span>
                    )}
                  </span>
                </TableHead>
              );
            })}
          </TableRow>
        </TableHeader>
        <TableBody>
          {loading ? (
            Array.from({ length: skeletonRows }, (_, i) => (
              <TableRow key={`sk-${i}`} className="border-b border-hairline/45 hover:bg-transparent">
                {table.getHeaderGroups()[0].headers.map((header) => (
                  <TableCell key={header.id} className="h-row-md px-s3">
                    <Skeleton className="h-3.5 w-full max-w-[140px] rounded-sm" />
                  </TableCell>
                ))}
              </TableRow>
            ))
          ) : (
            table.getRowModel().rows.map((row) => (
              <TableRow
                key={row.id}
                data-testid={rowTestId?.(row.original)}
                data-state={row.getIsSelected() ? "selected" : undefined}
                onClick={onRowClick ? () => onRowClick(row.original) : undefined}
                className={cn(
                  "border-b border-hairline/45 last:border-0 hover:bg-bg-module/55",
                  onRowClick && "cursor-pointer",
                  row.getIsSelected() && "bg-bg-module/40",
                )}
              >
                {row.getVisibleCells().map((cell) => {
                  const meta = cell.column.columnDef.meta as
                    | { numeric?: boolean; className?: string }
                    | undefined;
                  return (
                    <TableCell
                      key={cell.id}
                      className={cn(
                        "h-row-md px-s3",
                        meta?.numeric && "text-right font-mono tabular",
                        meta?.className,
                      )}
                    >
                      {flexRender(cell.column.columnDef.cell, cell.getContext())}
                    </TableCell>
                  );
                })}
              </TableRow>
            ))
          )}
        </TableBody>
      </Table>
    </div>
  );
}

function ColumnVisibilityMenu<T>({
  table,
}: {
  table: TanstackTable<T>;
}) {
  const [open, setOpen] = useState(false);
  const containerRef = useRef<HTMLDivElement>(null);
  const columns = table.getAllLeafColumns().filter((c) => c.id !== "__select");
  // M-75: close the menu on outside click or Escape — the old menu only
  // closed on toggling the button, so it stayed open over the table.
  useEffect(() => {
    if (!open) return;
    const onPointer = (e: MouseEvent) => {
      if (containerRef.current && !containerRef.current.contains(e.target as Node)) {
        setOpen(false);
      }
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", onPointer);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onPointer);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);
  if (columns.length === 0) return null;
  return (
    <div className="relative" ref={containerRef}>
      <Button
        type="button"
        variant="outline"
        size="sm"
        className="font-mono"
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
      >
        columns
        <ChevronDownIcon className="size-3" aria-hidden="true" />
      </Button>
      {open && (
        <div
          className="absolute right-0 top-full z-overlay mt-s1 w-40 rounded-md border border-hairline bg-bg-panel p-s2 shadow-pop"
          role="menu"
        >
          {columns.map((c) => (
            <label
              key={c.id}
              className="flex cursor-pointer items-center gap-s2 px-s2 py-s1 font-mono text-[11.5px] text-ink-secondary hover:bg-bg-module"
            >
              <input
                type="checkbox"
                checked={c.getIsVisible()}
                onChange={c.getToggleVisibilityHandler()}
                className="size-3.5 accent-[var(--color-green)]"
              />
              <span className="truncate">
                {flexRender(c.columnDef.header, {
                  table,
                  header: c.columnDef,
                  column: c,
                // M-75 (hardening): the column def's header is typed as
                // `object` (unknown renderable); flexRender's context type
                // is HeaderContext<T, unknown> which the loose cast didn't
                // satisfy. Cast to any to bridge the unknown header type.
                // eslint-disable-next-line @typescript-eslint/no-explicit-any
                } as any)}
              </span>
            </label>
          ))}
        </div>
      )}
    </div>
  );
}
