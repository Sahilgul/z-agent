"""fleet-bench tests: case validation, SWE-bench F2P/P2P scoring
semantics, mining shape, eval runner bookkeeping, report math, before/after.
"""

import pytest

from app.db.models.eval import EvalCase, EvalRun
from app.db.models.run import Run
from app.db.models.user import User
from app.services import bench


class FakeRM:
    def __init__(self):
        self.created = []

    async def create_run(self, source, initiated_by, mode_name, task,
                         repo=None, autonomy=None, **kw):
        import uuid
        run = type("R", (), {"id": str(uuid.uuid4())})()
        self.created.append({"id": run.id, "source": source, "task": task,
                             "repo": repo, "autonomy": autonomy})
        return run


def _case(**kw):
    args = dict(repo="ServerApp", title="billing rounding bug", task_text="fix it",
                base_commit="abc123", fail_to_pass=["test_rounds_half_up"],
                pass_to_pass=["test_billing_smoke"], work_item_id=1234)
    args.update(kw)
    return bench.create_case(**args)


# ----------------------------------------------------------------------- cases
def test_case_requires_f2p_and_base_commit():
    with pytest.raises(bench.BenchError):
        _case(fail_to_pass=[])
    with pytest.raises(bench.BenchError):
        _case(base_commit="")


def test_mine_from_work_item_shape_and_holdout(session):
    item = {"id": 1236, "repo": "ServerApp", "title": "fix totals",
            "description": "totals off by one", "base_commit": "deadbeef",
            "touched_tests": ["test_totals"], "smoke_tests": ["test_smoke"]}
    case = bench.mine_from_work_item(item, held_out_ratio=4)
    assert case["held_out"] is True  # 1236 % 4 == 0
    assert case["fail_to_pass"] == ["test_totals"]
    odd = bench.mine_from_work_item({**item, "id": 1237}, held_out_ratio=4)
    assert odd["held_out"] is False


# --------------------------------------------------------------------- scoring
def test_score_requires_all_f2p_and_all_p2p(session):
    _case()
    case = session.query(EvalCase).one()
    full = bench.score(case, {"test_rounds_half_up": True, "test_billing_smoke": True})
    assert full["resolved"] is True
    broken = bench.score(case, {"test_rounds_half_up": True, "test_billing_smoke": False})
    assert broken["resolved"] is False
    unfix = bench.score(case, {"test_rounds_half_up": False, "test_billing_smoke": True})
    assert unfix["resolved"] is False
    unknown_counts_as_fail = bench.score(case, {})
    assert unknown_counts_as_fail["resolved"] is False
    assert unknown_counts_as_fail["f2p_passed"] == 0


# ---------------------------------------------------------------------- runner
async def test_start_eval_records_eval_row_and_system_owned_run(session):
    session.add(User(username="system", display_name="sys", status="active", pin_hash="!"))
    session.commit()
    case = _case()
    rm = FakeRM()
    out = await bench.start_eval(case["id"], rm)
    assert session.get(EvalRun, out["eval_id"]).run_id == rm.created[0]["id"]
    assert rm.created[0]["source"] == "bench"
    assert "abc123" in rm.created[0]["task"]  # base commit pinned in the brief
    assert rm.created[0]["autonomy"] == "gated"


def test_record_result_computes_and_stores_verdict(session):
    case = _case()
    session.add(EvalRun(case_id=case["id"], run_id="r1"))
    session.commit()
    ev = session.query(EvalRun).one()
    verdict = bench.record_result(ev.id, {"test_rounds_half_up": True,
                                          "test_billing_smoke": True})
    assert verdict["resolved"] is True
    session.expire_all()  # the service wrote through its own session
    stored = session.get(EvalRun, ev.id)
    assert stored.resolved is True and stored.report["f2p_total"] == 1


# --------------------------------------------------------------------- reports
def test_report_resolution_rate_and_cost(session, make_user):
    u = make_user()
    session.add(Run(id="r1", created_by=u.id, mode="development", cost_usd=2.5))
    session.add(Run(id="r2", created_by=u.id, mode="development", cost_usd=1.5))
    case = _case()
    session.add(EvalRun(case_id=case["id"], run_id="r1", resolved=True,
                        f2p_passed=1, p2p_passed=1))
    session.add(EvalRun(case_id=case["id"], run_id="r2", resolved=False))
    session.commit()
    rep = bench.report()
    assert rep["evals"] == 2 and rep["resolved"] == 1
    assert rep["resolution_rate"] == 0.5
    assert rep["cost_total_usd"] == 4.0 and rep["cost_per_eval_usd"] == 2.0


def test_before_after_split(session):
    from datetime import datetime, timedelta, timezone
    case = _case()
    old = EvalRun(case_id=case["id"], resolved=False,
                  created_at=datetime.now(timezone.utc) - timedelta(days=10))
    new = EvalRun(case_id=case["id"], resolved=True)
    session.add_all([old, new])
    session.commit()
    split = datetime.now(timezone.utc) - timedelta(days=5)
    out = bench.before_after(split)
    assert out["before"]["resolution_rate"] == 0.0
    assert out["after"]["resolution_rate"] == 1.0


def test_bench_gate_summary():
    gate = bench.bench_gate("distiller-candidate-7",
                            [{"resolved": True}, {"resolved": True}, {"resolved": False}])
    assert gate["resolved"] == 2 and gate["total"] == 3
    assert gate["candidate"] == "distiller-candidate-7"
