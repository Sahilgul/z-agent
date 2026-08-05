"""Phase 1 rename pass 2: compound lane→thread terms (LaneManager, lane_manager, run_lane_container, etc).

Pass 1 handled single-word Lane/lane/lane_id. This handles the compound
identifiers that word-boundary patterns missed. Run on backend/app AND
backend/tests.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOTS = [Path(__file__).resolve().parent.parent / "backend" / "app",
         Path(__file__).resolve().parent.parent / "backend" / "tests"]

PATTERNS: list[tuple[str, str]] = [
    (r"\bLaneManager\b", "ThreadManager"),
    (r"\bLaneSpawnError\b", "ThreadSpawnError"),
    (r"\blane_manager\b", "thread_manager"),
    (r"\brun_lane_container\b", "run_thread_container"),
    (r"\bFakeLaneMgr\b", "FakeThreadMgr"),
    (r"\bfake_lane_manager\b", "fake_thread_manager"),
    (r"\bFakeLaneManager\b", "FakeThreadManager"),
    (r"\blast_lane\b", "last_thread"),
    (r"\bspawn_lane\b", "spawn_thread"),
    (r"\bkill_lane\b", "kill_thread"),
    (r"\bstop_lane\b", "stop_thread"),
    (r"\bnudge_lane\b", "nudge_thread"),
    (r"\bresumed_lane\b", "resumed_thread"),
    (r"\bnew_lane\b", "new_thread"),
    (r"\bnext_lane\b", "next_thread"),
]


def main() -> int:
    changed = []
    for root in ROOTS:
        for path in sorted(root.rglob("*.py")):
            if path.is_dir():
                continue
            text = path.read_text(encoding="utf-8")
            original = text
            for pat, repl in PATTERNS:
                text = re.sub(pat, repl, text)
            if text != original:
                path.write_text(text, encoding="utf-8")
                changed.append(path.relative_to(root.parent))
    print(f"Pass 2: renamed in {len(changed)} files:")
    for p in changed:
        print(f"  {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
