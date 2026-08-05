"""Delivery service: branch -> push -> PR -> merge ceremony.

Branch naming follows agent/<run8>-<slug> (thread suffix when a thread
stamps it) so ADO branch policies scoped to the agent/* namespace accept
the push. The evidence package is built BEFORE the PR opens — a PR without a
complete package never leaves the station. Merge ceremony identity: the
human's decision lives in Zagent's evidence trail (merged_by); completion rides
FLEET_PAT (service account granted bypass-policies-on-complete). When compliance
disallows policy bypass (settings.merge_native_ui), the merge tap hands off to
ADO's native complete UI via deep link instead (merge-identity lock).

PAT discipline: git push authenticates via the credential helper reading
FLEET_PAT from env — the token never appears in URLs, configs, events, or logs.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from datetime import datetime, timezone

import sqlalchemy as sa

from app.ado.client import AdoClient
from app.core.config import get_settings
from app.core.logging import get_logger
from app.db.base import get_session
from app.db.models.delivery import PrLink
from app.db.models.event import Event
from app.db.models.thread import Thread
from app.db.models.repo import Repo
from app.db.models.run import Plan, PlanStep, Run
from app.db.models.trajectory import TrajectorySummary

log = get_logger(service="delivery")

BRANCH_RE = re.compile(r"[^a-z0-9\-/]")


class DeliveryError(RuntimeError):
    pass


def branch_name_for(run: Run, thread: Thread | None = None) -> str:
    """Branch format agent/<run8>-<slug> — the agent/* namespace is what the
    ADO branch policies grant write access to; any other prefix fails."""
    slug = BRANCH_RE.sub("-", run.title.lower())[:32].strip("-") or "change"
    # H-30: a thread suffix used to be "/<thread8>", producing a TWO-level
    # branch (agent/<run8>-<slug>/<thread8>) — but the ADO branch policy
    # grants write to agent/* (a single-segment wildcard), which does NOT
    # match agent/foo/bar. Keep the branch one level under agent/ with a
    # hyphen suffix so the policy still grants write.
    suffix = f"-{thread.id[:8]}" if thread else ""
    return f"agent/{run.id[:8]}-{slug}{suffix}"


def evidence_sha256(package: dict) -> str:
    """Tamper-evidence for the PR appendix: the PR body (an audited ADO surface)
    carries this hash of the canonical package; the DB copy must match it —
    a package edited after the fact stops matching the PR that shipped it."""
    canonical = json.dumps(package, sort_keys=True, default=str).encode()
    return hashlib.sha256(canonical).hexdigest()


def build_evidence_package(run_id: str) -> dict:
    """Tamper-proof PR appendix: plan, test signals, trajectory, cost.
    Assembled from the DB — never from agent self-reports at PR time."""
    session = get_session()
    try:
        run = session.get(Run, run_id)
        if run is None:
            raise DeliveryError("run not found")
        plan_row = (
            session.query(Plan)
            .filter_by(run_id=run_id)
            .order_by(Plan.created_at.desc(), Plan.id.desc())
            .first()
        )
        threads = session.query(Thread).filter_by(run_id=run_id).all()
        test_events = (
            session.query(Event)
            .filter(Event.run_id == run_id, Event.type == "test_run")
            .order_by(Event.ts)
            .all()
        )
        trajectory = (
            session.query(TrajectorySummary)
            .filter_by(run_id=run_id)
            .order_by(TrajectorySummary.id.desc())
            .first()
        )
        # Step statuses come from the PlanStep ROWS — the system of record the
        # blueprints update as work lands. The planner's structured JSON always
        # says "pending" (that's the schema hint), so reading it here gated
        # every PR on steps that could never look done (review C1).
        step_rows = (
            session.query(PlanStep)
            .filter_by(plan_id=plan_row.id)
            .order_by(PlanStep.index)
            .all()
        ) if plan_row else []
        if step_rows:
            steps = [{"index": s.index, "title": s.title, "status": s.status}
                     for s in step_rows]
        else:
            steps = [
                {"index": s.get("index"), "title": s.get("title"), "status": s.get("status")}
                for s in (plan_row.structured or {}).get("steps", [])
            ] if plan_row else []
        return {
            "schema_version": 1,
            "run_id": run_id,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "plan_title": (plan_row.structured or {}).get("title", "") if plan_row else "",
            "plan_steps": steps,
            "threads": [
                {"persona": l.persona, "status": l.status, "steps": l.next_seq,
                 "cost_usd": l.cost_usd}
                for l in threads
            ],
            "test_signals": [
                {"title": e.title, "ts": e.ts.isoformat(), "payload": e.payload}
                for e in test_events[-10:]
            ],
            "trajectory": trajectory.summary if trajectory else "",
            "total_cost_usd": run.cost_usd,
            "total_tokens": run.tokens,
        }
    finally:
        session.close()


def evidence_complete(package: dict) -> list[str]:
    """What's missing before a PR may open. Empty list = cleared for takeoff."""
    gaps: list[str] = []
    if not package["plan_steps"]:
        gaps.append("no approved plan on record")
    unfinished = [s for s in package["plan_steps"] if s.get("status") not in ("done", "skipped")]
    if unfinished:
        gaps.append(f"{len(unfinished)} plan step(s) not done")
    if not any(l["status"] == "completed" for l in package["threads"]):
        gaps.append("no thread completed successfully")
    return gaps


async def _git(args: list[str], cwd: str, env_extra: dict[str, str]) -> str:
    settings = get_settings()
    import os
    env = dict(os.environ)
    env.update(env_extra)
    env["ZAGENT_CREDENTIAL_SCOPE"] = "fleet"
    env["GIT_CREDENTIAL_HELPER"] = str(settings.scripts_dir / "git-credential-zagent")
    proc = await asyncio.create_subprocess_exec(
        "git", *args, cwd=cwd, env=env,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    if proc.returncode != 0:
        raise DeliveryError(f"git {' '.join(args[:2])} failed: {err.decode()[:300]}")
    return out.decode()


async def sync_before_push(run_id: str, workspace: str, target_branch: str) -> None:
    """Long-run rule: fetch + rebase onto origin/<integrationBranch>
    inside the workspace BEFORE the push, so the PR diffs against current code.
    A conflict raises DeliveryError — rebasing is deterministic, resolving is not."""
    settings = get_settings()
    env = {"FLEET_PAT": settings.fleet_pat}
    await _git(["fetch", "origin", target_branch], cwd=workspace, env_extra=env)
    try:
        await _git(["rebase", f"origin/{target_branch}"], cwd=workspace, env_extra=env)
    except DeliveryError as exc:
        await _git(["rebase", "--abort"], cwd=workspace, env_extra=env)
        raise DeliveryError(
            f"pre-PR rebase onto origin/{target_branch} conflicted — human "
            f"resolution required in {workspace}") from exc
    log.info("workspace rebased pre-PR", run_id=run_id, target=target_branch)


async def commit_pending(run_id: str, workspace: str, branch: str,
                         message: str | None = None) -> bool:
    """Deterministic commit safety net for autonomous pipelines (goal mode):
    point the workspace at the run branch and commit any uncommitted work under
    the bot identity. Returns True when a commit was created, False on a clean
    tree (the agent's own commits already cover the work). checkout -B keeps
    the working tree + HEAD, so the developer thread's commits are never lost."""
    settings = get_settings()
    env = {"FLEET_PAT": settings.fleet_pat}
    await _git(["checkout", "-B", branch], cwd=workspace, env_extra=env)
    await _git(["add", "-A"], cwd=workspace, env_extra=env)
    staged = await _git(["status", "--porcelain"], cwd=workspace, env_extra=env)
    if not staged.strip():
        log.info("commit_pending: clean tree, nothing to do", run_id=run_id, branch=branch)
        return False
    subject = message or f"goal: uncommitted work safety net (run {run_id[:8]})"
    await _git(["-c", "user.name=zagent[bot]", "-c", "user.email=zagent-bot@localhost",
                "commit", "-m", subject], cwd=workspace, env_extra=env)
    log.info("commit_pending: committed", run_id=run_id, branch=branch)
    return True


async def push_branch(run_id: str, repo: Repo, workspace: str, branch: str) -> None:
    """Push the thread's stamped clone branch to ADO. FLEET_PAT rides env into the
    credential helper — never interpolated into the command line."""
    settings = get_settings()
    await _git(["push", "-u", "origin", branch], cwd=workspace,
               env_extra={"FLEET_PAT": settings.fleet_pat})
    log.info("branch pushed", run_id=run_id, repo=repo.name, branch=branch)


async def open_pr(run_id: str, repo_name: str, workspace: str,
                  ado_client: AdoClient | None = None) -> PrLink:
    """Evidence-gated PR open: package first, gaps block, then push+create."""
    package = build_evidence_package(run_id)
    gaps = evidence_complete(package)
    if gaps:
        raise DeliveryError("evidence incomplete: " + "; ".join(gaps))

    session = get_session()
    try:
        run = session.get(Run, run_id)
        repo = session.query(Repo).filter_by(name=repo_name).one_or_none()
        if run is None or repo is None:
            raise DeliveryError("run or repo not found")
        branch = branch_name_for(run)
    finally:
        session.close()

    await sync_before_push(run_id, workspace, repo.integration_branch)
    await push_branch(run_id, repo, workspace, branch)

    # ADO PR descriptions cap at ~4000 chars — the full package stays on the
    # PrLink row; the PR body pins its hash so the two can never silently differ.
    package["sha256"] = evidence_sha256(package)
    client = ado_client or AdoClient()
    description = (
        f"{package['plan_title']}\n\n"
        f"---\nEvidence package (Zagent run {run_id[:8]}):\n"
        f"- plan steps: {len(package['plan_steps'])} (all done/skipped)\n"
        f"- threads: {len(package['threads'])} · cost ${package['total_cost_usd']:.2f}\n"
        f"- test signals: {len(package['test_signals'])}\n"
        f"- evidence sha256: {package['sha256']}\n"
        f"Full package: Zagent run record {run_id[:8]} (PrLink.evidence).\n"
    )
    pr = await client.create_pull_request(
        repo_id=repo.ado_repo_id or repo.name,
        source_branch=branch,
        target_branch=repo.integration_branch,
        title=package["plan_title"] or (run.title if run else branch),
        description=description,
        work_item_id=run.work_item_id if run else None,
    )

    session = get_session()
    try:
        link = PrLink(
            run_id=run_id, repo=repo_name, branch=branch,
            delivery_id=run.delivery_id if run else None,
            ado_pr_id=pr.get("pullRequestId"), status="open", evidence=package,
        )
        session.add(link)
        session.commit()
        session.refresh(link)
        log.info("pr opened", run_id=run_id, pr=link.ado_pr_id, repo=repo_name)
        return link
    finally:
        session.close()


def pr_web_url(repo: Repo, pr_id: int) -> str:
    """Deep link into ADO's native PR UI — the merge handoff target when the
    service account may not bypass policies (merge-identity lock)."""
    settings = get_settings()
    return (f"https://dev.azure.com/{settings.ado_org}/{settings.ado_project}"
            f"/_git/{repo.ado_repo_id or repo.name}/pullrequest/{pr_id}")


async def merge_pr(run_id: str, user_id: int,
                   ado_client: AdoClient | None = None) -> dict:
    """Merge ceremony: the Zagent approval IS the approval of record. Service
    account completes; merged_by stamps the deciding human. When
    settings.merge_native_ui is on (compliance disallows bypass-on-complete),
    NO completion call is made — the human completes in ADO's own UI under
    their own identity; we record the handoff as an audit event and return
    the deep link. Returns {"link": PrLink, "handoff_url": str | None}."""
    settings = get_settings()
    session = get_session()
    try:
        link = (
            session.query(PrLink)
            .filter_by(run_id=run_id, status="open")
            .order_by(PrLink.created_at.desc())
            .first()
        )
        if link is None:
            raise DeliveryError("no open PR for this run")
        repo = session.query(Repo).filter_by(name=link.repo).one()
        pr_id = link.ado_pr_id
    finally:
        session.close()
    if pr_id is None:
        raise DeliveryError("PR link has no ADO id")

    if settings.merge_native_ui:
        handoff = pr_web_url(repo, pr_id)
        session = get_session()
        try:
            # "control-plane" pseudo-thread: the human's tap is a control-plane
            # act; no agent thread exists for it and events.thread_id is NOT NULL.
            session.add(Event(
                run_id=run_id, thread_id="control-plane", seq=0,
                type="merge_handoff",
                title=f"merge handed off to ADO native UI (PR {pr_id})",
                payload={"pr_id": pr_id, "handoff_url": handoff,
                         "decided_by": user_id},
            ))
            session.commit()
        finally:
            session.close()
        log.info("pr merge handed off to native ui", run_id=run_id, pr=pr_id,
                 decided_by=user_id)
        return {"link": link, "handoff_url": handoff}

    client = ado_client or AdoClient()
    await client.complete_pull_request(
        repo_id=repo.ado_repo_id or repo.name, pr_id=pr_id,
        merge_commit_message=f"Zagent run {run_id[:8]} — evidence package attached",
    )

    session = get_session()
    try:
        link = session.query(PrLink).filter_by(run_id=run_id, status="open").order_by(
            PrLink.created_at.desc()).first()
        link.status = "merged"
        link.merged_at = datetime.now(timezone.utc)
        link.merged_by = user_id
        session.commit()
        log.info("pr merged", run_id=run_id, pr=pr_id, merged_by=user_id)
        return {"link": link, "handoff_url": None}
    except Exception:
        # M-38: the ADO merge already succeeded (complete_pull_request
        # returned). A DB failure here used to leave the evidence trail stale
        # — link.status stayed "open" while the PR was actually merged in
        # ADO, so the UI showed an open PR that was already merged. Log a
        # critical reconciliation warning so a reconciler can repair the row;
        # re-raise so the caller knows the DB leg failed.
        log.critical(
            "pr merged in ADO but DB link update failed — evidence trail stale; needs reconciliation",
            run_id=run_id, pr=pr_id, merged_by=user_id)
        raise
    finally:
        session.close()


def mark_merged(run_id: str, user_id: int) -> dict:
    """G-16: the native-UI handoff path (merge_pr under
    settings.merge_native_ui) writes a `merge_handoff` event and returns the
    deep link, leaving the PrLink "open" — the human completes in ADO's own
    UI. There was no path to later mark that PrLink "merged", so the evidence
    trail stayed open forever for a PR the human had actually merged. This
    closes the loop: a webhook (or a manual mark-merged call) flips the
    open link to "merged" with the deciding human's id, matching the
    in-process merge_pr path's evidence trail."""
    session = get_session()
    try:
        link = (
            session.query(PrLink)
            .filter_by(run_id=run_id, status="open")
            .order_by(PrLink.created_at.desc())
            .first()
        )
        if link is None:
            raise DeliveryError("no open PR for this run")
        link.status = "merged"
        link.merged_at = datetime.now(timezone.utc)
        link.merged_by = user_id
        session.commit()
        log.info("pr marked merged (native-UI handoff closed)", run_id=run_id,
                 pr=link.ado_pr_id, merged_by=user_id)
        return {"link": link, "handoff_url": None}
    finally:
        session.close()
