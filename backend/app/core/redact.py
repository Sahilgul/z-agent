"""Surface-safe error text: spawn/infra exceptions can embed container ids,
host paths, and URL-embedded git credentials — none of which belong on a
client-rendered banner. Mirrors the worker's security.redact, minimal shape."""

import re

_TOKEN = "[redacted]"
_PATTERNS = (
    re.compile(r"https?://[^/\s:]+:[^@\s]+@"),          # URL-embedded creds
    re.compile(r"(?i)\b(sk|vk|ghp|gho|pat|bearer)[-_][A-Za-z0-9]{8,}"),
    re.compile(r"(?i)(password|token|secret|api[_-]?key)=\S+"),
)


def redact(text: str) -> str:
    if not text:
        return text
    for pat in _PATTERNS:
        text = pat.sub(_TOKEN, text)
    return text
