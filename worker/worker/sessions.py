"""Edit-and-resend via fork_session (verified API path).

fork_session lives in claude_agent_sdk._internal (PRIVATE API) and is synchronous —
this module wraps it in an async helper. The SDK version is PINNED in
worker/pyproject.toml and guarded by worker/tests/test_fork_smoke.py on any bump.
Degraded UX if it breaks: edit-and-resend disabled, plain resume still works.

Semantics: fork_session(old_session_id, up_to_message_id=<uuid before last user
msg>) -> NEW session UUID with the transcript sliced (inclusive) at that point.
Start the client with resume=<new_uuid>; the original attempt is preserved as a
sibling branch.
"""

# NOTE (K16, Wave 4 audit): LEGACY SDK-runtime only — no importers in the
# default ENGINE=custom path. Retained for the flag-gated SDK fallback; do not
# extend.


from __future__ import annotations

import asyncio


class ForkUnavailableError(RuntimeError):
    """Raised when the private fork_session API is missing/changed after an SDK
    bump — the control plane degrades to plain resume (edit-and-resend off)."""


def _load_fork():
    try:
        from claude_agent_sdk._internal.session_mutations import fork_session
        return fork_session
    except (ImportError, AttributeError) as exc:
        raise ForkUnavailableError(f"fork_session unavailable: {exc}") from exc


async def fork_for_edit_and_resend(old_session_id: str, up_to_message_id: str) -> str:
    """Async wrapper: slice the transcript (inclusive) at up_to_message_id and
    return the NEW session UUID to resume from."""
    fork_session = _load_fork()
    loop = asyncio.get_running_loop()
    new_session_id = await loop.run_in_executor(None, fork_session, old_session_id, up_to_message_id)
    return str(new_session_id)
