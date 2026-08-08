"""Guardian: CI failure on a Collegium PR → a gated fix run, under
a circuit breaker that is CODE, never prompt (the liver rule — a breaker the
agent could talk its way past is not a breaker).

Breaker rules:
  * max settings.guardian_max_attempts runs per PR per 24h (default 3).
  * REPEATED-SIGNATURE HALT: if the latest Guardian run for this PR carried the
    same failure signature, stop — the agent is retrying an identical failure.
Verdicts are recorded on the trigger_events log like every engine outcome.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

from collegium_contracts.triggers import TriggerEvent

from app.core.config import get_settings
from app.core.logging import get_logger
from app.db.base import get_session
from app.db.models.trigger import TriggerEventLog
from app.services import identity

log = get_logger(service="guardian")


def failure_signature(payload: dict) -> str:
    """What makes this CI failure THIS failure: repo + pipeline definition +
    the sorted set of failed task names. Same inputs, same signature."""
    tasks = sorted(str(t) for t in (payload.get("failed_tasks") or []))
    raw = "|".join([str(payload.get("repo", "")), str(payload.get("definition", "")), *tasks])
    return hashlib.sha1(raw.encode()).hexdigest()[:12]


def should_attempt(pr_id: int, signature: str, trigger_name: str) -> tuple[bool, str]:
    """The circuit breaker. Returns (allowed, halt_reason)."""
    session = get_session()
    try:
        since = datetime.now(UTC) - timedelta(hours=24)
        logs = (session.query(TriggerEventLog)
                .filter(TriggerEventLog.status == "matched",
                        TriggerEventLog.received_at >= since)
                .order_by(TriggerEventLog.id).all())
        # H-25: a matched log row now stores its per-trigger verdicts under
        # payload["verdicts"] (each carrying trigger + log_payload with
        # pr_id/failure_signature). The old code read top-level
        # payload.trigger_name/pr_id, which is gone, so the breaker never
        # counted attempts and never tripped. Count from the verdicts list
        # with a legacy fallback to the top-level fields.
        attempts: list[dict] = []
        for entry in logs:
            payload = entry.payload or {}
            verdicts = payload.get("verdicts")
            if verdicts:
                for v in verdicts:
                    lp = v.get("log_payload") or {}
                    if v.get("trigger") == trigger_name and lp.get("pr_id") == pr_id:
                        attempts.append({"failure_signature": lp.get("failure_signature")})
            elif payload.get("trigger_name") == trigger_name and payload.get("pr_id") == pr_id:
                attempts.append({"failure_signature": payload.get("failure_signature")})
        if len(attempts) >= get_settings().guardian_max_attempts:
            return False, "max_attempts"
        if attempts and attempts[-1]["failure_signature"] == signature:
            return False, "repeated_signature"
        return True, ""
    finally:
        session.close()


async def guard(event: TriggerEvent, trigger, run_manager) -> dict:
    """Engine handler for build.failed events: breaker check, then a gated
    system-owned fix run with the failure digest as the task."""
    pr_id = event.payload.get("pr_id")
    if pr_id is None:
        return {"status": "ignored", "reason": "no_pr_link"}
    signature = failure_signature(event.payload)
    allowed, reason = should_attempt(int(pr_id), signature, trigger.name)
    if not allowed:
        log.info("guardian halted", pr_id=pr_id, reason=reason, signature=signature)
        return {"status": "halted", "reason": reason,
                "log_payload": {"pr_id": pr_id, "failure_signature": signature}}
    failed = ", ".join(event.payload.get("failed_tasks") or []) or "unknown step"
    task = (f"CI failed on PR {pr_id} ({event.payload.get('repo', '?')}, "
            f"{event.payload.get('definition', 'pipeline')}): {failed}. "
            "Fetch the failing logs, fix the cause in the PR branch, push.")
    run = await run_manager.create_run(
        source="trigger", initiated_by=identity.system_user_id(),
        mode_name=trigger.mode, task=task, autonomy="gated")
    return {"status": "started", "run_id": run.id,
            "log_payload": {"pr_id": pr_id, "failure_signature": signature}}
