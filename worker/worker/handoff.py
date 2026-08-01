"""Long-run handoff: handoff.md + git checkpoint (plan §8 worker).

Written into the writable stamp so a kill-and-replace lane (or a human) can pick
up exactly where the previous container stopped. The 'living artifact' memory
faculty (plan §6) — an organ the next context READS.
"""

from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from pathlib import Path


def write_handoff(workspace: Path, run_id: str, lane_id: str, summary: str,
                  open_items: list[str], next_steps: list[str]) -> Path:
    handoff = workspace / "handoff.md"
    lines = [
        f"# Handoff — run {run_id} / lane {lane_id}",
        f"_written {datetime.now(timezone.utc).isoformat()}_",
        "",
        "## State so far",
        summary,
        "",
        "## Open items",
        *(f"- {item}" for item in open_items),
        "",
        "## Next steps",
        *(f"- {step}" for step in next_steps),
        "",
    ]
    handoff.write_text("\n".join(lines), encoding="utf-8")
    return handoff


def git_checkpoint(workspace: Path, message: str = "zagent checkpoint") -> str | None:
    """Commit WIP inside the disposable stamp so a replacement lane re-stamps
    with context — the stamp is disposable, but checkpoints ride pushed branches
    for survivors (plan §3)."""
    add = subprocess.run(["git", "-C", str(workspace), "add", "-A"], capture_output=True)
    if add.returncode != 0:
        return None
    commit = subprocess.run(
        ["git", "-C", str(workspace), "commit", "-m", message, "--no-verify"],
        capture_output=True, text=True,
    )
    if commit.returncode != 0:
        return None
    rev = subprocess.run(["git", "-C", str(workspace), "rev-parse", "HEAD"],
                         capture_output=True, text=True)
    return rev.stdout.strip() if rev.returncode == 0 else None
