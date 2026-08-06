"""@ repo mentions: the composer autocomplete wraps a picked repo as
`@RepoName` (backtick-quoted) inline in the user's message. Extraction and
validation against the registered fleet live here so the API, the blueprint
hydrates, and the follow-up intent path all parse identically.

There is NO default repo anywhere in the system: a run's repo scope comes
from an explicit API field or an @mention, never a fallback name.
"""

from __future__ import annotations

import re

from app.db.models.repo import Repo

# Only backtick-wrapped mentions count — the composer produces `@Name`; a
# bare @word in prose is conversation, not a scope directive.
MENTION_RE = re.compile(r"`@([A-Za-z0-9][A-Za-z0-9_.-]*)`")


def extract_mentions(text: str | None) -> list[str]:
    """Mentioned repo names, deduped, in first-appearance order."""
    if not text:
        return []
    names: list[str] = []
    for name in MENTION_RE.findall(text):
        if name not in names:
            names.append(name)
    return names


def resolve_run_repos(session, explicit_repo: str | None,
                      task_text: str | None) -> tuple[Repo | None, list[Repo], list[str]]:
    """Resolve a run's target + context repos from the explicit field and the
    task's @mentions.

    Returns (target, context, unknown):
      - target: the explicit repo if given, else the first mention, else None.
      - context: every resolved repo (target included, first), mention order.
      - unknown: mentioned names that aren't registered — caller decides
        whether that's a 422 (API) or a RuntimeError (blueprint hydrate).
    """
    names = extract_mentions(task_text)
    if explicit_repo and explicit_repo not in names:
        names.insert(0, explicit_repo)
    context: list[Repo] = []
    unknown: list[str] = []
    for name in names:
        repo = session.query(Repo).filter_by(name=name).one_or_none()
        (context if repo is not None else unknown).append(repo if repo is not None else name)
    target = context[0] if context else None
    return target, context, unknown
