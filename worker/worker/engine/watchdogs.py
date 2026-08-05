"""Team watchdogs (plan §11, Phase 8) — drift, collision, budget, idle.

The team layer runs alongside the engine, watching the run's threads for
problems that need human attention. Phase 8 ships:

  - DRIFT WATCHDOG: a thread is "drifting" if its tool-success rate drops
    over a window (event-based) OR if it's been running with no progress for
    too long (time-based). Fires a drift event; Phase 8 does NOT auto-stop
    (that's blocked-escalation).
  - COLLISION RADAR v1 (warn-only): two threads' repo scopes overlap. Phase 8
    WARNS only — it does not refuse the spawn (that's the engine-side veto in
    fanout.py). The warning surfaces to the human.
  - BUDGET REMINDERS: at 50% and 80% of a thread/run/goal budget, fire a
    reminder event. Never auto-stop on budget — the human decides.
  - IDLE-GATE TIMERS: a thread that's been idle past its TTL is a candidate
    for completion (the engine's idle watchdog already handles this; the
    team layer surfaces it to the human so they know the thread is done).

The critic×3 merged rubric (plan §8) wraps the implement/verify stages of
goal mode. Three critic passes (correctness, completeness, risk) with a
merged rubric; failures route to blocked-escalation (the only human gate
inside goal mode besides clarify).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

# --- Drift watchdog ---

@dataclass
class DriftSignal:
    thread_id: str
    kind: str  # "rate_drop" | "stall"
    detail: str
    severity: str  # "warn" | "alert"


@dataclass
class DriftWatchdog:
    """Detects drifting threads (event + time based)."""
    # A thread drifts if its tool-success rate drops by more than this over
    # the last N tool calls.
    rate_drop_threshold: float = 0.20
    rate_window: int = 20
    # A thread stalls if it's been running with no new event for this long.
    stall_timeout_s: float = 300.0  # 5 min

    def check(self, thread_id: str, *, tool_success_rates: list[float],
               last_event_age_s: float) -> DriftSignal | None:
        # Event-based: rate drop over the window
        if len(tool_success_rates) >= self.rate_window:
            recent = tool_success_rates[-self.rate_window:]
            first_half = recent[: len(recent) // 2]
            second_half = recent[len(recent) // 2:]
            f_avg = sum(first_half) / len(first_half) if first_half else 0.0
            s_avg = sum(second_half) / len(second_half) if second_half else 0.0
            if f_avg - s_avg > self.rate_drop_threshold:
                return DriftSignal(
                    thread_id, "rate_drop",
                    f"success rate dropped {f_avg:.2f} -> {s_avg:.2f}",
                    "alert",
                )
        # Time-based: stall
        if last_event_age_s > self.stall_timeout_s:
            return DriftSignal(
                thread_id, "stall",
                f"no event for {last_event_age_s:.0f}s",
                "warn",
            )
        return None


# --- Collision radar v1 (warn-only) ---

@dataclass
class CollisionWarning:
    thread_a: str
    thread_b: str
    repo: str
    severity: str = "warn"  # v1 is always warn-only


@dataclass
class CollisionRadar:
    """Warns when two threads' repo scopes overlap. v1: warn-only."""

    def check(self, threads: list[dict[str, Any]]) -> list[CollisionWarning]:
        """threads: [{thread_id, repo_scope}]. Returns warnings for overlaps."""
        warnings: list[CollisionWarning] = []
        seen: dict[str, str] = {}
        for t in threads:
            repo = t.get("repo_scope")
            tid = t.get("thread_id", "?")
            if not repo:
                continue
            if repo in seen:
                warnings.append(CollisionWarning(seen[repo], tid, repo))
            else:
                seen[repo] = tid
        return warnings


# --- Budget reminders ---

@dataclass
class BudgetReminder:
    thread_id: str
    pct: float
    level: str  # "50" | "80"


@dataclass
class BudgetWatchdog:
    """Fires reminders at 50% and 80% of budget. Never auto-stops."""
    fired_50: set[str] = field(default_factory=set)
    fired_80: set[str] = field(default_factory=set)

    def check(self, thread_id: str, *, used: float, cap: float) -> BudgetReminder | None:
        if cap <= 0:
            return None
        pct = used / cap
        if pct >= 0.80 and thread_id not in self.fired_80:
            self.fired_80.add(thread_id)
            return BudgetReminder(thread_id, pct, "80")
        if pct >= 0.50 and thread_id not in self.fired_50:
            self.fired_50.add(thread_id)
            return BudgetReminder(thread_id, pct, "50")
        return None


# --- Idle-gate timers ---

@dataclass
class IdleGate:
    """Surfaces idle threads past their TTL to the human."""
    idle_ttl_s: float = 600.0  # 10 min

    def check(self, thread_id: str, *, status: str, idle_for_s: float) -> bool:
        """True if the thread is idle past TTL (candidate for completion)."""
        return status == "idle" and idle_for_s > self.idle_ttl_s


# --- Critic×3 merged rubric (plan §8) ---

class CriticDimension(str, Enum):
    CORRECTNESS = "correctness"
    COMPLETENESS = "completeness"
    RISK = "risk"


@dataclass
class CriticFinding:
    dimension: CriticDimension
    severity: str  # "block" | "warn"
    detail: str


@dataclass
class CriticRubric:
    """Three critic passes with a merged rubric. A 'block' finding routes to
    blocked-escalation (the only human gate inside goal mode besides clarify)."""
    def evaluate(self, *, plan: dict[str, Any] | None, evidence: dict[str, Any] | None,
                  diff_summary: str | None) -> list[CriticFinding]:
        findings: list[CriticFinding] = []
        # Correctness: does the evidence show the success criteria are met?
        if evidence is None or not evidence.get("tests_pass"):
            findings.append(CriticFinding(
                CriticDimension.CORRECTNESS, "block",
                "tests do not pass — success criteria not verified",
            ))
        # Completeness: does the diff cover the plan's steps?
        if plan and plan.get("steps") and (not diff_summary or not diff_summary.strip()):
            findings.append(CriticFinding(
                CriticDimension.COMPLETENESS, "block",
                "diff is empty but plan has steps — incomplete",
            ))
        # Risk: any destructive or large-scope change?
        if diff_summary and ("force" in diff_summary.lower() or len(diff_summary) > 10000):
            findings.append(CriticFinding(
                CriticDimension.RISK, "warn",
                "large or force-bearing change — review carefully",
            ))
        return findings

    def should_block(self, findings: list[CriticFinding]) -> tuple[bool, str]:
        blocks = [f for f in findings if f.severity == "block"]
        if blocks:
            return True, "; ".join(f.detail for f in blocks)
        return False, ""


__all__ = [
    "BudgetReminder",
    "BudgetWatchdog",
    "CollisionRadar",
    "CollisionWarning",
    "CriticDimension",
    "CriticFinding",
    "CriticRubric",
    "DriftSignal",
    "DriftWatchdog",
    "IdleGate",
]
