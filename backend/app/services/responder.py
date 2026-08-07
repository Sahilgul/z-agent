"""Responder: a PR comment lands → the ORIGINATING run answers.

The comment author needs no Collegium identity — attribution was settled when the
run was created; the engine therefore skips owner resolution for handler-routed
events. Routing: pr_links.ado_pr_id → run. An active thread gets a nudge (graceful
interrupt + inject); a finished thread is CONTINUED — a responder thread spawned
against the same run with resume_session=True so the worker resumes from the
thread's durable session volume, then pushes the update.
"""

from __future__ import annotations

from app.core.logging import get_logger
from app.db.base import get_session
from app.db.models.delivery import PrLink
from app.db.models.thread import Thread
from app.db.models.repo import Repo
from app.db.models.run import Run
from app.services.runs import TERMINAL_STAGES
from collegium_contracts.triggers import TriggerEvent

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


def _active_thread(run_id: str) -> str | None:
    session = get_session()
    try:
        # M-40: an idle thread is still ALIVE (lingering for nudges). Excluding
        # it made _active_thread return None on a live-but-idle session, so the
        # responder spawned a DUPLICATE thread on the next trigger event.
        # Include idle so the existing thread is nudged, not replaced.
        thread = (session.query(Thread).filter_by(run_id=run_id)
                .filter(Thread.status.in_(["running", "queued", "idle"]))
                .order_by(Thread.created_at).first())
        return thread.id if thread else None
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

    thread_id = _active_thread(run_id)
    if thread_id is not None:
        await run_manager.nudge_thread(run_id, thread_id, _comment_prompt(event))
        return {"status": "nudged", "run_id": run_id}

    # The thread finished: continue the run from the durable session volume.
    session = get_session()
    try:
        run = session.get(Run, run_id)
        repo = session.query(Repo).filter_by(name=link["repo"]).one_or_none()
        session.expunge_all()
    finally:
        session.close()
    # M-42: a missing run (deleted / never created) used to crash on `run.id`
    # with an opaque AttributeError after the expunge+close detached the
    # instance. Surface a clear, durable verdict instead of a 500.
    if run is None:
        return {"status": "ignored", "reason": "run_not_found", "run_id": run_id}
    thread = await run_manager.thread_manager.spawn(
        run, persona="responder", prompt=_comment_prompt(event),
        persona_prompt=RESPONDER_PERSONA, writable_repo=repo, context_repos=[],
        resume_session=True)
    return {"status": "resumed", "run_id": run_id, "thread_id": thread.id}
