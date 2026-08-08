"""Wave 4 Stream B regression tests: terminal cleanup + money.

F1 every terminal path settles cost, releases the key, clears the secret.
F2 container-start failure never leaks a minted key.
F3 plan/debug/failed/stopped paths record spend.
F5 budget carries across kill/replace.
F6 settle-before-release; already-deleted keys are tolerated.
E4 a wedged explorer doesn't orphan its siblings.
"""

import pytest

from app.db.models.run import Run
from app.db.models.thread import Thread
from app.orchestrator.thread_manager import ThreadManager


class _FakeGateway:
    def __init__(self, spend=0.42, fail_delete=False):
        self.minted: list[dict] = []
        self.deleted: list[str] = []
        self.spend = spend
        self.fail_delete = fail_delete

    async def mint_key(self, alias, max_budget_usd, models=None):
        from app.gateway.litellm import VirtualKey
        self.minted.append({"alias": alias, "max_budget": max_budget_usd})
        return VirtualKey(key=f"sk-{alias}", alias=alias, max_budget=max_budget_usd)

    async def delete_key(self, key):
        if self.fail_delete:
            raise RuntimeError("gateway 500")
        self.deleted.append(key)

    async def read_spend_reconciled(self, key, **kw):
        return self.spend


class _FakeRelay:
    async def publish_thread_status(self, *a):
        pass

    async def publish_run_stage(self, *a):
        pass


class _FakeIngest:
    def register_run(self, run_id):
        pass

    def unregister_run(self, run_id):
        pass


def _tm():
    return ThreadManager(_FakeIngest(), _FakeRelay(), None)


def _mk(session, make_user, **tkw):
    from app.db.models.user import User
    u = session.query(User).filter_by(username="a").one_or_none() or make_user("a")
    if session.get(Run, "r1") is None:
        session.add(Run(id="r1", created_by=u.id, mode="ask", stage="investigating"))
    t = Thread(id=tkw.pop("id", "l1"), run_id="r1", persona="explorer", **tkw)
    session.add(t)
    session.commit()
    return t


# ------------------------------------------------------------------- F1/F6

async def test_cleanup_terminal_settles_releases_and_clears(session, make_user):
    """F1/F6: settle happens BEFORE the key is deleted, and the stored
    secret is cleared from the row afterward."""
    _mk(session, make_user, status="failed", gateway_key="sk-live",
        gateway_key_alias="thread-l1")
    tm = _tm()
    tm.gateway = gw = _FakeGateway(spend=1.23)
    await tm._cleanup_terminal("l1")
    session.expire_all()
    row = session.get(Thread, "l1")
    assert gw.deleted == ["sk-live"]
    assert row.cost_usd == 1.23          # settled
    assert row.gateway_key is None       # secret cleared
    assert row.gateway_key_alias is None
    assert row.finished_at is not None


async def test_cleanup_tolerates_already_deleted_key(session, make_user):
    """F6: a gateway delete failure must not strand the clear — the key is
    gone either way (or the TTL reaper gets it)."""
    _mk(session, make_user, status="stopped", gateway_key="sk-gone")
    tm = _tm()
    tm.gateway = _FakeGateway(fail_delete=True)
    await tm._cleanup_terminal("l1")  # must not raise
    session.expire_all()
    assert session.get(Thread, "l1").gateway_key is None


async def test_cleanup_tolerates_key_gone_before_settle(session, make_user):
    """The key TTL'd (or gateway-db reset) before the terminal cleanup, so the
    settle readback 404s: keep the last known cost (never clobber with 0.0),
    stay quiet, and still release + clear the stored secret."""
    import httpx
    from tests.conftest import FakeResponse

    class _GoneGateway(_FakeGateway):
        async def read_spend_reconciled(self, key, **kw):
            raise httpx.HTTPStatusError(
                "404", request=None, response=FakeResponse(status_code=404))

    _mk(session, make_user, status="failed", gateway_key="sk-gone", cost_usd=0.7)
    tm = _tm()
    tm.gateway = gw = _GoneGateway()
    await tm._cleanup_terminal("l1")  # must not raise
    session.expire_all()
    row = session.get(Thread, "l1")
    assert row.cost_usd == 0.7          # last known cost preserved
    assert gw.deleted == ["sk-gone"]    # release still ran
    assert row.gateway_key is None      # secret cleared


async def test_mark_replaced_releases_key(session, make_user):
    """F1: 'replaced' was missing from every cleanup list — a kill/replace'd
    thread leaked its key and spend."""
    _mk(session, make_user, status="running", gateway_key="sk-old")
    tm = _tm()
    tm.gateway = gw = _FakeGateway()
    await tm._mark("l1", "replaced")
    assert gw.deleted == ["sk-old"]
    session.expire_all()
    assert session.get(Thread, "l1").gateway_key is None


async def test_abandon_settles_and_clears_every_thread(session, make_user):
    """F1/F3: abandon used to leave threads non-terminal with live keys."""
    from app.orchestrator.run_manager import RunManager
    _mk(session, make_user, id="l1", status="running", gateway_key="sk-1")
    _mk(session, make_user, id="l2", status="idle", gateway_key="sk-2")

    tm = _tm()
    tm.gateway = gw = _FakeGateway(spend=0.5)

    class _Ctl:
        async def kill(self, tid, wait_ack=False):
            return True

    rm = RunManager(_FakeIngest(), _FakeRelay(), tm, _Ctl())
    await rm.abandon_run("r1")
    session.expire_all()
    for tid in ("l1", "l2"):
        row = session.get(Thread, tid)
        assert row.status == "stopped"        # capacity slot freed
        assert row.gateway_key is None        # secret cleared
        assert row.cost_usd == 0.5            # spend recorded
    assert sorted(gw.deleted) == ["sk-1", "sk-2"]


# ------------------------------------------------------------------- F5

async def test_kill_replace_carries_remaining_budget(session, make_user, monkeypatch):
    """F5: the replacement's budget is the OLD thread's budget minus its
    settled spend — repeated replaces can't multiply the effective cap."""
    import app.orchestrator.run_manager as rm_mod
    from app.orchestrator.run_manager import RunManager
    _mk(session, make_user, status="running", container_id="c1",
        budget_usd=5.0, gateway_key="sk-old",
        spawn_context={"prompt": "p", "persona_prompt": "pp"})

    tm = _tm()
    tm.gateway = _FakeGateway(spend=3.0)
    captured = {}

    async def fake_spawn(*a, **k):
        captured.update(k)
        return Thread(id="l2", run_id="r1", persona="explorer")
    tm.spawn = fake_spawn

    class _Ctl:
        async def kill(self, tid, wait_ack=False):
            return True

    monkeypatch.setattr(rm_mod.sandbox_manager, "wait_for_container_exit",
                        lambda cid, timeout_s=15.0: True)
    rm = RunManager(_FakeIngest(), _FakeRelay(), tm, _Ctl())
    await rm.kill_replace_thread("r1", "l1")
    assert captured["budget_usd"] == pytest.approx(2.0)  # 5.0 - 3.0 settled


# ------------------------------------------------------------------- F2

async def test_container_start_failure_releases_minted_key(session, make_user, monkeypatch):
    """F2: the key is persisted to the row BEFORE container start, so a
    start failure's cleanup can actually find and delete it."""
    from app.db.models.user import User
    from app.sandbox import manager as sandbox_mod
    u = session.query(User).filter_by(username="a").one_or_none() or make_user("a")
    if session.get(Run, "r1") is None:
        session.add(Run(id="r1", created_by=u.id, mode="ask", stage="investigating"))
    session.commit()

    tm = _tm()
    tm.gateway = gw = _FakeGateway()
    monkeypatch.setattr(sandbox_mod.sandbox_manager, "run_thread_container",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr("app.services.knowledge.prompt_block_for_run",
                        lambda *a, **k: _async_empty())

    from app.orchestrator.thread_manager import ThreadSpawnError
    with pytest.raises(ThreadSpawnError, match="container start failed"):
        await tm.spawn(session.get(Run, "r1"), persona="explorer",
                       prompt="p", persona_prompt="pp",
                       writable_repo=None, context_repos=[])
    minted_keys = [f"sk-{m['alias']}" for m in gw.minted]
    assert minted_keys, "a key was minted before the failed start"
    # The minted key was deleted during failure cleanup (F2: persisted to the
    # row before start, so release_key can find it).
    assert gw.deleted == minted_keys
    session.expire_all()
    row = session.query(Thread).filter_by(run_id="r1").one()
    assert row.status == "failed"
    assert row.gateway_key is None


async def _async_empty(*a, **k):
    return ""


# ------------------------------------------------------------------- F3

async def test_failed_run_terminates_live_threads(session, make_user, monkeypatch):
    """F3/F1: a blueprint exception terminates live threads for real and
    settles their spend — previously they idled until the reaper."""
    from app.orchestrator.run_manager import RunManager
    _mk(session, make_user, id="l1", status="running", container_id="c1",
        gateway_key="sk-1")

    tm = _tm()
    tm.gateway = gw = _FakeGateway(spend=0.9)
    rm = RunManager(_FakeIngest(), _FakeRelay(), tm, None)
    monkeypatch.setattr(rm, "_stop_thread_container", lambda tid: _async_empty())

    class Boom:
        async def execute(self, ctx):
            raise RuntimeError("node exploded")

    class Ctx:
        pass

    await rm._guarded_execute("r1", Ctx(), Boom())
    session.expire_all()
    row = session.get(Thread, "l1")
    assert row.status == "failed"
    assert row.cost_usd == 0.9
    assert row.gateway_key is None
    assert gw.deleted == ["sk-1"]
