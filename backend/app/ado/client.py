"""Azure DevOps REST adapter (adapter package; credentials lock).

TWO PATs: FETCH_PAT (Code:Read, golden fetcher ONLY) and FLEET_PAT (Code:R&W,
service account, injected into workers at container start). A PAT NEVER lands in
remote URLs, .git/config, events, or logs — git auth rides the credential helper
(scripts/git-credential-collegium) / http.extraHeader from env.

Identity binding: names are labels, never keys. resolve_identity maps an
ADO email -> descriptor via the Graph API and FAILS LOUDLY on 0 or 2+ matches
(the two-Alis rule — the system never picks the likelier Ali).
"""

from __future__ import annotations

import base64
from dataclasses import dataclass

import httpx

from app.core.config import get_settings


class IdentityResolutionError(RuntimeError):
    pass


@dataclass
class AdoIdentity:
    descriptor: str
    display_name: str
    mail: str


class AdoClient:
    def __init__(self, pat: str | None = None, org: str | None = None) -> None:
        settings = get_settings()
        self.org = org or settings.ado_org
        self.project = settings.ado_project
        token = pat if pat is not None else settings.fleet_pat
        # L-26: an empty fleet_pat produced a valid-looking "Basic Og=="
        # header (base64 of ":") and sailed to ADO, which returned a
        # confusing 401/403 instead of a clear "not configured" signal.
        # Fail fast in the constructor so the misconfiguration is caught
        # at the call site, not deep inside an ADO request.
        if not token:
            raise ValueError("ADO PAT is not configured (fleet_pat is empty)")
        auth = base64.b64encode(f":{token}".encode()).decode()
        self._headers = {"Authorization": f"Basic {auth}"}
        self._base = f"https://dev.azure.com/{self.org}"

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(headers=self._headers, timeout=30)

    # ------------------------------------------------------------- repos

    async def list_repos(self) -> list[dict]:
        async with self._client() as c:
            r = await c.get(f"{self._base}/{self.project}/_apis/git/repositories", params={"api-version": "7.1"})
            r.raise_for_status()
            return r.json().get("value", [])

    async def list_branches(self, repo_id: str) -> list[str]:
        """Branch list FETCHED from the remote — integrationBranch is selected
        from this list in the Add-Repo UI, never free-typed."""
        async with self._client() as c:
            r = await c.get(
                f"{self._base}/{self.project}/_apis/git/repositories/{repo_id}/refs",
                params={"api-version": "7.1", "filter": "heads/"},
            )
            r.raise_for_status()
            return [ref["name"].removeprefix("refs/heads/") for ref in r.json().get("value", [])]

    # ------------------------------------------------------------- identity

    async def resolve_identity(self, email: str) -> AdoIdentity:
        """ADO Graph: email -> descriptor. FAIL-LOUD on 0 or 2+ matches."""
        graph_base = f"https://vssps.dev.azure.com/{self.org}"
        matches: list = []
        continuation_token: str | None = None
        async with self._client() as c:
            # H-46: the Graph API pages users via continuationToken. The
            # old code read only the FIRST page, so a valid user past page 1
            # was unresolvable ("no ADO identity"). Loop pages until the
            # token is absent, collecting matches across all pages.
            while True:
                params = {"api-version": "7.1-preview.1"}
                if continuation_token:
                    params["continuationToken"] = continuation_token
                r = await c.get(f"{graph_base}/_apis/graph/users", params=params)
                r.raise_for_status()
                data = r.json()
                matches.extend(
                    u for u in data.get("value", [])
                    if (u.get("mailAddress") or "").lower() == email.lower()
                )
                continuation_token = data.get("continuationToken")
                if not continuation_token:
                    break
        if len(matches) == 0:
            raise IdentityResolutionError(f"no ADO identity for {email}")
        if len(matches) > 1:
            raise IdentityResolutionError(f"{len(matches)} ADO identities for {email} — resolve manually")
        u = matches[0]
        return AdoIdentity(
            descriptor=u["descriptor"],
            display_name=u.get("displayName", ""),
            mail=u.get("mailAddress", email),
        )

    async def connection_data(self, pat: str) -> AdoIdentity:
        """BYO-PAT identity proof: the PAT authenticates this call and
        ADO returns the authenticated user's own descriptor."""
        auth = base64.b64encode(f":{pat}".encode()).decode()
        async with httpx.AsyncClient(
            headers={"Authorization": f"Basic {auth}"}, timeout=30,
        ) as c:
            r = await c.get(
                f"https://dev.azure.com/{self.org}/_apis/connectionData",
                params={"api-version": "7.1"},
            )
            r.raise_for_status()
            user = r.json()["authenticatedUser"]
            return AdoIdentity(
                descriptor=user["descriptor"],
                display_name=user.get("customDisplayName") or user.get("providerDisplayName", ""),
                mail=user.get("properties", {}).get("Account", {}).get("$value", ""),
            )

    # ------------------------------------------------------------- work items

    async def get_work_item(self, work_item_id: int) -> dict:
        async with self._client() as c:
            r = await c.get(
                f"{self._base}/_apis/wit/workitems/{work_item_id}",
                params={"api-version": "7.1", "$expand": "all"},
            )
            r.raise_for_status()
            return r.json()

    async def my_active_tickets(self, descriptor: str) -> list[dict]:
        """'My active tickets' = AssignedTo the stored descriptor — GUID-exact,
        collision-immune. Hydration lists these; the user TAPS one."""
        wiql = {
            "query": (
                "SELECT [System.Id] FROM WorkItems "
                "WHERE [System.AssignedTo] = @me AND [System.State] <> 'Closed' "
                "ORDER BY [System.ChangedDate] DESC"
            )
        }
        async with self._client() as c:
            r = await c.post(
                f"{self._base}/{self.project}/_apis/wit/wiql",
                params={"api-version": "7.1"},
                json=wiql,
                headers={"Content-Type": "application/json"},
            )
            r.raise_for_status()
            refs = r.json().get("workItems", [])[:50]
            if not refs:
                return []
            ids = ",".join(str(w["id"]) for w in refs)
            r2 = await c.get(
                f"{self._base}/_apis/wit/workitems",
                params={"api-version": "7.1", "ids": ids, "fields": "System.Id,System.Title,System.State,System.WorkItemType"},
            )
            r2.raise_for_status()
            return r2.json().get("value", [])

    # ------------------------------------------------------------- pull requests

    async def create_pull_request(self, repo_id: str, source_branch: str, target_branch: str,
                                  title: str, description: str, work_item_id: int | None = None) -> dict:
        body = {
            "sourceRefName": f"refs/heads/{source_branch}",
            "targetRefName": f"refs/heads/{target_branch}",
            "title": title,
            "description": description,
        }
        if work_item_id:
            body["workItemRefs"] = [{"id": str(work_item_id)}]
        async with self._client() as c:
            r = await c.post(
                f"{self._base}/{self.project}/_apis/git/repositories/{repo_id}/pullrequests",
                params={"api-version": "7.1"}, json=body,
            )
            r.raise_for_status()
            return r.json()

    async def complete_pull_request(self, repo_id: str, pr_id: int, merge_commit_message: str = "") -> dict:
        """Merge ceremony lock: the human's approval lives in
        Collegium's evidence trail; completion rides FLEET_PAT (service account
        granted bypass-policies-on-complete). Fallback: deep-link to ADO native."""
        async with self._client() as c:
            r = await c.patch(
                f"{self._base}/{self.project}/_apis/git/repositories/{repo_id}/pullrequests/{pr_id}",
                params={"api-version": "7.1"},
                json={"status": "completed", "completionOptions": {"mergeCommitMessage": merge_commit_message}},
            )
            r.raise_for_status()
            return r.json()
