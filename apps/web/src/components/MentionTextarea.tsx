import { useEffect, useMemo, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";

import { Textarea } from "@/components/ui/textarea";
import { api } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import { cn } from "@/lib/utils";

interface Repo {
  id: number;
  name: string;
  integration_branch: string;
}

const DROPDOWN_CAP = 8;

/** Detect the @mention token at the caret in a textarea.
 *
 * A token is the text after the last unescaped `@` on the current line, up to
 * the caret, that contains no whitespace. Returns null when the caret is not
 * immediately after such a token. The returned range is the half-open
 * [start, end) span of the token (including the `@`) in the value. */
function tokenAtCaret(
  value: string,
  caret: number,
): { start: number; end: number; query: string } | null {
  // Walk back from the caret to the nearest `@` on this line.
  let i = caret;
  while (i > 0) {
    const ch = value[i - 1];
    if (ch === "@") {
      const start = i - 1;
      const query = value.slice(start, caret);
      // An empty query (just `@`) opens the dropdown with the full fleet.
      // A query with whitespace means the `@` was prose, not a mention.
      if (/\s/.test(query)) return null;
      return { start, end: caret, query };
    }
    if (/\s/.test(ch)) return null; // newline/space ends the search
    i -= 1;
  }
  return null;
}

/** A textarea with @mention autocomplete for repo scoping.
 *
 * Wraps the shared `Textarea`; tracks the `@token` at the caret and shows a
 * dropdown of repos from `GET /repos`, substring-filtered. ArrowUp/Down
 * navigate, Enter/Tab/click selects (replaces the token with `` `@Name` `` +
 * space, the backtick-wrapped form the backend's mention parser recognizes),
 * Esc closes. When the dropdown is closed, Enter keeps the parent's existing
 * send behavior (the parent's `onKeyDown` runs unchanged). */
export function MentionTextarea({
  value,
  onChange,
  onKeyDown,
  ...props
}: React.ComponentProps<"textarea"> & {
  value: string;
  onChange: (e: React.ChangeEvent<HTMLTextAreaElement>) => void;
}) {
  const ref = useRef<HTMLTextAreaElement | null>(null);
  const [token, setToken] = useState<{ start: number; end: number; query: string } | null>(null);
  const [active, setActive] = useState(0); // highlighted index in the dropdown
  // The last query text resync saw — Arrow keys resync without changing the
  // query, so resetting `active` unconditionally snapped the highlight back
  // to the top on every ArrowDown keyUP (keydown moved it, keyup undid it).
  const lastQuery = useRef<string | null>(null);

  const { data: repos = [] } = useQuery({
    queryKey: qk.repos,
    queryFn: () => api.get<Repo[]>("/repos").catch(() => [] as Repo[]),
  });

  // The filtered, capped list the dropdown shows for the current token.
  const matches = useMemo(() => {
    if (!token) return [];
    const q = token.query.slice(1).toLowerCase(); // drop the leading `@`
    const filtered = repos
      .filter((r) => r.name.toLowerCase().includes(q))
      .slice(0, DROPDOWN_CAP);
    return filtered;
  }, [token, repos]);

  // Re-sync the token whenever the value changes — including the initial
  // mount and any parent-driven update (e.g. a chip insertion). User input
  // also triggers resync via handleChange, but a programmatic value change
  // (the parent sets state) wouldn't fire onChange, so the dropdown would
  // never open for a value that arrives already containing an @token.
  useEffect(() => {
    resync();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [value]);

  // Re-sync the token whenever the value or caret moves. The parent owns the
  // value (controlled), so we re-derive on every change — cheap and correct.
  function resync() {
    const el = ref.current;
    if (!el) return;
    const t = tokenAtCaret(el.value, el.selectionStart ?? 0);
    setToken(t);
    const q = t?.query ?? null;
    if (q !== lastQuery.current) {
      lastQuery.current = q;
      if (t) setActive(0); // new query text = fresh list, highlight the first
    }
  }

  // Close the dropdown when the textarea loses focus (click-outside-friendly).
  function handleBlur() {
    // Defer so a click on a dropdown row fires before we tear it down.
    setTimeout(() => setToken(null), 120);
  }

  function selectRepo(repo: Repo) {
    const el = ref.current;
    if (!el || !token) return;
    const before = el.value.slice(0, token.start);
    const after = el.value.slice(token.end);
    // Backtick-wrap the mention so the backend parser recognizes it as a
    // scope directive (bare @word in prose is conversation, not a mount).
    // The composer produces `@Name`; the parser's MENTION_RE matches it.
    const inserted = "`@" + repo.name + "` ";
    const next = before + inserted + after;
    // Drive the parent's controlled state through onChange, then park the
    // caret right after the inserted token + space.
    el.value = next;
    el.setSelectionRange(before.length + inserted.length, before.length + inserted.length);
    onChange({ target: el } as React.ChangeEvent<HTMLTextAreaElement>);
    setToken(null);
  }

  function handleChange(e: React.ChangeEvent<HTMLTextAreaElement>) {
    onChange(e);
    // Re-derive after the parent's state updates — the value the parent
    // stores is what we read back from the DOM node.
    requestAnimationFrame(resync);
  }

  function handleKeyDown(e: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (token && matches.length > 0) {
      if (e.key === "ArrowDown") {
        e.preventDefault();
        setActive((a) => (a + 1) % matches.length);
        return;
      }
      if (e.key === "ArrowUp") {
        e.preventDefault();
        setActive((a) => (a - 1 + matches.length) % matches.length);
        return;
      }
      if (e.key === "Enter" || e.key === "Tab") {
        e.preventDefault();
        selectRepo(matches[active]);
        return;
      }
      if (e.key === "Escape") {
        e.preventDefault();
        setToken(null);
        return;
      }
      // Any other key falls through to the parent's onKeyDown ONLY IF it
      // wouldn't mutate the token (e.g. Backspace inside the token closes
      // the dropdown and lets the parent handle it). Backspace is special:
      // it shrinks the token, so re-derive on the next tick.
      if (e.key === "Backspace") {
        requestAnimationFrame(resync);
      }
    }
    // When the dropdown is closed (or empty), the parent's onKeyDown runs
    // unchanged — Enter still submits, Shift+Enter still inserts a newline.
    if (onKeyDown) onKeyDown(e);
  }

  // Keep the highlighted index in range when the filtered set shrinks.
  useEffect(() => {
    if (active >= matches.length) setActive(0);
  }, [matches.length, active]);

  return (
    <div className="relative">
      <Textarea
        ref={ref}
        value={value}
        onChange={handleChange}
        onKeyDown={handleKeyDown}
        onBlur={handleBlur}
        onKeyUp={resync}
        onClick={resync}
        {...props}
      />
      {token && matches.length > 0 && (
        <div
          className="absolute bottom-full left-0 mb-s1 max-h-[200px] w-full max-w-md overflow-y-auto rounded-md border border-hairline bg-bg-panel py-s1 shadow-lg"
          role="listbox"
          aria-label="repo mentions"
        >
          {matches.map((r, i) => (
            <button
              key={r.id}
              type="button"
              role="option"
              aria-selected={i === active}
              onMouseDown={(e) => e.preventDefault()} // keep focus on the textarea
              onClick={() => selectRepo(r)}
              className={cn(
                "flex w-full items-center gap-s2 px-s3 py-s1 text-left font-mono text-[12px]",
                i === active ? "bg-bg-module text-ink-primary" : "text-ink-secondary",
              )}
            >
              <span className="text-ink-faint">@</span>
              <span className="truncate">{r.name}</span>
              <span className="ml-auto text-[10px] text-ink-faint">{r.integration_branch}</span>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
