from collegium_contracts import (
    IRREVERSIBLE_INTENTS,
    ActionKind,
    Decomposition,
    IntentSource,
    Notebook,
    Plan,
    PlanStep,
    RunStage,
    StepEvent,
    StepKind,
    SwarmSlice,
    TriggerEvent,
    TriggerSource,
    TypingDelta,
    UserIntent,
)


def test_step_event_minimal():
    e = StepEvent(run_id="r1", lane_id="l1", seq=0, kind=StepKind.COMMAND, title="grep dedupe")
    assert e.sdk_message_uuid is None
    assert e.schema_version == 1
    assert e.detail == {}


def test_step_event_sdk_uuid_roundtrip():
    e = StepEvent(
        run_id="r1", lane_id="l1", seq=3, kind=StepKind.MESSAGE,
        title="Lead reply", sdk_message_uuid="uuid-abc",
    )
    data = e.model_dump(mode="json")
    assert StepEvent(**data).sdk_message_uuid == "uuid-abc"


def test_typing_delta_has_no_seq():
    d = TypingDelta(run_id="r1", lane_id="l1", kind=StepKind.THINKING, text="frag")
    assert not hasattr(d, "seq")


def test_run_stage_values_match_plan():
    assert [s.value for s in RunStage] == [
        "queued", "provisioning", "investigating", "planning", "awaiting_user",
        "developing", "verifying", "pr_ready", "interrupted", "completed",
        "failed", "abandoned",
    ]


def test_irreversible_intents():
    assert ActionKind.MERGE_PR in IRREVERSIBLE_INTENTS
    assert ActionKind.NUDGE not in IRREVERSIBLE_INTENTS


def test_user_intent_defaults():
    i = UserIntent(run_id="r1", intent=ActionKind.APPROVE_PLAN)
    assert i.source == IntentSource.BUTTON
    assert i.confirmed is False


def test_plan_schema_validates_steps():
    p = Plan(
        title="Fix dedupe",
        summary="s",
        steps=[PlanStep(index=0, title="t", description="d", success_criterion="tests pass")],
    )
    assert p.steps[0].status.value == "pending"
    # JSON-schema fidelity check: Plan mode's output_format depends on this
    schema = Plan.model_json_schema()
    assert "properties" in schema and "steps" in schema["properties"]


def test_notebook_shape():
    n = Notebook(findings=["f"], confidence="high")
    assert n.evidence == [] and n.open_questions == []


def test_trigger_event_idempotency():
    t = TriggerEvent(
        source=TriggerSource.ADO_WEBHOOK, external_id="12345", revision=7,
        event_type="work_item.updated",
    )
    assert t.idempotency_key == ("ado_webhook", "12345", 7)


def test_decomposition_shape():
    d = Decomposition(
        slices=[SwarmSlice(title="leg a", prompt="trace a", angle="ingress")],
        counter_proposal=1, rationale="two would duplicate",
    )
    assert d.slices[0].repo is None
    assert d.counter_proposal == 1
    # JSON-schema fidelity: the swarm decompose node's output_format target
    schema = Decomposition.model_json_schema()
    assert "slices" in schema["properties"]


def test_decomposition_roundtrip_from_lead_json():
    raw = '{"slices": [{"title": "a", "prompt": "p", "repo": null, "angle": "x"}], "counter_proposal": null, "rationale": ""}'
    d = Decomposition.model_validate_json(raw)
    assert len(d.slices) == 1 and d.slices[0].angle == "x"
