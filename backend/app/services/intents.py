"""Intent bus (plan §1a hybrid interaction): buttons AND typing are first-class —
both feed ONE bus. Typed text is classified against the CURRENT available_actions
only (tiny legal move set = near-perfect interpretation).

Safety rule: read-only intents (questions, nudges, 'show diff') execute immediately
from text; state-changing intents from text resolve to a CONFIRMATION CARD while
buttons execute directly. Irreversible intents always require confirmed=true.
"""

from __future__ import annotations

from zagent_contracts import IRREVERSIBLE_INTENTS, ActionKind, IntentSource, UserIntent

from app.db.base import get_session
from app.db.models.run import Run

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
        if kind.value in available and any(p in lowered for p in phrases):
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
    # Lane controls are always legal (§1a/§4: per-lane stop/nudge/pin/kill-replace
    # stay available while the agent works — they target a lane, not the run stage).
    legal = set(run.available_actions) | {ActionKind.NUDGE.value, ActionKind.SEND_MESSAGE.value,
                                          ActionKind.STOP_RUN.value, ActionKind.ABANDON_RUN.value,
                                          ActionKind.STOP_LANE.value, ActionKind.PIN_FINDING.value,
                                          ActionKind.KILL_REPLACE.value, ActionKind.LET_IT_RUN.value}
    if intent.intent.value not in legal:
        raise ValueError(f"intent {intent.intent.value} not legal in stage {run.stage}")


def load_run_for_user(run_id: str, user_id: int) -> Run | None:
    """§7a hard scope: sessions are PRIVATE — a user can only touch their own runs."""
    session = get_session()
    try:
        run = session.get(Run, run_id)
        if run is None or run.created_by != user_id:
            return None
        return run
    finally:
        session.close()
