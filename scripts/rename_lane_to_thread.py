"""Phase 1 mechanical rename: lane → thread across backend/app (idempotent, word-boundary safe).

Run from repo root: python scripts/rename_lane_to_thread.py

Rules (applied in order, each with word boundaries to avoid substring collisions):
  1. LANE_ID        → THREAD_ID          (env var)
  2. lane_id        → thread_id          (field/column/channel)
  3. .lanes         → .threads           (ORM relationship attr)
  4. "lanes"        → "threads"           (table name string literals)
  5. stop_lane      → stop_thread         (ActionKind value)
  6. Lane(          → Thread(            (class instantiation)
  7. : Lane"        → : Thread"           (type annotations in strings)
  8. | Lane |       → | Thread |          (markdown tables)
  9. Lane\b         → Thread              (class name reference, word-boundary)
 10. lane:          → thread:             (Redis channel prefix)
  11. lane/         → thread/              (doc paths)
  12. 'lane'        → 'thread'             (string literals)
  13. "lane"        → "thread"             (string literals)

NOT touched: IdeaThread (already uses Thread), lane in other contexts that
don't match the above patterns. The script reports every file it changed
and a per-file change count; review the diff before committing.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOTS = [Path(__file__).resolve().parent.parent / "backend" / "app",
         Path(__file__).resolve().parent.parent / "backend" / "tests"]

# Ordered (pattern, replacement) pairs. Each uses explicit boundaries to
# avoid corrupting substrings (e.g. IdeaThread, lanes-prefixed env vars).
PATTERNS: list[tuple[str, str]] = [
    (r"\bLANE_ID\b", "THREAD_ID"),
    (r"\blane_id\b", "thread_id"),
    (r"\.lanes\b", ".threads"),
    (r'"lanes"', '"threads"'),
    (r"'lanes'", "'threads'"),
    (r"\bstop_lane\b", "stop_thread"),
    (r"\bLane\(", "Thread("),
    (r": Lane\b", ": Thread"),
    (r"\| Lane \|", "| Thread |"),
    # Class-name references with word boundary (won't hit IdeaThread, since that
    # has a capital letter before 'Thread'). Match Lane as a standalone word
    # followed by a non-identifier char.
    (r"\bLane\b(?![A-Za-z0-9_])", "Thread"),
    (r"\blane:", "thread:"),
    (r"\blane/", "thread/"),
    (r"'lane'", "'thread'"),
    (r'"lane"', '"thread"'),
    # Plural standalone word (docstrings, comments) — only when surrounded by
    # non-identifier chars to avoid hitting 'lanes' table-name already handled.
    (r"(?<![A-Za-z])lanes(?![A-Za-z])", "threads"),
    (r"(?<![A-Za-z])lane(?![A-Za-z])", "thread"),
    # Uppercase env-var style: LANE_BUDGET etc. → THREAD_BUDGET
    (r"\bLANE_(?=[A-Z_])", "THREAD_"),
]


def rename_file(path: Path) -> int:
    text = path.read_text(encoding="utf-8")
    original = text
    for pat, repl in PATTERNS:
        text = re.sub(pat, repl, text)
    if text != original:
        path.write_text(text, encoding="utf-8")
        return text.count("\n") - original.count("\n")  # rough change indicator
    return 0


def main() -> int:
    changed = []
    for root in ROOTS:
        for path in sorted(root.rglob("*.py")):
            if path.is_dir():
                continue
            before = path.read_text(encoding="utf-8")
            rename_file(path)
            if before != path.read_text(encoding="utf-8"):
                changed.append(path.relative_to(root.parent))
    print(f"Renamed in {len(changed)} files:")
    for p in changed:
        print(f"  {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
