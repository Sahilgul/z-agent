"""Guidebook seeding (Layer 2): root AGENTS.md PR'd into each
fleet repo.

The generator is PURE and deterministic — content derives from the curated
fleet-config (stack, call chains with citations, judgment notes, anti-context)
plus the repo profile's test commands. <200 lines, always. The same generator
re-renders on drift, so the guidebook EVOLVES through the normal PR path —
never a second copy in .zagent/.

The seeder writes AGENTS.md (+ the CLAUDE.md bridge) into the golden clone on
an agent/guidebook-seed branch, pushes, and opens one PR per repo targeting
the repo's integrationBranch. Idempotent: unchanged content = no branch, no
PR. Per-repo failures are recorded, never sink the batch. Git and ADO are
injectable — unit tests never touch a real shell or socket.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

from app.core.config import get_settings
from app.core.fleet import get_fleet_config
from app.core.logging import get_logger
from app.db.base import get_session
from app.db.models.repo import Repo, RepoStatus

log = get_logger(service="guidebooks")

SEED_BRANCH = "agent/guidebook-seed"
MAX_LINES = 200


class GuidebookError(RuntimeError):
    pass


# ------------------------------------------------------------------ generate

def render_agents_md(repo_name: str, service: dict | None, repo_seed: dict | None,
                     profile_language: str = "", test_cmds: list[str] | None = None) -> str:
    """Render the root AGENTS.md for one repo. Deterministic: same fleet-config
    + profile in, same text out. Capped at MAX_LINES by construction."""
    service = service or {}
    repo_seed = repo_seed or {}
    integration = repo_seed.get("integrationBranch", "main")
    stack = service.get("stack") or repo_seed.get("stack") or profile_language or "unknown"
    role = service.get("role", "")

    lines: list[str] = [
        f"# {repo_name} — agent guidebook",
        "",
        "Audience: coding agents. Human onboarding lives in the README — this file",
        "is the agent's map: what this repo is, how to verify, and what NOT to read.",
        "",
        "## What this is",
        f"- Stack: {stack}",
    ]
    if role:
        lines.append(f"- Role: {role}")
    lines += [
        "",
        "## The golden branch rule",
        f"- The team integrates on `{integration}`. Base ALL work on",
        f"  `origin/{integration}` after a fresh fetch — NEVER on the local checkout's",
        "  current branch (often a personal or feature branch) and NEVER on origin/HEAD.",
        "",
        "## Verify",
    ]
    if test_cmds:
        lines += [f"- `{cmd}`" for cmd in test_cmds]
    else:
        lines.append("- No profile test commands registered yet — ask before inventing one.")
    lines.append("- Never claim a step done without a green verification run.")

    calls = service.get("calls") or []
    called_by = service.get("calledBy") or []
    if calls or called_by:
        lines += ["", "## Cross-repo wiring (fleet graph, code-verified)"]
        for c in calls:
            citation = f" ({c['citation']})" if c.get("citation") else ""
            lines.append(f"- Calls **{c['to']}** via {c['mechanism']}{citation}")
        if called_by:
            lines.append(f"- Called by: {', '.join(called_by)}")

    notes = [n for n in (service.get("notes"), repo_seed.get("notes")) if n]
    if notes:
        lines += ["", "## Judgment lines (carry these — they were earned)"]
        lines += [f"- {n}" for n in notes]

    stale = service.get("staleFlowsIgnore") or []
    anti: list[str] = []
    if stale:
        anti.append(
            "Stale flows — DO NOT load into context (dead code replaced by "
            f"ClinicalAIServices): {', '.join(stale)}")
    if repo_name == "PromptFlowApp":
        live = service.get("liveFlows") or []
        anti.append(
            "ONLY live flows: " + (", ".join(live) if live else "none") +
            ". Everything else here is legacy.")
    if anti:
        lines += ["", "## Anti-context (what NOT to read)"]
        lines += [f"- {a}" for a in anti]

    lines += [
        "",
        "## Working agreement",
        "- Cite file:line for every claim about this codebase; verify by grep,",
        "  never from memory.",
        "- Small, reversible diffs. Conventions of the repo outrank personal taste.",
        "",
    ]
    text = "\n".join(lines)
    if len(text.splitlines()) > MAX_LINES:
        raise GuidebookError(f"{repo_name}: guidebook exceeds {MAX_LINES} lines — tighten the source data")
    return text


def render_claude_md() -> str:
    """Claude-side bridge: one import line, nothing to drift."""
    return "@AGENTS.md\n"


def _fleet_entries() -> tuple[dict[str, dict], dict[str, dict]]:
    """fleet-config lookups by repo name: services graph entry + repos seed."""
    repos_seed, graph = get_fleet_config()
    services = {s["name"]: s for s in (graph.get("services") or [])}
    seeds = {r["name"]: r for r in (repos_seed or [])}
    return services, seeds


# --------------------------------------------------------------------- seed

def _git_default(args: list[str], cwd: Path) -> None:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if result.returncode != 0:
        raise GuidebookError(f"git {' '.join(args)} failed: {result.stderr.strip()[:200]}")


async def seed_repo_guidebook(repo: Repo, ado_client, golden_dir: Path,
                              git_runner=_git_default,
                              profile_language: str = "",
                              test_cmds: list[str] | None = None) -> dict:
    """Seed one repo. Returns a per-repo report dict; raises nothing — failures
    come back as status='error' entries so one repo never sinks the batch.
    Profile data arrives as plain values — the repo row may be detached."""
    services, seeds = _fleet_entries()
    try:
        content = render_agents_md(
            repo.name, services.get(repo.name), seeds.get(repo.name),
            profile_language=profile_language, test_cmds=test_cmds,
        )
        repo_dir = golden_dir / repo.name
        agents_path = repo_dir / "AGENTS.md"
        if agents_path.exists() and agents_path.read_text(encoding="utf-8") == content:
            return {"repo": repo.name, "status": "unchanged"}
        if not repo_dir.exists():
            raise GuidebookError(f"golden clone missing at {repo_dir}")

        agents_path.write_text(content, encoding="utf-8")
        (repo_dir / "CLAUDE.md").write_text(render_claude_md(), encoding="utf-8")
        git_runner(["checkout", "-B", SEED_BRANCH], repo_dir)
        git_runner(["add", "AGENTS.md", "CLAUDE.md"], repo_dir)
        git_runner(["commit", "-m", f"docs: seed agent guidebook (AGENTS.md) for {repo.name}"], repo_dir)
        git_runner(["push", "-u", "origin", SEED_BRANCH, "--force-with-lease"], repo_dir)

        pr = await ado_client.create_pull_request(
            repo_id=repo.ado_repo_id or repo.name,
            source_branch=SEED_BRANCH,
            target_branch=repo.integration_branch,
            title=f"Seed agent guidebook (AGENTS.md) — {repo.name}",
            description=(
                "Root AGENTS.md + CLAUDE.md bridge, generated by Zagent from the "
                "curated fleet-config (call chains, judgment lines, anti-context) "
                "and the repo profile. Regenerated on drift; evolves through the "
                "normal PR path."),
        )
        return {"repo": repo.name, "status": "pr_opened",
                "pr_id": pr.get("pullRequestId"), "branch": SEED_BRANCH}
    except Exception as exc:  # per-repo isolation — record, never sink the batch
        log.error("guidebook seed failed", repo=repo.name, error=str(exc)[:200])
        return {"repo": repo.name, "status": "error", "error": str(exc)[:200]}


async def seed_guidebooks(ado_client, golden_dir: Path | None = None,
                          repo_names: list[str] | None = None,
                          git_runner=_git_default) -> list[dict]:
    """Seed every ready repo (or the named subset). One PR per repo, opened
    sequentially — the batch is small (10) and ADO rate limits are real."""
    golden = golden_dir or get_settings().golden_dir
    session = get_session()
    try:
        q = session.query(Repo).filter(Repo.status.in_([RepoStatus.READY, RepoStatus.READY_NO_MAP]))
        if repo_names:
            q = q.filter(Repo.name.in_(repo_names))
        repos = q.order_by(Repo.name).all()
        # Read everything needed BEFORE detaching — git/ado work must never
        # trigger a lazy load on a dead session.
        work = [{
            "row": r,
            "language": r.profile.language if r.profile else "",
            "test_cmds": list(r.profile.test_cmds) if r.profile else [],
        } for r in repos]
        session.expunge_all()
    finally:
        session.close()
    reports = []
    for entry in work:
        reports.append(await seed_repo_guidebook(
            entry["row"], ado_client, golden, git_runner,
            profile_language=entry["language"], test_cmds=entry["test_cmds"]))
    return reports


def seed_guidebooks_sync(ado_client, **kwargs) -> list[dict]:
    """CLI entry: python -m app.services.guidebooks"""
    return asyncio.run(seed_guidebooks(ado_client, **kwargs))


if __name__ == "__main__":
    from app.ado.client import AdoClient
    for report in seed_guidebooks_sync(AdoClient()):
        print(report)
