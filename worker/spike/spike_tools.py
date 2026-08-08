"""LangGraph-based spike tools — minimal read/edit/bash for the Phase 0 fidelity matrix.

These are NOT the production tool suite (that lands in worker/engine/tools/ in
Phase 2). They are the smallest tool surface that exercises the gateway's
tool-call translation fidelity: a read, an edit, and a shell round-trip per
turn. The matrix measures whether the model + gateway can drive a tool loop
cleanly, not whether these tools are feature-complete.
"""

from __future__ import annotations

import asyncio
import re
import subprocess
from pathlib import Path
from typing import Any

from langchain_core.tools import tool

# ---------------------------------------------------------------- file_read

@tool
def file_read(file_path: str, offset: int = 1, limit: int = 2000) -> str:
    """Read a file from the workspace. Returns line-numbered text (1-indexed).

    Args:
        file_path: Path relative to the workspace root (or absolute).
        offset: 1-indexed first line to read (default 1).
        limit: Max lines to return (default 2000).

    Returns:
        Line-numbered content like `  1|first line`, plus a footer with
        total-line count and byte size. Truncated reads get an actionable footer.
    """
    p = Path(file_path)
    if not p.exists():
        return f"error: file not found: {file_path}"
    if p.is_dir():
        return f"error: path is a directory: {file_path}"
    try:
        raw = p.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        return f"error: read failed: {exc}"
    lines = raw.splitlines()
    total = len(lines)
    size = p.stat().st_size
    start = max(1, offset)
    end = min(total, start + limit - 1)
    slice_lines = lines[start - 1 : end]
    body = "\n".join(f"{i + start:6d}|{ln}" for i, ln in enumerate(slice_lines))
    footer = f"\n[{file_path}: {total} lines, {size} bytes; showed {start}-{end}]"
    if end < total:
        footer += f" — {total - end} more lines below"
    return body + footer


# ---------------------------------------------------------------- file_edit

@tool
def file_edit(file_path: str, old_string: str, new_string: str) -> str:
    """Replace exactly one occurrence of old_string with new_string in a file.

    Fails if old_string is absent or appears more than once (ambiguous). This is
    the exact-match contract the production file_edit uses; the spike validates
    that the model can produce well-formed edit args through the gateway.
    """
    p = Path(file_path)
    if not p.exists():
        return f"error: file not found: {file_path}"
    text = p.read_text(encoding="utf-8", errors="replace")
    occurrences = text.count(old_string)
    if occurrences == 0:
        return f"error: old_string not found in {file_path}"
    if occurrences > 1:
        return f"error: old_string appears {occurrences} times in {file_path} — needs more context"
    new_text = text.replace(old_string, new_string, 1)
    p.write_text(new_text, encoding="utf-8")
    diff_line = f"edited {file_path}: 1 replacement ({len(old_string)} -> {len(new_string)} chars)"
    return diff_line


# ---------------------------------------------------------------- bash

_BASH_TIMEOUT_S = 30.0
_BASH_MAX_OUTPUT = 16 * 1024  # 16 KiB inline; larger evicted to a pointer (spike: just truncated)


@tool
def bash(command: str) -> str:
    """Run a shell command in the workspace. One command per call.

    Returns combined stdout+stderr, truncated to 16 KiB with a footer. Times
    out at 30s (exit 124). Interactive commands are rejected (stdin closed).
    """
    if not command.strip():
        return "error: empty command"
    try:
        proc = subprocess.run(
            command,
            shell=True,
            check=False,
            capture_output=True,
            text=True,
            timeout=_BASH_TIMEOUT_S,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        return f"error: command timed out after {_BASH_TIMEOUT_S}s\n$ {command}"
    except Exception as exc:
        return f"error: exec failed: {exc}"
    out = (proc.stdout or "") + (proc.stderr or "")
    truncated = ""
    if len(out) > _BASH_MAX_OUTPUT:
        kept = out[:_BASH_MAX_OUTPUT]
        omitted = len(out) - _BASH_MAX_OUTPUT
        out = kept
        truncated = f"\n... {omitted} bytes omitted ..."
    return f"$ {command}\n{out}{truncated}\n[exit {proc.returncode}]"


# ---------------------------------------------------------------- registry

SPIKE_TOOLS = [file_read, file_edit, bash]

TOOL_BY_NAME: dict[str, Any] = {t.name: t for t in SPIKE_TOOLS}


def looks_like_test(command: str) -> bool:
    return bool(re.search(r"\b(pytest|npm test|vitest|jest|pnpm test)\b", command))


# ---------------------------------------------------------------- async shim

async def call_tool(name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Invoke a spike tool by name and return a structured result dict.

    The structured shape mirrors the production ToolResult taxonomy (plan §7):
    kind = success|error, plus the tool's raw output. Used by the matrix runner
    to score tool-call fidelity (check a) without coupling to LangGraph's
    internal ToolMessage format.
    """
    fn = TOOL_BY_NAME.get(name)
    if fn is None:
        return {"kind": "error", "ok": False, "output": f"unknown tool: {name}"}
    try:
        # langchain @tool-decorated functions are sync; run in a worker thread
        # so the event loop stays responsive during the soak.
        result = await asyncio.to_thread(fn.invoke, args)
        is_err = isinstance(result, str) and result.startswith("error:")
        return {"kind": "error" if is_err else "success", "ok": not is_err, "output": result}
    except Exception as exc:
        return {"kind": "error", "ok": False, "output": f"tool exception: {exc}"}


__all__ = ["SPIKE_TOOLS", "TOOL_BY_NAME", "bash", "call_tool", "file_edit", "file_read", "looks_like_test"]
