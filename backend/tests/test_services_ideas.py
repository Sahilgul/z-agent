"""Ideas space tests: shared threads, permanent Counsel comments,
Lead synthesis, promote-to-Plan brief. Completions are injected — no sockets.
"""

import pytest

from app.db.models.idea import IdeaThread
from app.services import ideas


def _thread(session, make_user, title="Ship the fleet graph to threads?"):
    u = make_user()
    return u, ideas.create_thread(title, "would it change how we scope plans?", u.id)


async def _fake_complete(messages):
    return "fake completion"


# ------------------------------------------------------------------- threads
def test_thread_crud_and_comment_names(session, make_user):
    u, t = _thread(session, make_user)
    ideas.comment(t["id"], "user", str(u.id), "strong yes — blast radius is our widest pain")
    detail = ideas.get_thread(t["id"])
    assert detail["comments"][0]["author_name"] == u.display_name
    assert detail["status"] == "open"
    listed = ideas.list_threads()
    assert listed[0]["comment_count"] == 1


def test_threads_are_team_wide(session, make_user):
    a = make_user("alice")
    b = make_user("bob")
    t = ideas.create_thread("bob's idea", "body", b.id)
    ideas.comment(t["id"], "user", str(a.id), "alice chiming in")
    detail = ideas.get_thread(t["id"])
    assert detail["comments"][0]["author_name"] == a.display_name
    assert ideas.list_threads()[0]["title"] == "bob's idea"


def test_get_missing_thread_raises(session):
    with pytest.raises(ideas.IdeasError):
        ideas.get_thread(999)
    with pytest.raises(ideas.IdeasError):
        ideas.comment(999, "user", "1", "hi")


# ------------------------------------------------------------------- Counsel
async def test_ask_counsel_posts_permanent_agent_comment(session, make_user):
    u, t = _thread(session, make_user)
    ideas.comment(t["id"], "user", str(u.id), "members first voice")
    seen = {}

    async def counsel(messages):
        seen["system"] = messages[0]["content"]
        seen["user"] = messages[1]["content"]
        return "Counsel: sequence this AFTER the knowledge flywheel — scoping is only as good as retrieval."

    out = await ideas.ask_counsel(t["id"], complete=counsel)
    assert out["author_type"] == "agent" and out["author_ref"] == "counsel"
    assert out["author_name"] == "counsel"
    # Counsel read the ENTIRE thread (title + body + the member's voice)
    assert "Ship the fleet graph" in seen["user"]
    assert "members first voice" in seen["user"]
    assert "product-thinking" in seen["system"]
    # the comment is permanent, alongside human comments
    detail = ideas.get_thread(t["id"])
    assert [c["author_type"] for c in detail["comments"]] == ["user", "agent"]


async def test_ask_counsel_missing_thread_raises(session):
    with pytest.raises(ideas.IdeasError):
        await ideas.ask_counsel(999, complete=_fake_complete)


# ----------------------------------------------------------------- synthesis
async def test_summarize_pins_structured_summary_and_marks_thread(session, make_user):
    u, t = _thread(session, make_user)
    ideas.comment(t["id"], "user", str(u.id), "yes")
    ideas.comment(t["id"], "agent", "counsel", "wait for the flywheel")

    async def lead(messages):
        return ('Preamble text {"consensus": "worth doing", '
                '"disagreements": ["sequencing vs flywheel"], '
                '"recommendation": "pilot on ServerApp", '
                '"open_questions": ["stale graph risk"]} trailing')

    summary = await ideas.summarize(t["id"], complete=lead)
    assert summary["consensus"] == "worth doing"
    assert summary["disagreements"] == ["sequencing vs flywheel"]
    detail = ideas.get_thread(t["id"])
    assert detail["status"] == "summarized"
    assert detail["summary"] == summary
    # raw voices preserved below the pinned card
    assert len(detail["comments"]) == 2


async def test_summarize_without_json_is_a_clean_error(session, make_user):
    _, t = _thread(session, make_user)

    async def bad(messages):
        return "no json here"

    with pytest.raises(ideas.IdeasError):
        await ideas.summarize(t["id"], complete=bad)


# ------------------------------------------------------------------- promote
def test_plan_task_composes_synthesis_and_voices(session, make_user):
    u, t = _thread(session, make_user)
    ideas.comment(t["id"], "user", str(u.id), "voice one")
    session = None  # service manages its own sessions
    from app.db.base import get_session
    s = get_session()
    try:
        thread = s.get(IdeaThread, t["id"])
        thread.summary_json = {"consensus": "do it", "disagreements": [],
                               "recommendation": "pilot", "open_questions": []}
        s.commit()
    finally:
        s.close()
    task = ideas.plan_task_for(t["id"])
    assert "# Ship the fleet graph to threads?" in task
    assert "Consensus: do it" in task
    assert "voice one" in task


def test_mark_promoted_pins_run(session, make_user):
    _, t = _thread(session, make_user)
    out = ideas.mark_promoted(t["id"], "run-123")
    assert out["status"] == "promoted" and out["promoted_run_id"] == "run-123"
    with pytest.raises(ideas.IdeasError):
        ideas.mark_promoted(999, "run-x")
