"""Sleep-Time Distiller tests: unmined-window bookkeeping, LLM
consolidation (injected), bench gate (drop regressions, neutral without pool),
nightly orchestration drafting user-scoped knowledge with bench deltas.
"""

from datetime import datetime, timedelta, timezone

from app.db.models.approval import Approval
from app.db.models.eval import EvalCase, EvalRun
from app.db.models.knowledge import KnowledgeItem
from app.db.models.trajectory import TrajectorySummary
from app.services import distiller, knowledge


def _summary(session, run_id, user_id, lessons=("retry with smaller diff",),
             days_old=0.2):
    s = TrajectorySummary(
        run_id=run_id, thread_id="thread-1", user_id=user_id,
        summary="run fixed the billing rounding bug after two failed attempts",
        key_decisions=["chose half-up rounding"], lessons=list(lessons),
        created_at=datetime.now(timezone.utc) - timedelta(days=days_old))
    session.add(s)
    session.commit()
    return s


async def _two_candidates(persona, corpus, model=None):
    import json as _json
    return _json.dumps([
        {"content": "retry with a smaller diff when the first patch fails lint",
         "trigger_description": "when a development thread's patch fails lint"},
        {"content": "billing tests need explicit rounding-mode fixtures",
         "trigger_description": "when touching billing totals"},
    ])


# -------------------------------------------------------------------- unmined
def test_unmined_excludes_already_mined_runs(session, make_user):
    u = make_user()
    _summary(session, "run-a", u.id)
    _summary(session, "run-b", u.id)
    knowledge.draft("x", "when y", created_by=u.id, source_run_id="run-a")
    rows = distiller.unmined_since()
    assert [r["run_id"] for r in rows] == ["run-b"]


def test_unmined_respects_window(session, make_user):
    u = make_user()
    _summary(session, "old-run", u.id, days_old=10)
    assert distiller.unmined_since(days=1) == []
    assert len(distiller.unmined_since(days=30)) == 1


# -------------------------------------------------------------------- distill
async def test_distill_parses_json_and_filters(session, make_user):
    u = make_user()
    s = _summary(session, "run-a", u.id)
    out = await distiller.distill([{"id": s.id, "run_id": "run-a", "user_id": u.id,
                                    "summary": s.summary, "lessons": s.lessons,
                                    "key_decisions": []}],
                                  complete=_two_candidates)
    assert len(out) == 2
    assert all("content" in c and "trigger_description" in c for c in out)


async def test_distill_never_crashes_on_bad_reply(session, make_user):
    u = make_user()

    async def bad(persona, corpus, model=None):
        return "not json"

    s = _summary(session, "run-a", u.id)
    out = await distiller.distill([{"id": s.id, "run_id": "run-a", "user_id": u.id,
                                    "summary": "x", "lessons": [], "key_decisions": []}],
                                  complete=bad)
    assert out == []


# ------------------------------------------------------------------ bench gate
def test_bench_delta_neutral_without_pool(session):
    delta = distiller.bench_delta("cand-1", scorer=lambda cases: [])
    assert delta["delta"] == 0.0 and "note" in delta


def test_bench_delta_compares_against_baseline(session):
    case = EvalCase(repo="S", title="c", task_text="t", base_commit="abc",
                    fail_to_pass=["t1"], held_out=True)
    session.add(case)
    session.commit()
    # baseline: one prior eval, resolved
    session.add(EvalRun(case_id=case.id, resolved=True))
    session.commit()
    passing = distiller.bench_delta("cand", scorer=lambda cases: [{"resolved": True}])
    assert passing["passes"] is True and passing["delta"] == 0.0
    regressing = distiller.bench_delta("cand", scorer=lambda cases: [{"resolved": False}])
    assert regressing["passes"] is False and regressing["delta"] == -1.0


# -------------------------------------------------------------------- nightly
async def test_run_nightly_drafts_user_scoped_with_bench_delta(session, make_user):
    u = make_user()
    _summary(session, "run-a", u.id)
    out = await distiller.run_nightly(complete=_two_candidates)
    assert out == {"mined": 1, "candidates": 2, "cards": 2}
    items = session.query(KnowledgeItem).all()
    assert len(items) == 2
    assert all(i.scope == "user" for i in items)  # PHI checkpoint, always
    card = session.query(Approval).filter_by(kind="knowledge").first()
    assert "bench_delta" in card.payload


async def test_run_nightly_drops_regressing_candidates(session, make_user):
    u = make_user()
    _summary(session, "run-a", u.id)
    session.add(EvalCase(repo="S", title="c", task_text="t", base_commit="abc",
                         fail_to_pass=["t1"], held_out=True))
    session.commit()
    session.add(EvalRun(case_id=session.query(EvalCase).one().id, resolved=True))
    session.commit()
    out = await distiller.run_nightly(complete=_two_candidates,
                                      scorer=lambda cases: [{"resolved": False}])
    assert out["candidates"] == 0
    assert session.query(KnowledgeItem).count() == 0


async def test_run_nightly_no_summaries_noop(session):
    assert await distiller.run_nightly() == {"mined": 0, "candidates": 0, "cards": 0}
