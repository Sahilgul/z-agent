import pytest
from collegium_contracts import ActionKind, IntentSource, RunStage, UserIntent

from app.db.models.run import Run
from app.services.intents import (
    READ_ONLY_INTENTS,
    IntentNeedsConfirmation,
    classify_text,
    gate_intent,
    load_run_for_user,
)


def _run(stage="awaiting_user", actions=None, run_id="r1", created_by=1):
    return Run(id=run_id, created_by=created_by, mode="ask", stage=stage,
               available_actions=actions or [])


# golden parity table: text -> ActionKind (only fires when the kind is legal)
GOLDEN_TEXT_CASES = [
    ("approve", ActionKind.APPROVE_PLAN, RunStage.AWAITING_USER),
    ("lgtm", ActionKind.APPROVE_PLAN, RunStage.AWAITING_USER),
    ("looks good to me", ActionKind.APPROVE_PLAN, RunStage.AWAITING_USER),
    ("go ahead", ActionKind.APPROVE_PLAN, RunStage.AWAITING_USER),
    ("reject this", ActionKind.REJECT_PLAN, RunStage.AWAITING_USER),
    ("no, redo", ActionKind.REJECT_PLAN, RunStage.AWAITING_USER),
    ("try again", ActionKind.REJECT_PLAN, RunStage.AWAITING_USER),
    ("merge it", ActionKind.MERGE_PR, RunStage.PR_READY),
    ("ship it", ActionKind.MERGE_PR, RunStage.PR_READY),
    ("mark done", ActionKind.MERGE_PR, RunStage.PR_READY),
    ("create pr", ActionKind.CREATE_PR, RunStage.VERIFYING),
    ("open pr please", ActionKind.CREATE_PR, RunStage.VERIFYING),
    ("make the pr", ActionKind.CREATE_PR, RunStage.VERIFYING),
    ("plan this", ActionKind.START_PLANNING, RunStage.PLANNING),
    ("start planning", ActionKind.START_PLANNING, RunStage.PLANNING),
    ("develop it", ActionKind.MOVE_TO_DEVELOPMENT, RunStage.DEVELOPING),
    ("start development", ActionKind.MOVE_TO_DEVELOPMENT, RunStage.DEVELOPING),
    ("code it", ActionKind.MOVE_TO_DEVELOPMENT, RunStage.DEVELOPING),
    ("stop now", ActionKind.STOP_RUN, RunStage.INTERRUPTED),
    ("halt", ActionKind.STOP_RUN, RunStage.INTERRUPTED),
]


def test_classify_text_golden_table():
    for text, kind, stage in GOLDEN_TEXT_CASES:
        run = _run(stage=stage.value, actions=[kind.value])
        intent = classify_text(run, text)
        assert intent is not None, f"expected {kind} for {text!r}"
        assert intent.intent == kind, f"expected {kind} for {text!r}, got {intent.intent}"
        assert intent.source == IntentSource.TEXT
        assert intent.text == text


def test_classify_text_returns_none_for_plain_message():
    run = _run(stage="awaiting_user", actions=[ActionKind.APPROVE_PLAN.value])
    assert classify_text(run, "what is the dedupe logic?") is None
    assert classify_text(run, "  ") is None


def test_classify_text_skips_when_action_not_legal():
    run = _run(stage="queued", actions=[])
    assert classify_text(run, "approve") is None
    assert classify_text(run, "merge it") is None


def test_classify_text_case_insensitive_and_stripped():
    run = _run(stage="awaiting_user", actions=[ActionKind.APPROVE_PLAN.value])
    assert classify_text(run, "  APPROVE  ").intent == ActionKind.APPROVE_PLAN
    assert classify_text(run, "LGTM").intent == ActionKind.APPROVE_PLAN


def test_gate_intent_legal_button_move():
    run = _run(stage="awaiting_user", actions=[ActionKind.APPROVE_PLAN.value])
    intent = UserIntent(run_id="r1", intent=ActionKind.APPROVE_PLAN, source=IntentSource.BUTTON)
    gate_intent(run, intent)


def test_gate_intent_illegal_move_raises_value_error():
    run = _run(stage="queued", actions=[])
    intent = UserIntent(run_id="r1", intent=ActionKind.APPROVE_PLAN, source=IntentSource.BUTTON)
    with pytest.raises(ValueError):
        gate_intent(run, intent)


def test_gate_intent_text_state_changing_needs_confirmation():
    run = _run(stage="awaiting_user", actions=[ActionKind.APPROVE_PLAN.value])
    intent = UserIntent(run_id="r1", intent=ActionKind.APPROVE_PLAN, source=IntentSource.TEXT)
    with pytest.raises(IntentNeedsConfirmation) as exc:
        gate_intent(run, intent)
    assert exc.value.intent.intent == ActionKind.APPROVE_PLAN


def test_gate_intent_text_read_only_executes():
    run = _run(stage="investigating", actions=[])
    intent = UserIntent(run_id="r1", intent=ActionKind.SEND_MESSAGE, source=IntentSource.TEXT)
    gate_intent(run, intent)


def test_gate_intent_text_confirmed_passes():
    run = _run(stage="awaiting_user", actions=[ActionKind.APPROVE_PLAN.value])
    intent = UserIntent(run_id="r1", intent=ActionKind.APPROVE_PLAN,
                        source=IntentSource.TEXT, confirmed=True)
    gate_intent(run, intent)


def test_gate_intent_irreversible_unconfirmed_raises():
    run = _run(stage="pr_ready", actions=[ActionKind.MERGE_PR.value])
    intent = UserIntent(run_id="r1", intent=ActionKind.MERGE_PR, source=IntentSource.BUTTON)
    with pytest.raises(IntentNeedsConfirmation):
        gate_intent(run, intent)


def test_gate_intent_irreversible_confirmed_passes():
    run = _run(stage="pr_ready", actions=[ActionKind.MERGE_PR.value])
    intent = UserIntent(run_id="r1", intent=ActionKind.MERGE_PR,
                        source=IntentSource.BUTTON, confirmed=True)
    gate_intent(run, intent)


def test_gate_intent_voice_source_needs_confirmation():
    run = _run(stage="awaiting_user", actions=[ActionKind.APPROVE_PLAN.value])
    intent = UserIntent(run_id="r1", intent=ActionKind.APPROVE_PLAN, source=IntentSource.VOICE)
    with pytest.raises(IntentNeedsConfirmation):
        gate_intent(run, intent)


def test_gate_intent_always_legal_nudge_and_stop():
    run = _run(stage="investigating", actions=[])
    gate_intent(run, UserIntent(run_id="r1", intent=ActionKind.NUDGE, source=IntentSource.BUTTON))
    gate_intent(run, UserIntent(run_id="r1", intent=ActionKind.STOP_RUN, source=IntentSource.BUTTON))
    gate_intent(run, UserIntent(run_id="r1", intent=ActionKind.ABANDON_RUN,
                                 source=IntentSource.BUTTON, confirmed=True))


def test_read_only_intents_set_membership():
    assert ActionKind.SEND_MESSAGE in READ_ONLY_INTENTS
    assert ActionKind.APPROVE_PLAN not in READ_ONLY_INTENTS


def test_load_run_for_user_scoping(session, make_user):
    from app.services.runs import transition
    owner = make_user("owner")
    other = make_user("other")
    run = Run(id="run-1", created_by=owner.id, mode="ask")
    transition(run, RunStage.QUEUED)
    session.add(run)
    session.commit()
    assert load_run_for_user("run-1", owner.id).id == "run-1"
    assert load_run_for_user("run-1", other.id) is None
    assert load_run_for_user("missing", owner.id) is None
