"""Diagnostics hook for file_write (NOT a pull tool).

After a successful write, lint the file and append a bounded ERROR-only block:

    <diagnostics file="src/app.py">
    E501 line too long (line 42)
    ...
    </diagnostics>

Contract: 150ms debounce is a UI-stream concern — engine-side the hook runs
once per write; 5s timeout; ERROR-severity only; cap 20 findings; ruff first
(fast), pyright optional per thread when available. Linter-absent is silent
(no block), linter-crash is silent — diagnostics NEVER fail a write.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

_TIMEOUT_S = 5.0
_CAP = 20
_LINTABLE_SUFFIXES = {".py"}


def diagnostics_block(path: Path) -> str:
    """The <diagnostics> block for a freshly written file, or "" if none."""
    if path.suffix not in _LINTABLE_SUFFIXES:
        return ""
    findings = _ruff(path)
    if not findings:
        return ""
    lines = "\n".join(findings[:_CAP])
    omitted = f"\n... {len(findings) - _CAP} more omitted ..." if len(findings) > _CAP else ""
    return f'\n\n<diagnostics file="{path.name}">\n{lines}{omitted}\n</diagnostics>'


def _ruff(path: Path) -> list[str]:
    exe = shutil.which("ruff")
    if not exe:
        return []
    try:
        proc = subprocess.run(
            [exe, "check", "--output-format=json", "--select", "E9,F,W5", str(path)],
            capture_output=True, text=True, timeout=_TIMEOUT_S, stdin=subprocess.DEVNULL,
            check=False,
        )
        items = json.loads(proc.stdout or "[]")
    except Exception:
        return []
    out = []
    for item in items:
        if not isinstance(item, dict):
            continue
        code = item.get("code", "")
        msg = item.get("message", "")
        loc = (item.get("location") or {}).get("row", "?")
        out.append(f"{code} {msg} (line {loc})")
    return out


__all__ = ["diagnostics_block"]
