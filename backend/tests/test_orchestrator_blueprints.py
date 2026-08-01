import pytest
from zagent_contracts import RunStage

from app.db.models.run import Run
from app.orchestrator.blueprints.base import Blueprint, BlueprintContext, Node


class _FakeRelay:
    def __init__(self):
        self.published = []
    async def publish_run_stage(self, run_id, stage, actions):
        self.published.append((run_id, stage, actions))


class _SpyBlueprint(Blueprint):
    name = "spy"

    def __init__(self):
        self.calls = []

    def nodes(self):
        async def n1(ctx):
            self.calls.append("n1")
        async def n2(ctx):
            self.calls.append("n2")
            raise RuntimeError("boom")
        async def n3(ctx):
            self.calls.append("n3")
        return [
            Node("n1", n1, deterministic=True, stage=RunStage.PROVISIONING),
            Node("n2", n2, deterministic=False, stage=RunStage.INVESTIGATING),
            Node("n3", n3, deterministic=True, stage=RunStage.VERIFYING),
        ]


async def test_execute_runs_nodes_in_order_and_transitions(session, make_user):
    u = make_user("a")
    run = Run(id="r1", created_by=u.id, mode="spy", stage=RunStage.QUEUED.value)
    session.add(run)
    session.commit()
    relay = _FakeRelay()
    ctx = BlueprintContext(run=run, services={"relay": relay})
    bp = _SpyBlueprint()
    with pytest.raises(RuntimeError, match="boom"):
        await bp.execute(ctx)
    assert bp.calls == ["n1", "n2"]
    session.expire_all()
    row = session.get(Run, "r1")
    assert row.stage == RunStage.INVESTIGATING.value
    assert relay.published[0][1] == RunStage.PROVISIONING.value
    assert relay.published[1][1] == RunStage.INVESTIGATING.value


async def test_execute_node_without_stage_skips_transition(session, make_user):
    u = make_user("a")
    run = Run(id="r1", created_by=u.id, mode="spy", stage=RunStage.QUEUED.value)
    session.add(run)
    session.commit()

    class _NoStageBlueprint(Blueprint):
        name = "nostage"
        def __init__(self): self.calls = []
        def nodes(self):
            async def n(ctx): self.calls.append("ran")
            return [Node("n", n, deterministic=True)]

    ctx = BlueprintContext(run=run, services={"relay": _FakeRelay()})
    bp = _NoStageBlueprint()
    await bp.execute(ctx)
    assert bp.calls == ["ran"]


def test_blueprint_is_abstract():
    with pytest.raises(TypeError):
        Blueprint()


def test_node_dataclass_fields():
    async def f(ctx): pass
    n = Node("x", f, deterministic=True, stage=RunStage.PLANNING)
    assert n.name == "x"
    assert n.deterministic is True
    assert n.stage == RunStage.PLANNING


def test_blueprint_context_defaults():
    ctx = BlueprintContext(run=None)
    assert ctx.services == {}
    assert ctx.artifacts == {}
