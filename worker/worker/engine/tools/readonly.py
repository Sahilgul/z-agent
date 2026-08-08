"""Read-only tools.

Ships ONLY read-only tools — writes (file_edit, file_write,
terminal_exec mutating) arrive with the approval + verbatim
contracts. This keeps the ask-mode loop safe to run while the approval
architecture is still being built.

Every tool returns a dict shaped for the event emitter:
  {kind: "success"|"error", ok: bool, output: str, ...}
The redactor (security.redact) is applied at the event-emission boundary,
NOT inside tools, so raw outputs remain available to the agent for reasoning.
"""

from __future__ import annotations

import asyncio
import fnmatch
import os
import re
import subprocess
from pathlib import Path
from typing import Any

from langchain_core.tools import tool

_BASH_TIMEOUT_S = 30.0
_BASH_MAX_OUTPUT = 16 * 1024
_MAX_READ_LINES = 2000


def _workspace() -> Path:
    return Path(os.environ.get("WORKSPACE_DIR", "/workspace"))


# --- file_read ---

@tool
def file_read(file_path: str, offset: int = 1, limit: int = _MAX_READ_LINES) -> str:
    """Read a file from the workspace. Returns line-numbered text (1-indexed).

    Args:
        file_path: path relative to the workspace root, or absolute.
        offset: 1-indexed line to start reading from.
        limit: max number of lines to return.
    """
    p = _resolve(file_path)
    if not p.exists():
        return f"error: file not found: {file_path}"
    if p.is_dir():
        return f"error: {file_path} is a directory"
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return f"error: {exc}"
    lines = text.splitlines()
    start = max(0, offset - 1)
    end = start + limit
    slice_ = lines[start:end]
    numbered = [f"{i + start + 1:6}|{line}" for i, line in enumerate(slice_)]
    footer = f"\n[{len(numbered)} lines; {len(lines)} total]"
    return "\n".join(numbered) + footer


# --- file_search (ripgrep-backed, read-only) ---

@tool
def file_search(pattern: str, glob: str | None = None, max_results: int = 200) -> str:
    """Search file contents with a regex (ripgrep-backed). Read-only.

    Args:
        pattern: regular expression to search for.
        glob: optional file glob to limit search (e.g. "*.py").
        max_results: max number of matches to return.
    """
    ws = _workspace()
    cmd = ["rg", "--no-heading", "-n", "--max-count", str(max_results)]
    if glob:
        cmd += ["-g", glob]
    cmd += [pattern, str(ws)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=15, check=False)
    except FileNotFoundError:
        # ripgrep missing — fall back to grep
        cmd = ["grep", "-rn", "-E", pattern]
        if glob:
            cmd += ["--include", glob]
        cmd += [str(ws)]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30, check=False)
    raw = proc.stdout
    truncated = len(raw) > _BASH_MAX_OUTPUT
    out = raw[:_BASH_MAX_OUTPUT]
    # M-12: an invalid regex makes rg exit 2 with the error on STDERR, but the
    # old code only inspected stdout (empty) and returned "no matches" — the
    # agent never learned its pattern was bad. rg: 0 = matches, 1 = no matches,
    # 2 = error; treat 2+ as an error so the bad pattern surfaces.
    if proc.returncode not in (0, 1):
        err = (proc.stderr or "").strip()
        return (f"error: file_search failed (exit {proc.returncode}): "
                f"{err[:200] or 'invalid pattern or I/O error'}")
    if not out:
        return "no matches"
    # K10: count matches in the FULL output, and say so when the visible
    # window was cut — the old footer counted newlines in the TRUNCATED
    # slice, underreporting with no marker.
    total = raw.count("\n")
    shown = out.count("\n")
    marker = (f"\n[truncated — showing {shown} of {total} matches; "
              "narrow the pattern or glob]") if truncated else ""
    return out + marker + f"\n[{total} matches]"


# --- file_glob ---

@tool
def file_glob(pattern: str) -> str:
    """Find files by glob pattern (e.g. "**/*.py"). Returns paths relative to workspace."""
    ws = _workspace()
    matches: list[str] = []
    for root, _dirs, files in os.walk(ws):
        for name in files:
            full = Path(root) / name
            try:
                rel = full.relative_to(ws)
            except ValueError:
                # A symlink pointing outside the workspace must not fail the
                # whole glob — skip it.
                continue
            # H-06: a pattern WITH a slash (`src/*.py`, `**/*.py`) must match the
            # relative PATH so the directory component is respected — the old
            # code stripped to the last component and matched the filename
            # only, so `src/*.py` matched `*.py` anywhere. A bare pattern
            # (`*.py`, `foo.py`) keeps matching the filename so it still finds
            # files of that name at any depth.
            if "/" in pattern:
                rel_str = str(rel).replace(os.sep, "/")
                matched = fnmatch.fnmatch(rel_str, pattern)
            else:
                matched = fnmatch.fnmatch(name, pattern)
            if matched:
                matches.append(str(rel))
                if len(matches) >= 500:
                    break
        if len(matches) >= 500:
            break
    if not matches:
        return "no files matched"
    return "\n".join(sorted(matches)) + f"\n[{len(matches)} files]"


# --- terminal_exec (read-only commands only) ---

# Commands that mutate state — blocked here (writes arrive with the approval contract).
_BLOCKED_COMMANDS = re.compile(
    r"\b(rm|mv|cp|mkdir|rmdir|chmod|chown|git\s+(commit|push|merge|rebase|reset|"
    r"checkout|branch|tag|stash)|pip\s+install|npm\s+install|yarn\s+add|"
    r"pnpm\s+add|curl|wget|scp|rsync|dd|mkfs|fdisk|kill|killall|pkill)\b"
)

# Commands that are explicitly read-only and always allowed
_READONLY_COMMANDS = re.compile(
    r"^\s*(ls|cat|head|tail|wc|grep|rg|find|git\s+(status|log|diff|show|blame|"
    r"ls-files|rev-parse|remote|branch|stash\s+list)|pwd|echo|env|whoami|"
    r"python\s+--version|node\s+--version|npm\s+--version|test)\b"
)

# Shell operators that chain, redirect, or substitute. Read-only mode forbids
# them outright so a "safe" prefix cannot smuggle a mutating tail past the
# blocked-command check — `ls; rm -rf ~` and `cat x > /etc/cron.d/evil` are the
# canonical escapes the old single-string gate let through.
_CHAIN_OPS = re.compile(r";|&&|\|\||\||&|`|\$\(|\$\{|>>|>|<|\n")


def _gate_command(command: str) -> str | None:
    """Return an error string if the command is unsafe for read-only mode,
    else None. The old gate checked `_BLOCKED_COMMANDS.search(command) and not
    _READONLY_COMMANDS.match(command)` — a chained command whose FIRST segment
    was read-only (e.g. `ls; rm -rf ~`) passed because the readonly prefix
    matched, so the blocked tail ran under `shell=True`. We now forbid chaining
    outright and require the whole (single) command to be on the allowlist."""
    stripped = command.strip()
    if not stripped:
        return "error: empty command"
    if _CHAIN_OPS.search(stripped):
        return ("error: shell chaining/redirection is blocked in read-only mode "
                "(run one read-only command at a time).")
    if _BLOCKED_COMMANDS.search(stripped):
        return ("error: mutating command blocked in read-only mode. "
                "Writes arrive with the approval contract.")
    if not _READONLY_COMMANDS.match(stripped):
        return ("error: command not on the read-only allowlist "
                "(ls, cat, head, tail, wc, grep, rg, find, git status/log/diff/show/blame, "
                "pwd, echo, env, whoami, <tool> --version, test).")
    return None


@tool
def terminal_exec(command: str) -> str:
    """Run a shell command in the workspace. Read-only commands only.

    Mutating commands (rm, git commit, pip install, etc.) are blocked until
    the approval contract ships. Read-only commands (ls, cat, git
    status, grep, rg, find) run directly. Shell chaining/redirection is
    forbidden so a read-only prefix can't smuggle a mutating tail through.
    """
    err = _gate_command(command)
    if err is not None:
        return err
    try:
        proc = subprocess.run(
            command, shell=True, check=False, capture_output=True, text=True,
            timeout=_BASH_TIMEOUT_S, stdin=subprocess.DEVNULL, cwd=str(_workspace()),
        )
    except subprocess.TimeoutExpired:
        return f"error: command timed out after {_BASH_TIMEOUT_S}s"
    combined = proc.stdout + proc.stderr
    truncated = len(combined) > _BASH_MAX_OUTPUT
    out = combined[:_BASH_MAX_OUTPUT]
    # K10: the [exit N] marker must survive (K17's evidence extractor depends
    # on it) and a cut must be VISIBLE — a mid-stream cut otherwise reads as
    # complete output.
    marker = ("\n[truncated — output cut at "
              f"{_BASH_MAX_OUTPUT} chars]") if truncated else ""
    return f"{out}{marker}\n[exit {proc.returncode}]"


def _resolve(file_path: str) -> Path:
    p = Path(file_path)
    if p.is_absolute():
        return p
    return _workspace() / p


# --- Registry ---

READONLY_TOOLS = [file_read, file_search, file_glob, terminal_exec]
TOOL_BY_NAME: dict[str, Any] = {t.name: t for t in READONLY_TOOLS}


async def call_tool(name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Async shim that invokes a tool and returns a normalized result dict.

    Tools return strings; an "error:" prefix indicates a logical error (the
    tool ran without raising but the operation failed). This matches the
    ToolResult taxonomy so the EventEmitter pairs correctly.
    """
    t = TOOL_BY_NAME.get(name)
    if t is None:
        return {"kind": "error", "ok": False, "output": f"unknown tool: {name}"}
    try:
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(None, lambda: t.invoke(args))
        output = str(result)
        is_error = output.startswith("error:")
        return {
            "kind": "error" if is_error else "success",
            "ok": not is_error,
            "output": output,
            "tool": name,
            "args": args,
        }
    except Exception as exc:
        return {"kind": "error", "ok": False, "output": f"error: {exc}", "tool": name, "args": args}


__all__ = [
    "READONLY_TOOLS",
    "TOOL_BY_NAME",
    "call_tool",
    "file_glob",
    "file_read",
    "file_search",
    "terminal_exec",
]
