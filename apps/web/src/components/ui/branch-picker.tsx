import { useEffect, useMemo, useRef, useState } from "react";

/** Branch combobox: type to filter, click or Enter to pick.
 *
 *  A native <select> collapses under real ADO repos — hundreds of branches with
 *  no way to search means scrolling past ticket-number branches to find `main`.
 *  The value is still constrained to what the remote reported; typing filters,
 *  it never invents a branch that can't be cloned. */
export function BranchPicker({
  branches,
  value,
  onChange,
  id,
  className = "",
}: {
  branches: string[];
  value: string;
  onChange: (branch: string) => void;
  id?: string;
  className?: string;
}) {
  const [query, setQuery] = useState(value);
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(0);
  const box = useRef<HTMLDivElement>(null);

  useEffect(() => setQuery(value), [value]);

  useEffect(() => {
    function onDocClick(e: MouseEvent) {
      if (box.current && !box.current.contains(e.target as Node)) {
        setOpen(false);
        setQuery(value);
      }
    }
    document.addEventListener("mousedown", onDocClick);
    return () => document.removeEventListener("mousedown", onDocClick);
  }, [value]);

  const matches = useMemo(() => {
    const sorted = [...branches].sort((a, b) => a.localeCompare(b));
    const q = query.trim().toLowerCase();
    // M-76: the old `q === value.toLowerCase()` short-circuit disabled
    // filtering when the user typed the EXACT selected value (showed all
    // branches instead of filtered). Only skip filtering when the query
    // is the default display (value) AND the menu is closed (not actively
    // filtering); once open, filter by the query even if it matches value.
    if (!q) return sorted;
    if (q === value.toLowerCase() && !open) return sorted;
    return sorted.filter((b) => b.toLowerCase().includes(q));
  }, [branches, query, value, open]);

  function pick(branch: string) {
    onChange(branch);
    setQuery(branch);
    setOpen(false);
  }

  return (
    <div ref={box} className={`relative ${className}`}>
      <input
        id={id}
        role="combobox"
        aria-expanded={open}
        // L-37: the listbox node (#branch-picker-list) only renders while
        // `open`, so a static aria-controls pointed at a non-existent node
        // when closed — screen readers following the control hit a broken
        // reference. Only advertise the control when the listbox is mounted.
        aria-controls={open ? "branch-picker-list" : undefined}
        autoComplete="off"
        value={query}
        placeholder="type to filter branches"
        onFocus={() => setOpen(true)}
        onChange={(e) => {
          setQuery(e.target.value);
          setActive(0);
          setOpen(true);
        }}
        onKeyDown={(e) => {
          if (e.key === "ArrowDown") {
            e.preventDefault();
            setOpen(true);
            // L-35: on empty matches `matches.length - 1` is -1, so
            // Math.min(i + 1, -1) clamped active to -1 (and matches[-1]
            // then resolves to the last element in JS — wrong highlight).
            // When there's nothing to highlight, hold active at 0.
            setActive((i) =>
              matches.length === 0 ? 0 : Math.min(i + 1, matches.length - 1),
            );
          } else if (e.key === "ArrowUp") {
            e.preventDefault();
            setActive((i) => Math.max(i - 1, 0));
          } else if (e.key === "Enter" && open && matches[active]) {
            e.preventDefault();
            pick(matches[active]);
          } else if (e.key === "Escape") {
            setOpen(false);
            setQuery(value);
          }
        }}
        className="h-8 w-full rounded-md border border-hairline bg-bg-raised px-s3 text-[13px] text-ink-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue focus-visible:ring-offset-1 focus-visible:ring-offset-jack"
      />
      {open && (
        <ul
          id="branch-picker-list"
          role="listbox"
          className="absolute z-20 mt-s1 max-h-[280px] w-full overflow-y-auto rounded-md border border-hairline bg-bg-panel py-s1 shadow-card"
        >
          {matches.length === 0 && (
            <li className="px-s3 py-s2 font-mono text-[11px] text-ink-faint">no branch matches</li>
          )}
          {matches.map((b, i) => (
            <li key={b}>
              <button
                type="button"
                role="option"
                aria-selected={b === value}
                onMouseEnter={() => setActive(i)}
                onClick={() => pick(b)}
                className={`block w-full truncate px-s3 py-s1 text-left font-mono text-[12px] ${
                  i === active ? "bg-bg-raised text-ink-primary" : "text-ink-secondary"
                } ${b === value ? "font-semibold" : ""}`}
              >
                {b}
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
