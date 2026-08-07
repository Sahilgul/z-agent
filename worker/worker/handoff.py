"""Long-run handoff: handoff.md + git checkpoint.

Written into the writable stamp so a kill-and-replace lane (or a human) can pick
up exactly where the previous container stopped. The 'living artifact' memory
faculty — an organ the next context READS.
"""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path


def write_handoff(workspace: Path, run_id: str, thread_id: str, summary: str,
                  open_items: list[str], next_steps: list[str]) -> Path:
    handoff = workspace / "handoff.md"
    lines = [
        f"# Handoff — run {run_id} / thread {thread_id}",
        f"_written {datetime.now(UTC).isoformat()}_",
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


def git_checkpoint(workspace: Path, message: str = "collegium checkpoint") -> str | None:
    """Commit WIP inside the disposable stamp so a replacement thread re-stamps
    with context — the stamp is disposable, but checkpoints ride pushed branches
    for survivors."""
    add = subprocess.run(["git", "-C", str(workspace), "add", "-A"], capture_output=True, check=False)
    if add.returncode != 0:
        return None
    commit = subprocess.run(
        ["git", "-C", str(workspace), "commit", "-m", message, "--no-verify"],
        capture_output=True, text=True, check=False,
    )
    if commit.returncode != 0:
        return None
    rev = subprocess.run(["git", "-C", str(workspace), "rev-parse", "HEAD"],
                         capture_output=True, text=True, check=False)
    return rev.stdout.strip() if rev.returncode == 0 else None
