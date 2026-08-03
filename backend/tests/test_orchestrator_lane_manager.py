import pytest

from app.db.models.lane import Lane
from app.db.models.repo import Repo
from app.db.models.run import Run
from app.gateway.litellm import VirtualKey
from app.orchestrator import lane_manager
from app.orchestrator.lane_manager import LaneManager, LaneSpawnError


class _FakeIngest:
    def __init__(self): self.registered = []
    def register_run(self, run_id): self.registered.append(run_id)


class _FakeRelay:
    def __init__(self): self.published = []
    async def publish_lane_status(self, run_id, lane_id, status):
        self.published.append((run_id, lane_id, status))


class _FakeGateway:
    def __init__(self, spend=1.5, fail=False):
        self.spend = spend
        self.fail = fail
        self.minted = []
        self.deleted = []
    async def mint_key(self, alias, max_budget_usd):
        if self.fail:
            raise RuntimeError("gateway down")
        self.minted.append((alias, max_budget_usd))
        return VirtualKey(key="vk-1", alias=alias, max_budget=max_budget_usd)
    async def read_spend_reconciled(self, key):
        return self.spend
    async def delete_key(self, key):
        self.deleted.append(key)


def _make_run(session, make_user, run_id="r1", autonomy="supervised"):
    u = make_user("a")
    run = Run(id=run_id, created_by=u.id, mode="ask", stage="investigating", autonomy=autonomy)
    session.add(run)
    session.commit()
    return run


async def test_spawn_creates_lane_and_starts_container(session, make_user, monkeypatch):
    run = _make_run(session, make_user)
    ingest, relay, gw = _FakeIngest(), _FakeRelay(), _FakeGateway()
    lm = LaneManager(ingest, relay, gw)

    async def fake_acquire(repo):
        return True, ""
    monkeypatch.setattr(lane_manager.capacity, "try_acquire", fake_acquire)
    monkeypatch.setattr(lane_manager.sandbox_manager, "run_lane_container",
                        lambda *a, **k: "container-xyz")

    lane = await lm.spawn(run, "researcher", "task", "persona", None, [])
    assert lane.id
    assert run.id in ingest.registered
    assert relay.published[-1][0] == run.id
    session.expire_all()
    row = session.get(Lane, lane.id)
    assert row.status == "running"
    assert row.container_id == "container-xyz"
    assert row.gateway_key == "vk-1"


async def test_spawn_capacity_denied_raises(session, make_user, monkeypatch):
    run = _make_run(session, make_user)
    lm = LaneManager(_FakeIngest(), _FakeRelay(), _FakeGateway())

    async def fake_acquire(repo):
        return False, "cap reached"
    monkeypatch.setattr(lane_manager.capacity, "try_acquire", fake_acquire)
    with pytest.raises(LaneSpawnError, match="cap reached"):
        await lm.spawn(run, "researcher", "task", "persona", None, [])


async def test_spawn_gateway_failure_marks_lane_failed(session, make_user, monkeypatch):
    run = _make_run(session, make_user)
    lm = LaneManager(_FakeIngest(), _FakeRelay(), _FakeGateway(fail=True))

    async def fake_acquire(repo):
        return True, ""
    monkeypatch.setattr(lane_manager.capacity, "try_acquire", fake_acquire)
    with pytest.raises(LaneSpawnError, match="gateway key mint failed"):
        await lm.spawn(run, "researcher", "task", "persona", None, [])
    lanes = session.query(Lane).all()
    assert lanes[0].status == "failed"


async def test_spawn_container_failure_marks_lane_failed(session, make_user, monkeypatch):
    run = _make_run(session, make_user)
    lm = LaneManager(_FakeIngest(), _FakeRelay(), _FakeGateway())

    async def fake_acquire(repo):
        return True, ""
    monkeypatch.setattr(lane_manager.capacity, "try_acquire", fake_acquire)

    def boom(*a, **k):
        raise RuntimeError("docker exploded")
    monkeypatch.setattr(lane_manager.sandbox_manager, "run_lane_container", boom)
    with pytest.raises(LaneSpawnError, match="container start failed"):
        await lm.spawn(run, "researcher", "task", "persona", None, [])
    assert session.query(Lane).first().status == "failed"


async def test_spawn_resume_from_lane_inherits_session_id(session, make_user, monkeypatch):
    """A mode-switch respawn must inherit the prior lane's session_id so the
    SDK picks up the conversation, and mount the prior lane's session volume."""
    run = _make_run(session, make_user)
    prior = Lane(id="l-old", run_id=run.id, persona="researcher", status="completed",
                 session_id="sess-prior-abc")
    session.add(prior); session.commit()

    ingest, relay, gw = _FakeIngest(), _FakeRelay(), _FakeGateway()
    lm = LaneManager(ingest, relay, gw)

    async def fake_acquire(repo):
        return True, ""
    monkeypatch.setattr(lane_manager.capacity, "try_acquire", fake_acquire)

    captured = {}
    def fake_run_container(run, lane, prompt, persona_prompt, permission_mode,
                            writable_repo, context_repos, resume_from_lane_id=None):
        captured["resume_from_lane_id"] = resume_from_lane_id
        captured["session_id"] = lane.session_id
        return "container-new"
    monkeypatch.setattr(lane_manager.sandbox_manager, "run_lane_container", fake_run_container)

    lane = await lm.spawn(run, "researcher", "task", "persona", None, [],
                          resume_from_lane_id="l-old")
    assert lane.session_id == "sess-prior-abc"
    assert captured["resume_from_lane_id"] == "l-old"
    assert captured["session_id"] == "sess-prior-abc"


async def test_settle_cost_updates_lane_and_run(session, make_user):
    run = _make_run(session, make_user)
    ingest, relay, gw = _FakeIngest(), _FakeRelay(), _FakeGateway(spend=2.25)
    lm = LaneManager(ingest, relay, gw)
    lane = Lane(id="l1", run_id=run.id, persona="researcher", status="completed",
                gateway_key="vk-1", budget_usd=5.0)
    session.add(lane)
    session.commit()
    spend = await lm.settle_cost("l1")
    assert spend == 2.25
    session.expire_all()
    assert session.get(Lane, "l1").cost_usd == 2.25
    assert session.get(Run, run.id).cost_usd == 2.25


async def test_settle_cost_no_lane_returns_zero(session):
    lm = LaneManager(_FakeIngest(), _FakeRelay(), _FakeGateway())
    assert await lm.settle_cost("ghost") == 0.0


async def test_settle_cost_no_gateway_key_returns_zero(session, make_user):
    run = _make_run(session, make_user)
    lm = LaneManager(_FakeIngest(), _FakeRelay(), _FakeGateway())
    lane = Lane(id="l1", run_id=run.id, persona="researcher", status="completed")
    session.add(lane)
    session.commit()
    assert await lm.settle_cost("l1") == 0.0


async def test_release_key_calls_gateway_delete(session, make_user):
    run = _make_run(session, make_user)
    gw = _FakeGateway()
    lm = LaneManager(_FakeIngest(), _FakeRelay(), gw)
    lane = Lane(id="l1", run_id=run.id, persona="researcher", status="completed", gateway_key="vk-9")
    session.add(lane)
    session.commit()
    await lm.release_key("l1")
    assert "vk-9" in gw.deleted


async def test_release_key_swallows_gateway_error(session, make_user):
    run = _make_run(session, make_user)
    gw = _FakeGateway(fail=True)
    lm = LaneManager(_FakeIngest(), _FakeRelay(), gw)
    lane = Lane(id="l1", run_id=run.id, persona="researcher", status="completed", gateway_key="vk-9")
    session.add(lane)
    session.commit()
    await lm.release_key("l1")  # should not raise


async def test_release_key_missing_lane_is_noop(session):
    lm = LaneManager(_FakeIngest(), _FakeRelay(), _FakeGateway())
    await lm.release_key("ghost")


def test_lane_spawn_error_is_runtime_error():
    assert issubclass(LaneSpawnError, RuntimeError)


# --------------------------------------------------------------- spawn_many (width swarm)
def _specs(n):
    return [{"persona": "explorer", "prompt": f"slice {i}",
             "persona_prompt": "p", "lane_hint": f"explorer-{i}"} for i in range(n)]


async def test_spawn_many_spawns_all_specs(session, make_user, monkeypatch):
    run = _make_run(session, make_user)
    lm = LaneManager(_FakeIngest(), _FakeRelay(), _FakeGateway())
    monkeypatch.setattr(lane_manager.sandbox_manager, "run_lane_container",
                        lambda *a, **k: "cid")
    lanes = await lm.spawn_many(run, _specs(3), [], queue_poll_seconds=0.001)
    assert len(lanes) == 3
    session.expire_all()
    assert session.query(Lane).filter_by(run_id=run.id, status="running").count() == 3


async def test_spawn_many_queues_past_cap_and_announces_once(session, make_user, monkeypatch):
    """§4: over-cap requests queue deterministically AND the UI says so (one
    queued note per waiting lane, not a spam loop)."""
    run = _make_run(session, make_user)
    relay = _FakeRelay()
    lm = LaneManager(_FakeIngest(), relay, _FakeGateway())
    monkeypatch.setattr(lane_manager.sandbox_manager, "run_lane_container",
                        lambda *a, **k: "cid")
    attempts = iter([(False, "global lane cap (12) reached — queued"), (True, "")])

    async def fake_acquire(repo):
        return next(attempts, (True, ""))
    monkeypatch.setattr(lane_manager.capacity, "try_acquire", fake_acquire)

    lanes = await lm.spawn_many(run, _specs(1), [], queue_poll_seconds=0.001)
    assert len(lanes) == 1
    queued = [p for p in relay.published if p[2] == "queued"]
    assert len(queued) == 1


async def test_spawn_many_skips_lane_on_non_capacity_failure(session, make_user, monkeypatch):
    """A gateway/container failure sinks ONE lane, never the swarm."""
    run = _make_run(session, make_user)
    lm = LaneManager(_FakeIngest(), _FakeRelay(), _FakeGateway(fail=True))
    monkeypatch.setattr(lane_manager.sandbox_manager, "run_lane_container",
                        lambda *a, **k: "cid")
    lanes = await lm.spawn_many(run, _specs(2), [], queue_poll_seconds=0.001)
    assert lanes == []
    session.expire_all()
    assert session.query(Lane).filter_by(status="failed").count() == 2
