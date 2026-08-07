"""Engine-side contract tests: dedicated approval StepKind + action_id
pairing, and the ◆ recap emission at goal stage advances."""

from __future__ import annotations

import pytest
from langgraph.checkpoint.memory import MemorySaver
from collegium_contracts import StepKind

from worker.engine.events import EventEmitter
from worker.engine.goal_mode import GoalStage
from worker.engine.state import Mode

from test_spine_contract import EventCollector, _ai, _config, _initial, _patch_llm, _tc


def test_approval_stepkind_exists_in_contracts():
    assert StepKind.APPROVAL.value == "approval"


class TestApprovalEventPairing:
    def test_card_uses_dedicated_kind_and_action_id(self):
        em = EventEmitter("r1", "t1")
        card = em.approval_card({
            "approval_id": "ap-tc1", "tool": "terminal_exec",
            "args": {"command": "git push"}, "preview": "push", "destructive": True,
        }, "task-1")
        assert card.kind is StepKind.APPROVAL
        assert card.detail["kind"] == "approval_card"
        assert card.detail["action_id"] == "ap-tc1"
        assert card.detail["destructive"] is True

    def test_decision_pairs_with_card_action_id(self):
        em = EventEmitter("r1", "t1")
        card = em.approval_card({"approval_id": "ap-tc9", "tool": "file_write"}, "task-1")
        decision = em.approval_decision("ap-tc9",
                                        {"decision": "edited_allow", "edited_args": {}}, "task-1")
        assert decision.kind is StepKind.APPROVAL
        assert decision.detail["action_id"] == card.detail["action_id"]
        assert decision.detail["kind"] == "approval_decision"
        assert decision.detail["decision"] == "edited_allow"
        assert decision.detail["edited"] is True


@pytest.mark.asyncio
async def test_recap_emitted_at_goal_stage_advance(monkeypatch: pytest.MonkeyPatch, tmp_path):
    """Every stage advance emits the ◆ recap StepEvent (console parity)."""
    from worker.engine.graph import build_graph

    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path))
    (tmp_path / "routes.py").write_text("def upload(): pass\n")
    collector = EventCollector()
    graph = build_graph(checkpointer=MemorySaver())
    config = _config(collector)

    story = "Add rate limiting to the upload endpoint in api/routes.py"
    _patch_llm(monkeypatch, [
        _ai("", [_tc("tc1", "file_read", {"file_path": "routes.py"})]),
        _ai("Explored the upload handler."),
        _ai("The plan is to add a middleware."),
    ])
    await graph.ainvoke(_initial(mode=Mode.GOAL, prompt=story, autonomy="autonomous"), config)
    await collector.flush()

    recaps = [e for e in collector.events if e.detail.get("kind") == "recap"]
    assert recaps, "stage advances must emit ◆ recap events"
    stages = {e.detail["stage"] for e in recaps}
    assert GoalStage.EXPLORE.value in stages
    assert all(e.title.startswith("◆ recap") for e in recaps)
    assert all("summary" in e.detail for e in recaps)
