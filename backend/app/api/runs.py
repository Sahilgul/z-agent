"""Run routes (THIN). POST /runs + POST /runs/{id}/intent are the single entry
points; §7a: every query hard-scopes by created_by = requesting user.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from zagent_contracts import ActionKind, IntentSource, UserIntent

from app.core.deps import current_user
from app.db.base import get_session
from app.db.models.lane import Lane
from app.db.models.run import Plan, Run
from app.db.models.user import User
from app.services import autonomy as autonomy_dial
from app.services import plans as plan_service
from app.services.intents import (
    IntentNeedsConfirmation, classify_text, gate_intent, load_run_for_user,
)
from app.services.sessions import replay_events

router = APIRouter(prefix="/runs", tags=["runs"])


def _persist_user_message(run_id: str, lane_id: str, text: str) -> dict | None:
    """Store the user's own message as a message event so the transcript is a
    real conversation — otherwise only the agent's side renders and follow-ups
    look like they vanished. role="user" lets the stream style it as the
    sender's bubble; seq rides the lane's counter like every other event.
    Returns the serialized event so the caller can push it live over WS."""
    from app.db.models.event import Event
    session = get_session()
    try:
        lane = session.get(Lane, lane_id)
        if lane is None:
            return None
        seq = lane.next_seq
        lane.next_seq = seq + 1
        session.add(Event(
            run_id=run_id, lane_id=lane_id, seq=seq,
            type="message", title=text[:120],
            payload={"text": text, "role": "user"}, sdk_message_uuid=None,
        ))
        session.commit()
        return {
            "run_id": run_id, "lane_id": lane_id, "seq": seq,
            "kind": "message", "title": text[:120],
            "detail": {"text": text, "role": "user"}, "sdk_message_uuid": None,
        }
    finally:
        session.close()


class CreateRunBody(BaseModel):
    mode: str = "ask"
    task: str
    repo: str | None = None
    work_item_id: int | None = None
    autonomy: str | None = None
    fanout: int | None = None  # §4 user-requested swarm width (Lead still decomposes)


class IntentBody(BaseModel):
    intent: str | None = None       # button/chip intents arrive pre-typed
    text: str | None = None         # typed/voice intents arrive as text
    source: str = "button"
    lane_id: str | None = None
    confirmed: bool = False
    payload: dict = {}


def _serialize(run: Run) -> dict:
    return {
        "id": run.id, "mode": run.mode, "autonomy": run.autonomy, "stage": run.stage,
        "title": run.title, "auto_summary": run.auto_summary, "repo": run.repo,
        "work_item_id": run.work_item_id, "available_actions": run.available_actions,
        "cost_usd": run.cost_usd, "tokens": run.tokens,
        "last_active_at": run.last_active_at.isoformat() if run.last_active_at else None,
        "created_at": run.created_at.isoformat() if run.created_at else None,
    }


@router.post("")
async def create_run(body: CreateRunBody, request: Request, user: User = Depends(current_user)):
    run_manager = request.app.state.run_manager
    try:
        run = await run_manager.create_run(
            source="button", initiated_by=user.id, mode_name=body.mode,
            task=body.task, repo=body.repo, work_item_id=body.work_item_id,
            autonomy=autonomy_dial.clamp(body.autonomy, user.id), fanout=body.fanout,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _serialize(run)


@router.get("")
def list_my_runs(user: User = Depends(current_user), repo: str | None = None,
                 stage: str | None = None, q: str | None = None):
    """Inbox = MY runs only (§7a) — filter by repo/mode/status, search by
    title/auto_summary."""
    session = get_session()
    try:
        query = session.query(Run).filter(Run.created_by == user.id)
        if repo:
            query = query.filter(Run.repo == repo)
        if stage:
            query = query.filter(Run.stage == stage)
        if q:
            like = f"%{q}%"
            query = query.filter(Run.title.ilike(like) | Run.auto_summary.ilike(like))
        runs = query.order_by(Run.last_active_at.desc()).limit(100).all()
        return [_serialize(r) for r in runs]
    finally:
        session.close()


@router.get("/{run_id}")
def get_run(run_id: str, user: User = Depends(current_user)):
    run = load_run_for_user(run_id, user.id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    return _serialize(run)


@router.get("/{run_id}/events")
def run_events(run_id: str, user: User = Depends(current_user), lane_id: str | None = None,
               after_seq: int | None = None):
    return replay_events(run_id, user.id, lane_id=lane_id, after_seq=after_seq)


@router.get("/{run_id}/lanes")
def run_lanes(run_id: str, user: User = Depends(current_user)):
    run = load_run_for_user(run_id, user.id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    session = get_session()
    try:
        lanes = session.query(Lane).filter_by(run_id=run_id).all()
        return [{
            "id": l.id, "persona": l.persona, "repo_scope": l.repo_scope,
            "status": l.status, "cost_usd": l.cost_usd, "budget_usd": l.budget_usd,
            "steps": l.next_seq, "forked_from_session_id": l.forked_from_session_id,
            "heartbeat_at": l.heartbeat_at.isoformat() if l.heartbeat_at else None,
            "has_container": l.container_id is not None,
            "created_at": l.created_at.isoformat() if l.created_at else None,
            "finished_at": l.finished_at.isoformat() if l.finished_at else None,
        } for l in lanes]
    finally:
        session.close()


@router.get("/{run_id}/plan")
def run_plan(run_id: str, user: User = Depends(current_user)):
    """Plan-approval card data (plan §2/§9): the latest Plan row + its steps.
    Hard-scoped to the run owner (§7a)."""
    run = load_run_for_user(run_id, user.id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    session = get_session()
    try:
        plan = (
            session.query(Plan)
            .filter_by(run_id=run_id)
            .order_by(Plan.created_at.desc(), Plan.id.desc())
            .first()
        )
        if plan is None:
            raise HTTPException(status_code=404, detail="no plan for this run")
        return {
            "id": plan.id, "run_id": plan.run_id, "status": plan.status,
            "structured": plan.structured,
            "decided_by": plan.decided_by,
            "decided_at": plan.decided_at.isoformat() if plan.decided_at else None,
            "created_at": plan.created_at.isoformat() if plan.created_at else None,
            "steps": [{
                "id": s.id, "index": s.index, "title": s.title,
                "description": s.description, "repo": s.repo, "files": s.files,
                "success_criterion": s.success_criterion, "status": s.status,
            } for s in plan.steps],
        }
    finally:
        session.close()


@router.get("/{run_id}/evidence")
def run_evidence(run_id: str, user: User = Depends(current_user)):
    """PR overlay data (§9): the tamper-proof evidence package — assembled from
    the DB at read time, with the sha256 the PR body pins. 404 before any plan
    exists (the overlay explains itself from that)."""
    run = load_run_for_user(run_id, user.id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    from app.services import delivery
    try:
        package = delivery.build_evidence_package(run_id)
    except delivery.DeliveryError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if not package["plan_steps"]:
        raise HTTPException(status_code=404, detail="no evidence package yet")
    package["sha256"] = delivery.evidence_sha256(package)
    return package


@router.post("/{run_id}/intent")
async def post_intent(run_id: str, body: IntentBody, request: Request,
                      user: User = Depends(current_user)):
    """THE single endpoint for button intents and classified text intents
    (plan §1a). Irreversible intents require confirmed=true."""
    run = load_run_for_user(run_id, user.id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    run_manager = request.app.state.run_manager

    if body.text and not body.intent:
        intent = classify_text(run, body.text)
        if intent is None:
            # Plain conversation: a nudge to the Lead lane (typed Lead-nudges
            # stay enabled while the agent works — §1a carve-out).
            intent = UserIntent(run_id=run_id, intent=ActionKind.SEND_MESSAGE,
                                source=IntentSource.TEXT, text=body.text,
                                lane_id=body.lane_id)
    else:
        # The frontend always sends intent="send_message" with the text as a
        # SEPARATE field — this branch must carry body.text through or every
        # follow-up message reaches the agent empty (the "no content" loop).
        intent = UserIntent(run_id=run_id, intent=ActionKind(body.intent),
                            source=IntentSource(body.source), lane_id=body.lane_id,
                            text=body.text,
                            confirmed=body.confirmed, payload=body.payload)

    try:
        gate_intent(run, intent)
    except IntentNeedsConfirmation as exc:
        return {"status": "confirm", "intent": exc.intent.intent.value,
                "card": f"{exc.intent.intent.value} — confirm?"}

    kind = intent.intent
    if kind == ActionKind.STOP_RUN:
        await run_manager.stop_run(run_id)
    elif kind == ActionKind.ABANDON_RUN:
        await run_manager.abandon_run(run_id)
    elif kind in (ActionKind.NUDGE, ActionKind.SEND_MESSAGE):
        lane_id = intent.lane_id or _lead_lane_id(run_id)
        if lane_id:
            # Mode switch takes effect here: if run.mode no longer matches the
            # mode the current lane was spawned under, chain the new blueprint
            # instead of nudging the old lane. The new lane resumes the prior
            # session volume (resume_from_lane, wired in lane_manager.spawn).
            session = get_session()
            try:
                lane = session.get(Lane, lane_id)
                spawned_mode = (lane.spawn_context or {}).get("mode") if lane else None
                run_mode = session.get(Run, run_id).mode if session.get(Run, run_id) else None
            finally:
                session.close()

            if (
                kind == ActionKind.SEND_MESSAGE
                and spawned_mode
                and run_mode
                and spawned_mode != run_mode
            ):
                # The user's message is the task for the new blueprint's
                # first lane; persist it as a user event so the transcript
                # shows the question before the new mode's answer.
                if intent.text:
                    _persist_user_message(run_id, lane_id, intent.text)
                extra = {"resume_from_lane_id": lane_id}
                if intent.text:
                    extra["task"] = intent.text
                await run_manager._run_blueprint(run_id, run_mode, extra_artifacts=extra)
            elif intent.text:
                user_event = _persist_user_message(run_id, lane_id, intent.text)
                # The persisted row bypasses the worker's Redis stream, so it
                # never reaches an open browser on its own — push it over the
                # run socket or the user's bubble only appears after a reload.
                if user_event is not None:
                    try:
                        from zagent_contracts import StepEvent, StepKind
                        await request.app.state.relay.publish_step(run_id, StepEvent(
                            run_id=user_event["run_id"], lane_id=user_event["lane_id"],
                            seq=user_event["seq"], kind=StepKind.MESSAGE,
                            title=user_event["title"], detail=user_event["detail"],
                            sdk_message_uuid=None,
                        ))
                    except Exception:  # WS fanout is best-effort; the row is durable
                        pass
                await run_manager.nudge_lane(run_id, lane_id, intent.text or "")
            else:
                await run_manager.nudge_lane(run_id, lane_id, intent.text or "")
    elif kind == ActionKind.APPROVE_PLAN:
        try:
            plan = plan_service.approve_plan(run_id, user.id)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        await run_manager.continue_to_development(run_id)
        return {"status": "ok", "intent": kind.value, "plan_id": plan.id,
                "plan_status": plan.status}
    elif kind == ActionKind.REJECT_PLAN:
        notes = intent.text or ""
        try:
            plan = plan_service.reject_plan(run_id, user.id, notes)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        await run_manager.replan(run_id, plan_service.critic_notes(plan))
        return {"status": "ok", "intent": kind.value, "plan_id": plan.id,
                "plan_status": plan.status}
    elif kind == ActionKind.CREATE_PR:
        link = await run_manager.create_pr(run_id)
        return {"status": "ok", "intent": kind.value, "pr_id": getattr(link, "ado_pr_id", None)}
    elif kind == ActionKind.MERGE_PR:
        handoff_url = await run_manager.merge_pr(run_id, user.id)
        # handoff_url is set only under merge_native_ui: the UI deep-links the
        # human into ADO's native complete screen (plan §9 merge-identity lock).
        return {"status": "ok", "intent": kind.value, "handoff_url": handoff_url}
    elif kind == ActionKind.START_PLAN:
        await run_manager.start_plan(run_id)
        return {"status": "ok", "intent": kind.value}
    elif kind == ActionKind.STOP_LANE:
        if not intent.lane_id:
            raise HTTPException(status_code=422, detail="stop_lane needs lane_id")
        await run_manager.stop_lane(run_id, intent.lane_id)
        return {"status": "ok", "intent": kind.value, "lane_id": intent.lane_id}
    elif kind == ActionKind.PIN_FINDING:
        if not intent.lane_id:
            raise HTTPException(status_code=422, detail="pin_finding needs lane_id")
        try:
            await run_manager.pin_finding(run_id, intent.lane_id,
                                          intent.payload.get("note", ""))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"status": "ok", "intent": kind.value, "lane_id": intent.lane_id}
    elif kind == ActionKind.KILL_REPLACE:
        if not intent.lane_id:
            raise HTTPException(status_code=422, detail="kill_replace needs lane_id")
        try:
            replacement = await run_manager.kill_replace_lane(run_id, intent.lane_id)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"status": "ok", "intent": kind.value, "lane_id": intent.lane_id,
                "replacement_lane_id": replacement.id}
    elif kind == ActionKind.SWITCH_MODE:
        mode_name = intent.payload.get("mode")
        if not mode_name:
            raise HTTPException(status_code=422, detail="switch_mode needs a mode in payload")
        try:
            await run_manager.switch_mode(run_id, str(mode_name))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"status": "ok", "intent": kind.value, "mode": mode_name}
    elif kind == ActionKind.LET_IT_RUN:
        # Watchdog card dismissal (§4): no lane action — the card clears and the
        # lane keeps working; recorded so the UI's dismissal is intentional.
        return {"status": "ok", "intent": kind.value}
    return {"status": "ok", "intent": kind.value}


def _lead_lane_id(run_id: str) -> str | None:
    session = get_session()
    try:
        lane = session.query(Lane).filter_by(run_id=run_id).order_by(Lane.created_at).first()
        return lane.id if lane else None
    finally:
        session.close()
