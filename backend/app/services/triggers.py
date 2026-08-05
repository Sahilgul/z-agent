"""Triggers engine (triggers-as-data).

ONE generic ingress normalizes sources into contracts.TriggerEvent; the engine
matches events against `triggers` ROWS — the ADO state vocabulary lives in row
filters, never in code. A new state is config; a new source is one new
normalizer. Runs are created with created_by = the resolving human (their
inbox, their steering) — identity resolution is FAIL-CLOSED.

Four non-negotiable guardrails:
  1. LOOP PREVENTION — events changed_by the service account's own descriptor
     are ignored (agent acts → webhook fires → ∞).
  2. STATE FLAPPING — one active run per (work item, trigger); repeats within
     settings.trigger_flap_window_minutes coalesce into a NUDGE, not a new run.
  3. BULK-EDIT BLAST — per-trigger rate limit; overflow events queue
     (status='queued') and drain_queued() starts them as capacity returns.
  4. TRUST — triggered runs are GATED, never Autonomous, whatever the row says
     (the trigger surface is attacker-influenceable).
"""

from __future__ import annotations

import hmac
import hashlib
from datetime import datetime, timedelta, timezone

from sqlalchemy.exc import IntegrityError

from app.core.config import get_settings
from app.core.logging import get_logger
from app.db.base import get_session
from app.db.models.thread import Thread
from app.db.models.run import Run
from app.db.models.trigger import Trigger, TriggerEventLog, TriggerEventVerdict
from app.services import identity
from app.services.runs import TERMINAL_STAGES
from zagent_contracts.triggers import TriggerEvent, TriggerSource

log = get_logger(service="triggers")


class TriggerError(ValueError):
    pass


# --------------------------------------------------------------- normalizers
def normalize_ado_work_item(body: dict) -> TriggerEvent:
    """ADO service-hook 'workitem.updated' payload -> canonical TriggerEvent.
    This is the ENTIRE ADO-specific surface; the engine below is source-blind."""
    resource = body.get("resource") or {}
    fields = (resource.get("fields") or {})
    revised_by = resource.get("revisedBy") or {}
    work_item_id = resource.get("workItemId") or resource.get("id")
    if work_item_id is None:
        raise TriggerError("payload carries no work item id")
    revision = int(resource.get("rev") or 0)
    state = fields.get("System.State") or (
        (fields.get("System.State") or {}).get("newValue") if isinstance(fields.get("System.State"), dict) else None)
    # ADO update payloads carry changed fields under 'fields' with old/new
    changed = resource.get("fields") or {}
    new_state = None
    if isinstance(changed.get("System.State"), dict):
        new_state = changed["System.State"].get("newValue")
    title = ""
    if isinstance(changed.get("System.Title"), dict):
        title = changed["System.Title"].get("newValue") or ""
    return TriggerEvent(
        source=TriggerSource.ADO_WEBHOOK,
        external_id=str(work_item_id),
        revision=revision,
        event_type="work_item.updated",
        changed_by_descriptor=(revised_by.get("id") if isinstance(revised_by, dict) else None),
        payload={
            "state": new_state or state,
            "title": title,
            "url": (resource.get("_links") or {}).get("html", {}).get("href", "")
            if isinstance(resource.get("_links"), dict) else "",
        },
    )


def normalize_ado_build(body: dict) -> TriggerEvent:
    """ADO build.completed (failed) payload -> TriggerEvent for the Guardian.
    external_id is the BUILD id (each failed build is one event); the PR the
    Guardian guards rides in the payload."""
    resource = body.get("resource") or {}
    build_id = resource.get("id")
    if build_id is None:
        raise TriggerError("payload carries no build id")
    result = str(resource.get("result") or "").lower()
    pr_id = (resource.get("triggerInfo") or {}).get("pr.number") or resource.get("pr_id")
    timeline = resource.get("timeline") or {}
    failed_tasks = [t.get("name") for t in (timeline.get("tasks") or [])
                    if str(t.get("result", "")).lower() == "failed" and t.get("name")]
    return TriggerEvent(
        source=TriggerSource.ADO_WEBHOOK,
        external_id=str(build_id),
        revision=0,
        event_type="build.failed",
        changed_by_descriptor=None,  # pipelines have no human author — no loop risk, no owner
        payload={
            "result": result,
            "pr_id": int(pr_id) if pr_id is not None else None,
            "repo": resource.get("repository", {}).get("name", "")
            if isinstance(resource.get("repository"), dict) else str(resource.get("repository", "")),
            "definition": (resource.get("definition") or {}).get("name", "")
            if isinstance(resource.get("definition"), dict) else "",
            "failed_tasks": failed_tasks,
        },
    )


def normalize_ado_pr_comment(body: dict) -> TriggerEvent:
    """ADO PR thread/comment payload -> TriggerEvent for the Responder.
    Dedupe on (pr_id, comment_id); the comment author needs no Zagent identity."""
    resource = body.get("resource") or {}
    comment = resource.get("comment") or resource
    pr = resource.get("pullRequest") or {}
    pr_id = pr.get("pullRequestId") or resource.get("pullRequestId")
    comment_id = comment.get("id") or resource.get("commentId")
    if pr_id is None or comment_id is None:
        raise TriggerError("payload carries no pr/comment id")
    author = (comment.get("author") or {})
    return TriggerEvent(
        source=TriggerSource.ADO_WEBHOOK,
        external_id=str(pr_id),
        revision=int(comment_id),
        event_type="pr.comment",
        changed_by_descriptor=(author.get("id") if isinstance(author, dict) else None),
        payload={
            "pr_id": int(pr_id),
            "text": comment.get("content", ""),
            "author": (author.get("displayName") if isinstance(author, dict) else "") or "",
        },
    )


def normalize_ado_pr_created(body: dict) -> TriggerEvent:
    """ADO git.pullrequest.created -> TriggerEvent for the Review-bot.
    Dedupe on (pr_id, lastMergeSourceCommit) so a push-update re-reviews."""
    resource = body.get("resource") or {}
    pr_id = resource.get("pullRequestId")
    if pr_id is None:
        raise TriggerError("payload carries no pull request id")
    commit = ((resource.get("lastMergeSourceCommit") or {}).get("commitId")
              or "0")[:8]
    return TriggerEvent(
        source=TriggerSource.ADO_WEBHOOK,
        external_id=str(pr_id),
        revision=0,  # created fires once per PR; updates are a separate ADO event
        event_type="pr.created",
        changed_by_descriptor=(resource.get("createdBy") or {}).get("id")
        if isinstance(resource.get("createdBy"), dict) else None,
        payload={
            "pr_id": int(pr_id),
            "title": resource.get("title", ""),
            "repo": ((resource.get("repository") or {}).get("name", "")
                     if isinstance(resource.get("repository"), dict) else ""),
            "head_commit": commit,
        },
    )


# Handler-routed event types (liver rule): the trigger ROW still owns
# enablement, vocabulary, and rate limits; these own the mechanics. A handler
# returning a verdict replaces the default start-a-new-run path, and the
# engine SKIPS owner resolution for routed events (attribution is settled by
# the handler — Guardian is system-owned, Responder continues the owner's run).
def _event_handlers():
    from app.services import guardian, responder
    return {"build.failed": guardian.guard, "pr.comment": responder.respond}


# -------------------------------------------------------------------- engine
def _matches(trigger: Trigger, event: TriggerEvent) -> bool:
    """Row filters own the vocabulary: every key in filter_json must equal the
    event's field — event_type plus any payload keys (e.g. state=zagent-plan)."""
    f = trigger.filter_json or {}
    if "event_type" in f and f["event_type"] != event.event_type:
        return False
    for key, want in f.items():
        if key == "event_type":
            continue
        if event.payload.get(key) != want:
            return False
    return True


def _log_event(event: TriggerEvent) -> TriggerEventLog | None:
    """Insert the dedupe row FIRST — the unique (source, external_id, revision)
    constraint is the idempotency contract. Returns None on a duplicate."""
    session = get_session()
    try:
        row = TriggerEventLog(
            source=event.source.value, external_id=event.external_id,
            revision=event.revision, event_type=event.event_type,
            changed_by_descriptor=event.changed_by_descriptor,
            payload=event.payload, status="received",
        )
        session.add(row)
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            return None
        session.refresh(row)
        session.expunge(row)
        return row
    finally:
        session.close()


def _set_log(log_id: int, **fields) -> None:
    session = get_session()
    try:
        row = session.get(TriggerEventLog, log_id)
        for key, value in fields.items():
            setattr(row, key, value)
        session.commit()
    finally:
        session.close()


def _persist_verdicts(log_id: int, verdicts: list[dict]) -> None:
    """M-39 (coord D): write one TriggerEventVerdict row per verdict so the
    rate-limit check can be scoped by trigger_name at DB level. Idempotent:
    a re-run for the same log replaces the verdict rows rather than
    duplicating (so a retried process() doesn't double-count)."""
    session = get_session()
    try:
        session.query(TriggerEventVerdict).filter_by(log_id=log_id).delete()
        for v in verdicts:
            session.add(TriggerEventVerdict(
                log_id=log_id,
                trigger_name=str(v.get("trigger")),
                status=str(v.get("status", "ignored")),
                run_id=v.get("run_id"),
            ))
        session.commit()
    finally:
        session.close()


def _is_loop(event: TriggerEvent) -> bool:
    own = get_settings().service_account_descriptor
    return bool(own) and event.changed_by_descriptor == own


def _recent_active_run(trigger: Trigger, event: TriggerEvent) -> str | None:
    """Guardrail 2: an active run started by this trigger for this work item
    within the flap window -> coalesce into a nudge."""
    window = datetime.now(timezone.utc) - timedelta(
        minutes=get_settings().trigger_flap_window_minutes)
    session = get_session()
    try:
        logs = (session.query(TriggerEventLog)
                .filter_by(external_id=event.external_id, status="matched")
                .filter(TriggerEventLog.run_id.isnot(None),
                        TriggerEventLog.received_at >= window)
                .order_by(TriggerEventLog.id.desc()).all())
        for entry in logs:
            run = session.get(Run, entry.run_id)
            if run and run.stage not in TERMINAL_STAGES:
                # H-26: scope the coalescence to THIS trigger. The old code
                # coalesced into ANY active run for the work item, so a
                # dev-mode trigger nudged a plan-mode run (wrong mode) and
                # one trigger's flap window captured another trigger's run.
                # Match the run's mode to the trigger's mode AND require the
                # prior matched log to have been started by this trigger
                # (verdicts list, or legacy top-level trigger_name).
                if run.mode != trigger.mode:
                    continue
                payload = entry.payload or {}
                verdicts = payload.get("verdicts")
                if verdicts:
                    if not any(v.get("trigger") == trigger.name
                              and v.get("status") == "started"
                              and v.get("run_id") == run.id for v in verdicts):
                        continue
                elif payload.get("trigger_name") != trigger.name:
                    continue
                return run.id
        return None
    finally:
        session.close()


def _rate_limited(trigger) -> bool:
    """Guardrail 3: runs this trigger started in the last hour vs its row cap.
    M-39 (coord D): scoped at DB level by trigger_name (indexed child table)
    instead of loading every matched log and counting in Python. A single
    trigger_events row can carry verdicts for multiple triggers (H-25), so
    the per-trigger count is correct under a multi-trigger blast — each
    started verdict is its own row. The window is the event's received_at
    (joined from the log row), matching the original semantic; the
    server-side COUNT keeps the work off the Python loop."""
    since = datetime.now(timezone.utc) - timedelta(hours=1)
    cap = trigger.rate_limit_per_hour
    session = get_session()
    try:
        # Only STARTED verdicts count — a queued event must not hold its own
        # slot, or it could never drain. Scope by the indexed trigger_name on
        # the child table; join to the log for the received_at window.
        count = (session.query(TriggerEventVerdict)
                 .join(TriggerEventLog, TriggerEventLog.id == TriggerEventVerdict.log_id)
                 .filter(TriggerEventVerdict.trigger_name == trigger.name,
                         TriggerEventVerdict.status == "started",
                         TriggerEventLog.received_at >= since)
                 .count())
        return count >= cap
    finally:
        session.close()


async def process(event: TriggerEvent, run_manager) -> dict:
    """One event through the engine. Returns a verdict dict; the TriggerEventLog
    row is the durable record (admin metadata makes every verdict visible)."""
    row = _log_event(event)
    if row is None:
        return {"status": "duplicate"}
    log_id = row.id

    if _is_loop(event):  # guardrail 1
        _set_log(log_id, status="ignored")
        return {"status": "ignored", "reason": "loop_prevention"}

    session = get_session()
    try:
        triggers = (session.query(Trigger)
                    .filter_by(enabled=True, source=event.source.value).all())
        matched = [t for t in triggers if _matches(t, event)]
        # detach everything the rest of the flow needs
        matched = [{"id": t.id, "name": t.name, "mode": t.mode,
                    "autonomy": t.autonomy, "owner_resolution": t.owner_resolution,
                    "rate_limit_per_hour": t.rate_limit_per_hour} for t in matched]
    finally:
        session.close()
    if not matched:
        _set_log(log_id, status="ignored")
        return {"status": "ignored", "reason": "no_trigger_row"}

    verdicts = []
    handlers = _event_handlers()
    for t in matched:
        trigger = _RowView(t)
        handler = handlers.get(event.event_type)
        owner_id = None
        if handler is None:
            # identity: fail-closed; system only by explicit row config. Handler-
            # routed events skip this — the handler owns attribution (Guardian is
            # system-owned; the Responder continues the original owner's run).
            if trigger.owner_resolution == "system":
                owner_id = identity.system_user_id()
            else:
                owner_id = identity.resolve_descriptor(event.changed_by_descriptor)
            if owner_id is None:
                verdicts.append({"trigger": trigger.name, "status": "failed",
                                 "reason": "unresolved_identity"})
                continue

        active = _recent_active_run(trigger, event)  # guardrail 2
        if active is not None:
            thread_id = _lead_thread_id(active)
            if thread_id:
                await run_manager.nudge_thread(active, thread_id, _nudge_text(event))
            verdicts.append({"trigger": trigger.name, "status": "nudged",
                             "run_id": active, "resolved_user_id": owner_id})
            continue

        if _rate_limited(trigger):  # guardrail 3 — queue, drain later
            verdicts.append({"trigger": trigger.name, "status": "queued",
                             "resolved_user_id": owner_id})
            continue

        if handler is not None:
            verdict = await handler(event, trigger, run_manager)
            extra = verdict.pop("log_payload", {})
            verdicts.append({"trigger": trigger.name, **verdict,
                             "log_payload": {**event.payload, "trigger_name": trigger.name, **extra}})
            continue

        run = await run_manager.create_run(  # guardrail 4: GATED, always
            source="trigger", initiated_by=owner_id, mode_name=trigger.mode,
            task=_task_text(event), work_item_id=int(event.external_id),
            autonomy="gated")
        verdicts.append({"trigger": trigger.name, "status": "started",
                         "run_id": run.id, "resolved_user_id": owner_id})
    # H-25: write the dedupe log ONCE with ALL verdicts. The old code called
    # _set_log per trigger, overwriting the single row — only the last
    # trigger's outcome survived, so the audit trail lost every earlier
    # trigger and _rate_limited undercounted (idempotency loss). Each
    # verdict carries its own trigger name + run_id; the payload keeps a
    # per-trigger trigger_name map for legacy readers. The row STATUS must
    # reflect the verdicts: all-queued -> "queued" (so drain_queued finds
    # it), all-failed -> "failed", else "matched" (started/nudged).
    started = [v for v in verdicts if v.get("status") == "started"]
    if not verdicts:
        status = "ignored"
    elif all(v.get("status") == "queued" for v in verdicts):
        status = "queued"
    elif all(v.get("status") == "failed" for v in verdicts):
        status = "failed"
    else:
        status = "matched"
    # G-19: persist resolved_user_id onto the log ROW (not just inside the
    # verdict payload). drain_queued reads q.resolved_user_id to start the
    # run as the original owner; before this the column stayed NULL for a
    # queued event (the owner id lived only in payload["verdicts"][*]), so a
    # drained run was initiated_by=None — it landed in nobody's inbox and
    # lost the resolver's steering. Take it from the first verdict that
    # carries one (every verdict sets resolved_user_id).
    resolved_user_id = next(
        (v.get("resolved_user_id") for v in verdicts
         if v.get("resolved_user_id") is not None), None)
    _set_log(log_id,
             status=status,
             run_id=started[0]["run_id"] if started else None,
             resolved_user_id=resolved_user_id,
             payload={"verdicts": verdicts, **event.payload})
    # M-39 (coord D): persist one child row per verdict so _rate_limited can
    # scope by trigger_name at DB level (indexed) instead of loading every
    # matched log and counting in Python. A single log row can carry verdicts
    # for multiple triggers (H-25), so the per-trigger association can't be
    # a scalar on the log row.
    _persist_verdicts(log_id, verdicts)
    return {"status": status, "verdicts": verdicts}


class _RowView:
    """Detached trigger-row data; the engine never touches the ORM outside a session."""

    def __init__(self, d: dict) -> None:
        self.id = d["id"]
        self.name = d["name"]
        self.mode = d["mode"]
        self.autonomy = d["autonomy"]
        self.owner_resolution = d["owner_resolution"]
        self.rate_limit_per_hour = d["rate_limit_per_hour"]


def _lead_thread_id(run_id: str) -> str | None:
    session = get_session()
    try:
        thread = (session.query(Thread).filter_by(run_id=run_id)
                .filter(Thread.status.in_(["running", "queued"]))
                .order_by(Thread.created_at).first())
        return thread.id if thread else None
    finally:
        session.close()


def _task_text(event: TriggerEvent) -> str:
    title = event.payload.get("title") or ""
    if event.event_type.startswith("pr."):
        return (f"Review PR {event.payload.get('pr_id')} in "
                f"{event.payload.get('repo', '?')}: '{title}'. Fetch the diff via "
                "the ADO tools and post findings as PR comments. Read-only: do "
                "not modify code.")
    return f"ADO work item {event.external_id}: {title}".strip()


def _nudge_text(event: TriggerEvent) -> str:
    return (f"The triggering work item changed again (rev {event.revision}, "
            f"state: {event.payload.get('state', '?')}). Fold the new state into "
            "your current work — do not restart.")


def _queued_trigger_name(payload: dict | None) -> str | None:
    """H-25: a queued log row stores its verdict under payload["verdicts"][*]
    ("trigger"); legacy rows stored it at top-level payload["trigger_name"]."""
    if not payload:
        return None
    verdicts = payload.get("verdicts")
    if verdicts:
        for v in verdicts:
            if v.get("status") == "queued":
                return v.get("trigger")
        return verdicts[0].get("trigger") if verdicts else None
    return payload.get("trigger_name")


async def drain_queued(run_manager, limit: int = 5) -> list[dict]:
    """Start queued (rate-limited) events as capacity returns — oldest first,
    each still under its trigger's hourly cap."""
    session = get_session()
    try:
        queued = (session.query(TriggerEventLog).filter_by(status="queued")
                  .order_by(TriggerEventLog.id).limit(limit).all())
        items = [{
            "log_id": q.id, "external_id": q.external_id,
            # H-25: the trigger name now lives under payload["verdicts"][*]
            # ("trigger"), with a legacy fallback to top-level trigger_name.
            "trigger_name": _queued_trigger_name(q.payload),
            "resolved_user_id": q.resolved_user_id,
            "revision": q.revision, "payload": q.payload,
            "event_type": q.event_type,
        } for q in queued]
    finally:
        session.close()
    started = []
    for item in items:
        session = get_session()
        try:
            trigger = session.query(Trigger).filter_by(name=item["trigger_name"]).one_or_none()
            if trigger is None:
                continue
            view = _RowView({"id": trigger.id, "name": trigger.name, "mode": trigger.mode,
                             "autonomy": trigger.autonomy,
                             "owner_resolution": trigger.owner_resolution,
                             "rate_limit_per_hour": trigger.rate_limit_per_hour})
        finally:
            session.close()
        if _rate_limited(view):
            continue
        event = TriggerEvent(
            source=TriggerSource.ADO_WEBHOOK, external_id=item["external_id"],
            revision=item["revision"],
            # H-27: use the queued event's ACTUAL type — the old code hardcoded
            # "work_item.updated", so a queued build/pr event drained into the
            # work_item handler (wrong task text, wrong routing).
            event_type=item["event_type"] or "work_item.updated",
            payload=item["payload"] or {})
        run = await run_manager.create_run(
            source="trigger", initiated_by=item["resolved_user_id"],
            mode_name=view.mode, task=_task_text(event),
            work_item_id=int(item["external_id"]), autonomy="gated")
        _set_log(item["log_id"], status="matched", run_id=run.id)
        started.append({"log_id": item["log_id"], "run_id": run.id})
    return started


# ------------------------------------------------------------------ signature
def verify_signature(body: bytes, signature: str | None) -> bool:
    """HMAC-SHA256 over the raw body. Fail-closed: no configured secret or no
    header = reject. Compare in constant time."""
    secret = get_settings().ado_webhook_secret
    if not secret or not signature:
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature.removeprefix("sha256="))
