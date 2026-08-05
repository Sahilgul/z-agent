"""Rename pass 3: frontend (apps/web/src) lane → thread.

TS/TSX/CSS mechanical rename. Mirrors the Python rename but for JS identifier
conventions (camelCase laneId → threadId, snake_case lane_id → thread_id,
PascalCase Lane → Thread, UPPER LANE → THREAD).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "apps" / "web" / "src"

PATTERNS: list[tuple[str, str]] = [
    # snake_case (JSON field names from the backend)
    (r"\blane_id\b", "thread_id"),
    (r"\blane_status\b", "thread_status"),
    # camelCase (JS identifiers)
    (r"\blaneId\b", "threadId"),
    (r"\bisStaleLane\b", "isStaleThread"),
    (r"\bstaleLanes\b", "staleThreads"),
    (r"\bcriticalLaneIds\b", "criticalThreadIds"),
    (r"\blastLane\b", "lastThread"),
    (r"\bstopLane\b", "stopThread"),
    (r"\bnudgeLane\b", "nudgeThread"),
    (r"\bnewLane\b", "newThread"),
    # PascalCase (type names) — word boundary, not followed by identifier char
    (r"\bLane\b(?![A-Za-z0-9_])", "Thread"),
    # String literals
    (r"'lane'", "'thread'"),
    (r'"lane"', '"thread"'),
    (r"`lane`", "`thread`"),
    # Channel/comment prose — standalone word
    (r"(?<![A-Za-z])lanes(?![A-Za-z])", "threads"),
    (r"(?<![A-Za-z])lane(?![A-Za-z])", "thread"),
    # UPPER (constants)
    (r"\bLANE_ID\b", "THREAD_ID"),
    (r"\bLANE\b(?![A-Za-z0-9_])", "THREAD"),
]


def main() -> int:
    changed = []
    for path in sorted(ROOT.rglob("*")):
        if path.is_dir():
            continue
        if path.suffix not in {".ts", ".tsx", ".css", ".js", ".jsx"}:
            continue
        text = path.read_text(encoding="utf-8")
        original = text
        for pat, repl in PATTERNS:
            text = re.sub(pat, repl, text)
        if text != original:
            path.write_text(text, encoding="utf-8")
            changed.append(path.relative_to(ROOT))
    print(f"Frontend renamed in {len(changed)} files:")
    for p in changed:
        print(f"  {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
