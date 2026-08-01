"""Hydration service (plan §8/WU5): deterministic pre-run hydration.

Three concerns, all pure code (no LLM):
  * my_tickets — the user's ADO "My active tickets" (AssignedTo the stored
    descriptor), so the New-Run picker lists work items to tap instead of typing.
  * blast_radius — the Layer 0 fleet graph's blast radius for a repo, so the
    planner knows which services a change touches before it drafts.
  * hydrate_title — resolve a run title from an ADO work item when the user taps
    a ticket (falls back to the typed task).
  * PrewarmPool — a stub that records desired prewarms (pre-clone repos for lanes).
    The real pool stamps clones on a warmed worker; the stub records intent so the
    API surface is testable without Docker.
"""

from __future__ import annotations

from app.ado.client import AdoClient
from app.core.fleet import get_fleet_config
from app.core.logging import get_logger

log = get_logger(service="hydration")


async def my_tickets(user, ado_client: AdoClient | None = None) -> list[dict]:
    """The user's ADO 'My active tickets'. Requires a bound ado_descriptor
    (identity binding §1b); returns [] when unbound so the UI can prompt
    the user to bind their ADO account."""
    descriptor = getattr(user, "ado_descriptor", None)
    if not descriptor:
        return []
    client = ado_client or AdoClient()
    items = await client.my_active_tickets(descriptor)
    return [
        {"id": wi.get("id"), "title": (wi.get("fields") or {}).get("System.Title", ""),
         "state": (wi.get("fields") or {}).get("System.State", ""),
         "type": (wi.get("fields") or {}).get("System.WorkItemType", "")}
        for wi in items
    ]


def blast_radius(repo_name: str) -> list[str]:
    """Layer 0 fleet-graph blast radius for a repo. Returns [] when the graph
    is unavailable (degrades gracefully — the planner still drafts without it)."""
    try:
        _repos, graph = get_fleet_config()
        if graph is None:
            return []
        return list(graph.blast_radius_for(repo_name))
    except Exception as exc:
        log.info("blast radius unavailable", repo=repo_name, error=str(exc)[:120])
        return []


async def hydrate_title(work_item_id: int | None, task: str,
                         ado_client: AdoClient | None = None) -> str:
    """Resolve a run title from an ADO work item when the user taps a ticket;
    fall back to the typed task when no work item is selected or ADO is down."""
    if not work_item_id:
        return task or ""
    try:
        client = ado_client or AdoClient()
        wi = await client.get_work_item(work_item_id)
        return (wi.get("fields") or {}).get("System.Title", "") or (task or "")
    except Exception as exc:
        log.info("title hydration failed; falling back to task", item=work_item_id, error=str(exc)[:120])
        return task or ""


class PrewarmPool:
    """Stub prewarm pool (WU5): records desired prewarms so the API surface is
    testable without Docker. The real pool stamps read-only clones on a warmed
    worker; this stub records intent and reports a per-repo status. A single
    shared instance lives on app.state."""

    def __init__(self) -> None:
        self.requested: list[tuple[str, str]] = []  # (repo, integration_branch)

    async def prewarm(self, repos: list[dict]) -> dict:
        from app.db.base import get_session
        from app.db.models.repo import Repo
        results: list[dict] = []
        session = get_session()
        try:
            for entry in repos:
                name = entry.get("name") if isinstance(entry, dict) else str(entry)
                repo = session.query(Repo).filter_by(name=name).one_or_none()
                branch = (repo.integration_branch if repo else "main") or "main"
                self.requested.append((name, branch))
                # "recorded", never "prewarmed": the stub only records intent — a
                # status claiming warmth the pool can't deliver would lie to the UI.
                results.append({"repo": name, "branch": branch,
                                 "status": "recorded" if repo else "repo_not_registered"})
        finally:
            session.close()
        return {"prewarmed": results}
