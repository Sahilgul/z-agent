"""Responder (plan Phase 4): a PR comment lands → the ORIGINATING run answers.

The comment author needs no Zagent identity — attribution was settled when the
run was created; the engine therefore skips owner resolution for handler-routed
events. Routing: pr_links.ado_pr_id → run. An active lane gets a nudge (graceful
interrupt + inject); a finished lane is CONTINUED — a responder lane spawned
against the same run with resume_session=True so the worker resumes from the
lane's durable session volume (BUG-1 fix), then pushes the update.
"""

from __future__ import annotations

from app.core.logging import get_logger
from app.db.base import get_session
from app.db.models.delivery import PrLink
from app.db.models.lane import Lane
from app.db.models.repo import Repo
from app.db.models.run import Run
from app.services.runs import TERMINAL_STAGES
from zagent_contracts.triggers import TriggerEvent

log = get_logger(service="responder")

RESPONDER_PERSONA = (
    "You are the Responder continuing this run. A reviewer commented on your PR. "
    "Your session volume is mounted — resume your prior context, address the "
    "comment with the smallest honest change, run the repo's verification, and "
    "push the update to the SAME branch. If the comment is a question, answer it "
    "in the PR thread instead of changing code."
)


def _comment_prompt(event: TriggerEvent) -> str:
    author = event.payload.get("author") or "a reviewer"
    text = (event.payload.get("text") or "")[:4000]
    return (f"PR {event.payload.get('pr_id')} review comment from {author}:\n\n"
            f"{text}\n\nAddress it per your instructions.")


def _pr_link(pr_id: int) -> dict | None:
    session = get_session()
    try:
        link = (session.query(PrLink).filter_by(ado_pr_id=pr_id)
                .order_by(PrLink.id.desc()).first())
        if link is None:
            return None
        return {"run_id": link.run_id, "repo": link.repo, "branch": link.branch}
    finally:
        session.close()


def _active_lane(run_id: str) -> str | None:
    session = get_session()
    try:
        lane = (session.query(Lane).filter_by(run_id=run_id)
                .filter(Lane.status.in_(["running", "queued"]))
                .order_by(Lane.created_at).first())
        return lane.id if lane else None
    finally:
        session.close()


async def respond(event: TriggerEvent, trigger, run_manager) -> dict:
    """Engine handler for pr.comment events."""
    pr_id = event.payload.get("pr_id")
    link = _pr_link(int(pr_id)) if pr_id is not None else None
    if link is None:
        return {"status": "ignored", "reason": "unknown_pr"}
    run_id = link["run_id"]

    session = get_session()
    try:
        run = session.get(Run, run_id)
        if run is None or run.stage in TERMINAL_STAGES:
            return {"status": "ignored", "reason": "run_terminal"}
    finally:
        session.close()

    lane_id = _active_lane(run_id)
    if lane_id is not None:
        await run_manager.nudge_lane(run_id, lane_id, _comment_prompt(event))
        return {"status": "nudged", "run_id": run_id}

    # The lane finished: continue the run from the durable session volume.
    session = get_session()
    try:
        run = session.get(Run, run_id)
        repo = session.query(Repo).filter_by(name=link["repo"]).one_or_none()
        session.expunge_all()
    finally:
        session.close()
    lane = await run_manager.lane_manager.spawn(
        run, persona="responder", prompt=_comment_prompt(event),
        persona_prompt=RESPONDER_PERSONA, writable_repo=repo, context_repos=[],
        resume_session=True)
    return {"status": "resumed", "run_id": run_id, "lane_id": lane.id}
