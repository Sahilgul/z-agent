"""Mutating tools (plan §6 tools/, Phase 3) + MULTI-ACTOR CONTRACTS §2/§3.

file_edit / file_write / terminal_exec (full) — the mutating surface. These
implement:

  §2 Read-before-edit: every edit/write carries an expected content hash; the
     tool computes the actual hash and REFUSES on mismatch (returns current
     content so the agent re-reads before retrying).
  §3 Two-phase verbatim approval: in SUPERVISED/GATED, mutating calls are
     intercepted by the approval gate (approvals.py) BEFORE execution. The
     tool only ever sees the verbatim args the human approved.

Phase 2's terminal_exec blocked mutating commands; this module's terminal_exec
allows them (after approval, when the gate lets them through).
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
from pathlib import Path
from typing import Any

from langchain_core.tools import tool

_BASH_TIMEOUT_S = 120.0
_BASH_MAX_OUTPUT = 32 * 1024
_BG_TIMEOUT_S = 2 * 60 * 60  # 2h cap for background commands (plan §4 fan-out)


def _workspace() -> Path:
    return Path(os.environ.get("WORKSPACE_DIR", "/workspace"))


def _resolve(file_path: str) -> Path:
    p = Path(file_path)
    return p if p.is_absolute() else _workspace() / p


def content_hash(text: str) -> str:
    """sha256 of file content, hex, truncated to 16 chars (the §2 contract)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


# --- file_edit ---

@tool
def file_edit(file_path: str, old_string: str, new_string: str,
              expected_hash: str | None = None) -> str:
    """Replace exactly one occurrence of old_string with new_string in a file.

    Args:
        file_path: path relative to workspace root, or absolute.
        old_string: the exact text to replace (must appear exactly once).
        new_string: the replacement text.
        expected_hash: optional content hash guard (§2 read-before-edit). If
            provided and the file's current hash differs, the edit is REFUSED
            and the current content is returned so the caller re-reads first.
    """
    p = _resolve(file_path)
    if not p.exists() or p.is_dir():
        return f"error: file not found or is a directory: {file_path}"
    try:
        current = p.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return f"error: {exc}"

    # §2 read-before-edit guard
    if expected_hash is not None and content_hash(current) != expected_hash:
        return (f"error: content hash mismatch — the file changed since you read it. "
                f"Re-read {file_path} and retry. Current hash: {content_hash(current)}\n"
                f"--- current content ---\n{current[:4000]}")

    count = current.count(old_string)
    if count == 0:
        return f"error: old_string not found in {file_path}"
    if count > 1:
        return f"error: old_string appears {count} times in {file_path} — must be unique"

    new_content = current.replace(old_string, new_string, 1)
    try:
        p.write_text(new_content, encoding="utf-8")
    except OSError as exc:
        return f"error: write failed: {exc}"
    return (f"edited {file_path} ({len(old_string)} -> {len(new_string)} chars). "
            f"new hash: {content_hash(new_content)}")


# --- file_write ---

@tool
def file_write(file_path: str, content: str, expected_hash: str | None = None) -> str:
    """Write content to a file (create or overwrite).

    Args:
        file_path: path relative to workspace root, or absolute.
        content: the full file content to write.
        expected_hash: §2 guard. If null, the file must NOT exist (create-new).
            If provided, the file must exist with that hash (overwrite).
    """
    p = _resolve(file_path)
    p.parent.mkdir(parents=True, exist_ok=True)

    if p.exists():
        if expected_hash is None:
            return f"error: {file_path} already exists — provide expected_hash to overwrite"
        try:
            current = p.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return f"error: {exc}"
        if content_hash(current) != expected_hash:
            return (f"error: content hash mismatch — the file changed since you read it. "
                    f"Re-read {file_path} and retry. Current hash: {content_hash(current)}")
    else:
        if expected_hash is not None:
            return f"error: {file_path} does not exist — cannot overwrite a non-existent file"

    try:
        p.write_text(content, encoding="utf-8")
    except OSError as exc:
        return f"error: write failed: {exc}"
    # §7 diagnostics hook: ERROR-only, bounded, never fails the write.
    from worker.engine.tools.diagnostics import diagnostics_block
    return (f"wrote {file_path} ({len(content)} chars). new hash: {content_hash(content)}"
            + diagnostics_block(p))


# --- terminal_exec (full, mutating allowed after approval) ---

# Commands that are DESTRUCTIVE — never eligible for always_allow (§3).
# No trailing \b: commands can end in non-word chars (e.g. "rm -rf /").
DESTRUCTIVE_COMMANDS = re.compile(
    r"\b("
    r"git\s+push\s+(-f|--force)"
    r"|git\s+reset\s+--hard"
    r"|rm\s+-rf?\s+/"
    r"|mkfs"
    r"|fdisk"
    r"|dd\s+if="
    r"|git\s+push\s+.*--no-verify"
    r"|:\(\)\s*\{\s*:\|:&\s*\};:\s*\}"
    r"|git\s+filter-branch"
    r")"
)


@tool
def terminal_exec(command: str, background: bool = False,
                  watch_regex: str | None = None) -> str:
    """Run a shell command in the workspace. Mutating commands allowed (after
    approval, when the gate lets them through).

    Background contract (R24#1): commands outliving the 30s foreground window
    auto-background — you get a job id and the command keeps running; poll its
    output or wait on it instead of retrying the same command.

    Args:
        command: ONE shell command (no interactive programs; stdin is closed).
        background: skip the foreground window, start backgrounded immediately.
        watch_regex: optional regex watched in the output (>=5s debounce);
            matches are reported at turn end.
    """
    return "ok: dispatched by the engine (terminal contract in tools/background.py)"


async def terminal_exec_async(args: dict[str, Any]) -> dict[str, Any]:
    """The real terminal_exec dispatch (background contract, tools/background.py).
    Quick commands still resolve synchronously inside the foreground window;
    destructive approval happens upstream at the gate."""
    command = str(args.get("command", ""))
    if not command.strip():
        return {"kind": "error", "ok": False, "output": "error: empty command",
                "tool": "terminal_exec", "args": args}
    if any(tok in command for tok in ("rm -rf /", "mkfs", ":(){")):
        pass  # destructiveness is a GATE concern (verbatim card); execution obeys
    from worker.engine.tools.background import terminal_manager
    try:
        return await terminal_manager().run(
            command,
            background=bool(args.get("background", False)),
            watch_regex=args.get("watch_regex"),
        )
    except Exception as exc:  # noqa: BLE001
        return {"kind": "error", "ok": False, "output": f"error: {exc}",
                "tool": "terminal_exec", "args": args}


def is_destructive_command(command: str) -> bool:
    """§3: destructive commands never get always_allow — verbatim every time."""
    return bool(DESTRUCTIVE_COMMANDS.search(command))


# --- Registry ---

MUTATING_TOOLS = [file_edit, file_write, terminal_exec]
MUTATING_TOOL_BY_NAME: dict[str, Any] = {t.name: t for t in MUTATING_TOOLS}


async def call_mutating_tool(name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Async shim for mutating tools — same error-prefix convention as readonly.
    terminal_exec routes through the R24#1 background contract manager."""
    if name == "terminal_exec":
        return await terminal_exec_async(args)
    t = MUTATING_TOOL_BY_NAME.get(name)
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
    except Exception as exc:  # noqa: BLE001
        return {"kind": "error", "ok": False, "output": f"error: {exc}", "tool": name, "args": args}


__all__ = [
    "MUTATING_TOOLS",
    "MUTATING_TOOL_BY_NAME",
    "call_mutating_tool",
    "content_hash",
    "file_edit",
    "file_write",
    "is_destructive_command",
    "terminal_exec",
]
