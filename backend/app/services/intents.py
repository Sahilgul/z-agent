"""Intent bus (hybrid interaction): buttons AND typing are first-class —
both feed ONE bus. Typed text is classified against the CURRENT available_actions
only (tiny legal move set = near-perfect interpretation).

Safety rule: read-only intents (questions, nudges, 'show diff') execute immediately
from text; state-changing intents from text resolve to a CONFIRMATION CARD while
buttons execute directly. Irreversible intents always require confirmed=true.
"""

from __future__ import annotations

import re

from collegium_contracts import (
    IRREVERSIBLE_INTENTS,
    ActionKind,
    IntentSource,
    UserIntent,
)

from app.db.base import get_session
from app.db.models.run import Run
from app.db.models.thread import Thread

READ_ONLY_INTENTS = frozenset({
    ActionKind.REVIEW_PLAN, ActionKind.REVIEW_EVIDENCE, ActionKind.REVIEW_DIFF,
    ActionKind.NUDGE, ActionKind.SEND_MESSAGE, ActionKind.LET_IT_RUN,
    ActionKind.PIN_FINDING, ActionKind.ASK_COUNSEL, ActionKind.SUMMARIZE_THREAD,
})


class IntentNeedsConfirmation(Exception):
    def __init__(self, intent: UserIntent) -> None:
        self.intent = intent
        super().__init__(f"intent {intent.intent.value} requires confirmation")


def classify_text(run: Run, text: str) -> UserIntent | None:
    """Map free text onto the run's CURRENT legal move set. Returns None when the
    text is a plain message (goes to the Lead as conversation)."""
    lowered = text.strip().lower()
    available = set(run.available_actions)
    keyword_map = {
        ActionKind.APPROVE_PLAN: ("approve", "lgtm", "looks good", "go ahead"),
        ActionKind.REJECT_PLAN: ("reject", "no, redo", "try again"),
        ActionKind.MERGE_PR: ("merge", "ship it", "mark done"),
        ActionKind.CREATE_PR: ("create pr", "open pr", "make the pr"),
        ActionKind.START_PLANNING: ("plan this", "start planning"),
        ActionKind.MOVE_TO_DEVELOPMENT: ("develop", "start development", "code it"),
        ActionKind.STOP_RUN: ("stop", "halt"),
    }
    for kind, phrases in keyword_map.items():
        # H-35: word-boundary match, not substring. The old `p in lowered`
        # made "disapprove" match "approve" (-> APPROVE_PLAN) and "no, redo"
        # match "no" — inverting user intent. \b on both ends of the phrase
        # requires a real word boundary, so "disapprove" no longer hits
        # "approve" and "I approve this" still does.
        if kind.value in available and any(
                re.search(rf"\b{re.escape(p)}\b", lowered) for p in phrases):
            return UserIntent(run_id=run.id, intent=kind, source=IntentSource.TEXT, text=text)
    return None


def gate_intent(run: Run, intent: UserIntent) -> None:
    """Raises IntentNeedsConfirmation when a state-changing intent arrives as
    unconfirmed TEXT; raises ValueError when the intent isn't a legal move."""
    if intent.intent in IRREVERSIBLE_INTENTS and not intent.confirmed:
        raise IntentNeedsConfirmation(intent)
    if intent.source in (IntentSource.TEXT, IntentSource.VOICE):
        if intent.intent not in READ_ONLY_INTENTS and not intent.confirmed:
            raise IntentNeedsConfirmation(intent)
    # Thread controls are always legal (per-thread stop/nudge/pin/kill-replace
    # stay available while the agent works — they target a thread, not the run stage).
    # SWITCH_MODE is always legal too: it sets run.mode for the next send, never
    # touches in-flight work, so no stage gates it.
    legal = set(run.available_actions) | {ActionKind.NUDGE.value, ActionKind.SEND_MESSAGE.value,
                                          ActionKind.STOP_RUN.value, ActionKind.ABANDON_RUN.value,
                                          ActionKind.STOP_THREAD.value, ActionKind.PIN_FINDING.value,
                                          ActionKind.KILL_REPLACE.value, ActionKind.LET_IT_RUN.value,
                                          ActionKind.SWITCH_MODE.value}
    if intent.intent.value not in legal:
        raise ValueError(f"intent {intent.intent.value} not legal in stage {run.stage}")


def load_run_for_user(run_id: str, user_id: int) -> Run | None:
    """Hard scope: sessions are PRIVATE — a user can only touch their own runs."""
    session = get_session()
    try:
        run = session.get(Run, run_id)
        if run is None or run.created_by != user_id:
            return None
        return run
    finally:
        session.close()


def load_thread_for_run(run_id: str, thread_id: str) -> Thread | None:
    """IDOR guard for per-thread controls: a thread is only actionable through
    the run that owns it. `load_run_for_user` already proved the run belongs to
    the requesting user; this proves the thread belongs to that run. Without
    it, /threads/{id}/nudge|stop and the STOP_THREAD/SEND_MESSAGE/NUDGE intents
    could target ANY thread by pairing the caller's own run_id with another
    user's thread_id (C-08..C-11)."""
    session = get_session()
    try:
        thread = session.get(Thread, thread_id)
        if thread is None or thread.run_id != run_id:
            return None
        return thread
    finally:
        session.close()
