"""Contract tests for Phase 8 critics + team watchdogs (plan §17)."""

from __future__ import annotations

from worker.engine.watchdogs import (
    BudgetWatchdog,
    CollisionRadar,
    CriticDimension,
    CriticFinding,
    CriticRubric,
    DriftWatchdog,
    IdleGate,
)

# --- Drift watchdog ---

def test_drift_rate_drop_detected():
    wd = DriftWatchdog(rate_drop_threshold=0.20, rate_window=10)
    # First 5 calls all success, last 5 all fail — a 1.0 -> 0.0 drop
    rates = [1.0] * 5 + [0.0] * 5
    sig = wd.check("t1", tool_success_rates=rates, last_event_age_s=1.0)
    assert sig is not None
    assert sig.kind == "rate_drop"
    assert sig.severity == "alert"


def test_drift_no_signal_when_stable():
    wd = DriftWatchdog(rate_window=10)
    rates = [1.0] * 10
    sig = wd.check("t1", tool_success_rates=rates, last_event_age_s=1.0)
    assert sig is None


def test_drift_stall_detected_by_time():
    wd = DriftWatchdog(stall_timeout_s=10.0)
    sig = wd.check("t1", tool_success_rates=[], last_event_age_s=20.0)
    assert sig is not None
    assert sig.kind == "stall"
    assert sig.severity == "warn"


def test_drift_rate_drop_takes_priority_over_stall():
    """If both fire, rate_drop (alert) wins."""
    wd = DriftWatchdog(rate_drop_threshold=0.20, rate_window=10, stall_timeout_s=10.0)
    rates = [1.0] * 5 + [0.0] * 5
    sig = wd.check("t1", tool_success_rates=rates, last_event_age_s=20.0)
    assert sig.kind == "rate_drop"


# --- Collision radar v1 (warn-only) ---

def test_collision_radar_warns_on_overlap():
    radar = CollisionRadar()
    threads = [
        {"thread_id": "t1", "repo_scope": "ServerApp/src/auth"},
        {"thread_id": "t2", "repo_scope": "ServerApp/src/auth"},  # overlap
        {"thread_id": "t3", "repo_scope": "ServerApp/src/api"},
    ]
    warnings = radar.check(threads)
    assert len(warnings) == 1
    assert warnings[0].thread_a == "t1"
    assert warnings[0].thread_b == "t2"
    assert warnings[0].severity == "warn"


def test_collision_radar_no_warning_for_disjoint():
    radar = CollisionRadar()
    threads = [
        {"thread_id": "t1", "repo_scope": "A"},
        {"thread_id": "t2", "repo_scope": "B"},
    ]
    assert radar.check(threads) == []


def test_collision_radar_skips_threads_without_scope():
    radar = CollisionRadar()
    threads = [{"thread_id": "t1"}, {"thread_id": "t2"}]
    assert radar.check(threads) == []


# --- Budget reminders ---

def test_budget_fires_at_50_pct():
    bw = BudgetWatchdog()
    r = bw.check("t1", used=5.0, cap=10.0)
    assert r is not None
    assert r.level == "50"


def test_budget_fires_at_80_pct():
    bw = BudgetWatchdog()
    bw.check("t1", used=5.0, cap=10.0)  # fire 50
    r = bw.check("t1", used=8.0, cap=10.0)  # fire 80
    assert r is not None
    assert r.level == "80"


def test_budget_does_not_refire_same_level():
    bw = BudgetWatchdog()
    bw.check("t1", used=5.0, cap=10.0)
    r = bw.check("t1", used=6.0, cap=10.0)
    assert r is None


def test_budget_zero_cap_returns_none():
    bw = BudgetWatchdog()
    assert bw.check("t1", used=0, cap=0) is None


# --- Idle gate ---

def test_idle_gate_flags_idle_past_ttl():
    gate = IdleGate(idle_ttl_s=10.0)
    assert gate.check("t1", status="idle", idle_for_s=20.0) is True


def test_idle_gate_ignores_running_thread():
    gate = IdleGate(idle_ttl_s=10.0)
    assert gate.check("t1", status="running", idle_for_s=100.0) is False


def test_idle_gate_ignores_idle_within_ttl():
    gate = IdleGate(idle_ttl_s=10.0)
    assert gate.check("t1", status="idle", idle_for_s=5.0) is False


# --- Critic×3 rubric ---

def test_critic_blocks_when_tests_fail():
    rubric = CriticRubric()
    findings = rubric.evaluate(
        plan={"steps": ["a", "b"]},
        evidence={"tests_pass": False},
        diff_summary="some changes",
    )
    blocks = [f for f in findings if f.severity == "block"]
    assert any(f.dimension == CriticDimension.CORRECTNESS for f in blocks)
    should, _ = rubric.should_block(findings)
    assert should is True


def test_critic_blocks_when_plan_has_steps_but_diff_empty():
    rubric = CriticRubric()
    findings = rubric.evaluate(
        plan={"steps": ["a", "b"]},
        evidence={"tests_pass": True},
        diff_summary="",
    )
    blocks = [f for f in findings if f.severity == "block"]
    assert any(f.dimension == CriticDimension.COMPLETENESS for f in blocks)


def test_critic_warns_on_large_or_force_diff():
    rubric = CriticRubric()
    findings = rubric.evaluate(
        plan={"steps": ["a"]},
        evidence={"tests_pass": True},
        diff_summary="x" * 11000,
    )
    warns = [f for f in findings if f.severity == "warn"]
    assert any(f.dimension == CriticDimension.RISK for f in warns)


def test_critic_passes_clean_case():
    rubric = CriticRubric()
    findings = rubric.evaluate(
        plan={"steps": ["a"]},
        evidence={"tests_pass": True},
        diff_summary="some reasonable changes",
    )
    should, _ = rubric.should_block(findings)
    assert should is False


def test_critic_should_block_returns_reason():
    rubric = CriticRubric()
    findings = [CriticFinding(CriticDimension.CORRECTNESS, "block", "tests fail")]
    should, reason = rubric.should_block(findings)
    assert should
    assert "tests fail" in reason
