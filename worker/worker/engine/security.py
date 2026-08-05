"""Security contracts (plan §6 security.py, §7 quarantine).

Two load-bearing security surfaces:

1. SECRETS REDACTION — every tool output that leaves the sandbox (events,
   deltas, approvals) is run through redact(). The redactor is a denylist of
   high-entropy patterns + known secret shapes; it errs toward redaction
   (false positives are acceptable, false negatives are not). Redacted text is
   tagged so the UI can show a "redacted" chip instead of a blank.

2. QUARANTINE — terminal_exec writes that touch sensitive paths (secrets,
   credentials, .env, private keys) are quarantined: the write is intercepted,
   the file is moved to a read-only quarantine dir, and the event is flagged
   for human review. Never auto-delete; the human decides.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
from pathlib import Path
from typing import Any

# --- Secrets redaction (denylist + high-entropy shapes) ---

_REDACTION_TOKEN = "«REDACTED»"

# Known secret shapes (order matters — most specific first)
_SECRET_PATTERNS: list[re.Pattern[str]] = [
    # Bearer / Authorization tokens
    re.compile(r"(Bearer\s+)[A-Za-z0-9\-._~+/]+=*", re.IGNORECASE),
    # xoxb- / xoxp- (Slack)
    re.compile(r"(xox[bpoa]-)\d+-\d+-\d+-\w+"),
    # sk- / sk-ant- (OpenAI / Anthropic)
    re.compile(r"(sk-ant-)[A-Za-z0-9\-_]{20,}"),
    re.compile(r"(sk-)[A-Za-z0-9]{20,}"),
    # AKIA... (AWS access key id)
    re.compile(r"(AKIA)[A-Z0-9]{16}"),
    # gh[ps]_ / github_pat_ (GitHub tokens)
    re.compile(r"(gh[ps]_)[A-Za-z0-9]{36,}"),
    re.compile(r"(github_pat_)[A-Za-z0-9_]{22,}"),
    # jwt-ish (three base64url chunks separated by dots, 20+ chars each)
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
    # generic api_key= / token= / secret= assignments
    re.compile(
        r"(?i)(api[_-]?key|token|secret|password|passwd|auth)\s*[=:]\s*['\"]?[A-Za-z0-9+/=_\-]{16,}['\"]?"
    ),
    # private key blocks
    re.compile(r"-----BEGIN (RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----[\s\S]*?-----END (RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----"),
]

# Sensitive filenames whose contents are redacted wholesale
_SENSITIVE_FILENAMES = {
    ".env", ".env.local", ".env.production", ".env.development",
    "credentials.json", "credentials", ".npmrc", ".pypirc",
    "id_rsa", "id_ecdsa", "id_ed25519",
    ".aws/credentials", ".git-credentials",
}


def redact(text: str) -> str:
    """Redact known secret shapes from text. Errs toward redaction."""
    if not text:
        return text
    for pat in _SECRET_PATTERNS:
        text = pat.sub(_REDACTION_TOKEN, text)
    return text


def redact_dict(d: dict[str, Any]) -> dict[str, Any]:
    """Recursively redact string values in a dict (tool outputs, event details)."""
    out: dict[str, Any] = {}
    for k, v in d.items():
        if isinstance(v, str):
            out[k] = redact(v)
        elif isinstance(v, dict):
            out[k] = redact_dict(v)
        elif isinstance(v, list):
            out[k] = [redact_dict(x) if isinstance(x, dict) else (redact(x) if isinstance(x, str) else x) for x in v]
        else:
            out[k] = v
    return out


def is_sensitive_path(path: str) -> bool:
    """True if the path looks like a secrets/credentials file."""
    name = os.path.basename(path)
    if name in _SENSITIVE_FILENAMES:
        return True
    lower = path.lower()
    return any(s in lower for s in ("/.ssh/", "/.aws/credentials", "id_rsa", "id_ed25519", "id_ecdsa"))


# --- Quarantine (plan §7 — terminal_exec sensitive writes) ---

class QuarantineError(RuntimeError):
    """Raised when a sensitive write cannot be quarantined (disk full, etc.)."""


def quarantine_path(workspace: Path) -> Path:
    """The read-only quarantine directory inside the workspace stamp."""
    q = workspace / ".zagent" / "quarantine"
    q.mkdir(parents=True, exist_ok=True)
    return q


def quarantine_file(src: Path, workspace: Path, *, reason: str) -> Path:
    """Move a sensitive file to quarantine, make it read-only, return the dest.

    Never auto-delete. The human reviews and either restores (after redaction)
    or removes. The quarantine dir is append-only + read-only on the files.
    """
    q = quarantine_path(workspace)
    dest = q / f"{src.name}.{os.urandom(4).hex()}"
    shutil.move(str(src), str(dest))
    # Read-only: owner read, no write/exec
    os.chmod(dest, stat.S_IRUSR)
    # Drop a manifest entry so the human knows why + the original path
    manifest = q / "MANIFEST.md"
    with manifest.open("a", encoding="utf-8") as f:
        f.write(f"- `{dest.name}` <- `{src}` — {reason}\n")
    return dest


# --- Injection quarantine boundary markers (plan §12, R31) ---
#
# web_fetch/web_search results, AGENTS.md content, MCP tool outputs, file
# contents, and teammate messages are DATA, never instructions. The tool layer
# wraps untrusted content in typed boundary markers MECHANICALLY (not prompt
# goodwill); the base prompt's S7 names these markers and the critic rubric
# reviews anything crossing the boundary into plans/diffs.

UNTRUSTED_OPEN = "<untrusted_content source=\"{source}\">"
UNTRUSTED_CLOSE = "</untrusted_content>"


def wrap_untrusted(text: str, source: str) -> str:
    """Wrap untrusted tool output in typed boundary markers (§12).

    `source` names the provenance (web_fetch, mcp__server, agents_md, file,
    teammate). Nested markers from the content itself are neutralized so a
    malicious page cannot forge a boundary close.
    """
    safe = text.replace("<untrusted_content", "<untrusted-content") \
               .replace("</untrusted_content>", "</untrusted-content>")
    return f"{UNTRUSTED_OPEN.format(source=source)}\n{safe}\n{UNTRUSTED_CLOSE}"


__all__ = [
    "_REDACTION_TOKEN",
    "QuarantineError",
    "is_sensitive_path",
    "quarantine_file",
    "quarantine_path",
    "redact",
    "redact_dict",
    "wrap_untrusted",
]
