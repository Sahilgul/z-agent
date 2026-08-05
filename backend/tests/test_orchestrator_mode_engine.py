import pytest

from app.db.models.mode import Mode
from app.orchestrator.blueprints.ask import AskBlueprint
from app.orchestrator.blueprints.base import Blueprint
from app.orchestrator.blueprints.goal import GoalBlueprint
from app.orchestrator import mode_engine


def test_blueprints_registry_contains_ask():
    assert "ask" in mode_engine.BLUEPRINTS
    assert mode_engine.BLUEPRINTS["ask"] is AskBlueprint


def test_blueprints_registry_contains_goal():
    assert mode_engine.BLUEPRINTS["goal"] is GoalBlueprint


def test_blueprint_for_goal_returns_instance():
    bp = mode_engine.blueprint_for("goal")
    assert isinstance(bp, GoalBlueprint)
    assert bp.name == "goal"


def test_blueprint_for_ask_returns_instance():
    bp = mode_engine.blueprint_for("ask")
    assert isinstance(bp, AskBlueprint)
    assert isinstance(bp, Blueprint)
    assert bp.name == "ask"


def test_blueprint_for_unknown_raises_keyerror():
    with pytest.raises(KeyError):
        mode_engine.blueprint_for("ghost")


def test_ask_blueprint_nodes_in_order():
    bp = mode_engine.blueprint_for("ask")
    nodes = bp.nodes()
    assert [n.name for n in nodes] == ["hydrate", "investigate", "complete"]
    assert nodes[0].deterministic is True
    assert nodes[1].deterministic is False
    assert nodes[2].deterministic is True


# --------------------------------------------------------------- topology resolution
def test_blueprint_for_uses_topology_when_registered(session):
    session.add(Mode(name="custom-plan", topology="ask", enabled=True,
                     persona_prompt="p", permission_mode="default"))
    session.commit()
    bp = mode_engine.blueprint_for("custom-plan")
    assert isinstance(bp, AskBlueprint)


def test_blueprint_for_falls_back_to_name_when_topology_not_a_blueprint(session):
    session.add(Mode(name="ask", topology="single", enabled=True,
                     persona_prompt="p", permission_mode="default"))
    session.commit()
    bp = mode_engine.blueprint_for("ask")
    assert isinstance(bp, AskBlueprint)


def test_blueprint_for_unknown_topology_and_name_raises(session):
    session.add(Mode(name="ghost", topology="ghosty", enabled=True,
                     persona_prompt="p", permission_mode="default"))
    session.commit()
    with pytest.raises(KeyError):
        mode_engine.blueprint_for("ghost")


def test_topology_for_returns_none_when_row_missing(session):
    assert mode_engine._topology_for("no-such-mode") is None


def test_topology_for_reads_row_topology(session):
    session.add(Mode(name="probe", topology="ask", enabled=True,
                     persona_prompt="p", permission_mode="default"))
    session.commit()
    assert mode_engine._topology_for("probe") == "ask"
