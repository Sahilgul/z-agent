"""Ideas space + Counsel + Lead synthesis.

A team-wide SHARED space (shared-by-design exception — never privacy-scoped):
threads and ALL comments, including Counsel's, are visible to every teammate and
persist permanently. Counsel is the product-thinking 11th team member — on demand
("Ask Counsel") by default, never auto-invoked into human threads. "Summarize"
asks the Lead to synthesize ALL voices into a pinned structured summary;
"Promote to Plan" carries the thread into a plan-mode run.

Counsel/Lead completions go through the LiteLLM gateway; both are injectable
(unit tests never touch a socket).
"""

from __future__ import annotations

import json
import re

import httpx
from sqlalchemy import func

from app.core.config import get_settings
from app.core.logging import get_logger
from app.db.base import get_session
from app.db.models.idea import IdeaComment, IdeaThread
from app.db.models.repo import Repo
from app.db.models.user import User

log = get_logger(service="ideas")

COUNSEL_PERSONA = (
    "You are Counsel, the product-thinking 11th member of the engineering team. "
    "You think in user pain, adoption, sequencing, and opportunity cost — grounded in "
    "the actual repo landscape and architecture, not generic product slogans. You read "
    "the ENTIRE thread before speaking. You disagree openly when the room is wrong, and "
    "you say 'I don't know' when the evidence isn't there. You speak once, substantively, "
    "in a few short paragraphs — never a bulleted wall."
)

LEAD_PERSONA = (
    "You are the Lead synthesizing an idea thread. Produce a JSON object with keys: "
    "consensus (string), disagreements (array of strings), recommendation (string), "
    "open_questions (array of strings). Weight every voice — members and Counsel — "
    "by evidence, not by volume. Reply with ONLY the JSON object."
)

SUMMARY_KEYS = ("consensus", "disagreements", "recommendation", "open_questions")


class IdeasError(ValueError):
    pass


def _serialize_thread(t: IdeaThread, comment_count: int | None = None) -> dict:
    out = {
        "id": t.id, "title": t.title, "body": t.body, "created_by": t.created_by,
        "source": t.source, "proposal_id": t.proposal_id, "status": t.status,
        "summary": t.summary_json, "promoted_run_id": t.promoted_run_id,
        "created_at": t.created_at.isoformat() if t.created_at else None,
    }
    if comment_count is not None:
        out["comment_count"] = comment_count
    return out


def _serialize_comment(c: IdeaComment, author_name: str) -> dict:
    return {
        "id": c.id, "thread_id": c.thread_id, "author_type": c.author_type,
        "author_ref": c.author_ref, "author_name": author_name, "body": c.body,
        "created_at": c.created_at.isoformat() if c.created_at else None,
    }


def create_thread(title: str, body: str, created_by: int,
                  source: str = "user", proposal_id: int | None = None) -> dict:
    session = get_session()
    try:
        thread = IdeaThread(title=title, body=body, created_by=created_by,
                            source=source, proposal_id=proposal_id)
        session.add(thread)
        session.commit()
        session.refresh(thread)
        return _serialize_thread(thread, comment_count=0)
    finally:
        session.close()


def list_threads(status: str | None = None) -> list[dict]:
    session = get_session()
    try:
        q = session.query(IdeaThread)
        if status:
            q = q.filter_by(status=status)
        threads = q.order_by(IdeaThread.id.desc()).all()
        counts = dict(
            session.query(IdeaComment.thread_id, func.count(IdeaComment.id))
            .group_by(IdeaComment.thread_id).all()
        )
        return [_serialize_thread(t, counts.get(t.id, 0)) for t in threads]
    finally:
        session.close()


def get_thread(thread_id: int) -> dict:
    session = get_session()
    try:
        thread = session.get(IdeaThread, thread_id)
        if thread is None:
            raise IdeasError("thread not found")
        comments = (session.query(IdeaComment).filter_by(thread_id=thread_id)
                    .order_by(IdeaComment.id).all())
        user_ids = {c.author_ref for c in comments if c.author_type == "user"}
        names = {
            str(u.id): u.display_name
            for u in session.query(User).filter(User.id.in_([int(i) for i in user_ids if i.isdigit()])).all()
        } if user_ids else {}
        out = _serialize_thread(thread)
        out["comments"] = [
            _serialize_comment(c, names.get(c.author_ref, c.author_ref) if c.author_type == "user"
                               else c.author_ref)
            for c in comments
        ]
        return out
    finally:
        session.close()


def comment(thread_id: int, author_type: str, author_ref: str, body: str) -> dict:
    if author_type not in ("user", "agent"):
        raise IdeasError("author_type must be user|agent")
    session = get_session()
    try:
        if session.get(IdeaThread, thread_id) is None:
            raise IdeasError("thread not found")
        c = IdeaComment(thread_id=thread_id, author_type=author_type,
                        author_ref=author_ref, body=body)
        session.add(c)
        session.commit()
        session.refresh(c)
        name = author_ref
        if author_type == "user" and author_ref.isdigit():
            u = session.get(User, int(author_ref))
            name = u.display_name if u else author_ref
        return _serialize_comment(c, name)
    finally:
        session.close()


# ------------------------------------------------------------------ Counsel

def _thread_transcript(thread_id: int) -> str:
    """The ENTIRE thread, voices labeled — Counsel reads everything before speaking."""
    detail = get_thread(thread_id)
    lines = [f"# {detail['title']}", "", detail["body"], ""]
    for c in detail["comments"]:
        speaker = c["author_name"] if c["author_type"] == "user" else f"[{c['author_ref']} · agent]"
        lines.append(f"{speaker}: {c['body']}")
        lines.append("")
    return "\n".join(lines)


def _fleet_grounding() -> str:
    """Counsel is grounded in the real repo landscape, not generic product talk."""
    session = get_session()
    try:
        repos = session.query(Repo).filter(Repo.status != "archived").all()
        if not repos:
            return ""
        lines = [f"- {r.name} ({r.profile.language or 'unknown stack'})" for r in repos]
        return "The fleet today:\n" + "\n".join(lines)
    finally:
        session.close()


async def gateway_complete(messages: list[dict], model: str | None = None) -> str:
    """One chat completion through the LiteLLM gateway. Raises on any failure —
    callers decide their own fallback."""
    settings = get_settings()
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            f"{settings.gateway_url}/chat/completions",
            headers={"Authorization": f"Bearer {settings.litellm_master_key}"},
            json={"model": model or settings.ideas_model, "messages": messages, "temperature": 0.4},
        )
        resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


async def ask_counsel(thread_id: int, complete=None) -> dict:
    """Counsel reads the whole thread and posts ONE permanent, visibly-labeled
    agent comment (author_ref='counsel'). On demand only — never auto-invoked."""
    complete = complete or gateway_complete
    transcript = _thread_transcript(thread_id)  # raises IdeasError when missing
    messages = [
        {"role": "system", "content": COUNSEL_PERSONA + "\n\n" + _fleet_grounding()},
        {"role": "user", "content": transcript},
    ]
    opinion = await complete(messages)
    return comment(thread_id, "agent", "counsel", opinion.strip())


async def summarize(thread_id: int, complete=None) -> dict:
    """Lead synthesis of ALL voices into the pinned structured summary; the raw
    comments stay preserved below it. Status -> summarized."""
    complete = complete or gateway_complete
    transcript = _thread_transcript(thread_id)
    messages = [
        {"role": "system", "content": LEAD_PERSONA},
        {"role": "user", "content": transcript},
    ]
    raw = await complete(messages)
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        raise IdeasError("lead synthesis returned no JSON object")
    parsed = json.loads(match.group(0))
    summary = {
        "consensus": str(parsed.get("consensus", "")),
        "disagreements": [str(d) for d in parsed.get("disagreements", [])],
        "recommendation": str(parsed.get("recommendation", "")),
        "open_questions": [str(q) for q in parsed.get("open_questions", [])],
    }
    session = get_session()
    try:
        thread = session.get(IdeaThread, thread_id)
        thread.summary_json = summary
        thread.status = "summarized"
        session.commit()
    finally:
        session.close()
    return summary


# ------------------------------------------------------------------ promote

def plan_task_for(thread_id: int) -> str:
    """Compose the plan-mode task from the thread: title + body + the Lead
    synthesis + every voice (bounded — a plan brief, not a log dump)."""
    detail = get_thread(thread_id)
    parts = [f"# {detail['title']}", "", detail["body"][:2000]]
    if detail["summary"]:
        s = detail["summary"]
        parts += ["", "## Lead synthesis",
                  f"Consensus: {s.get('consensus', '')}",
                  f"Recommendation: {s.get('recommendation', '')}"]
        if s.get("disagreements"):
            parts.append("Disagreements: " + "; ".join(s["disagreements"]))
        if s.get("open_questions"):
            parts.append("Open questions: " + "; ".join(s["open_questions"]))
    voices = [f"- {c['author_name']}: {c['body'][:400]}" for c in detail["comments"][:20]]
    if voices:
        parts += ["", "## Thread voices"] + voices
    return "\n".join(parts)


def mark_promoted(thread_id: int, run_id: str) -> dict:
    session = get_session()
    try:
        thread = session.get(IdeaThread, thread_id)
        if thread is None:
            raise IdeasError("thread not found")
        thread.status = "promoted"
        thread.promoted_run_id = run_id
        session.commit()
        return _serialize_thread(thread)
    finally:
        session.close()
