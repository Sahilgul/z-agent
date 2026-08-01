from app.db.models.repo import Repo
from app.services import hydration


class _FakeAdo:
    def __init__(self, tickets=None, work_items=None):
        self._tickets = tickets or []
        self._work_items = work_items or {}

    async def my_active_tickets(self, descriptor):
        return self._tickets

    async def get_work_item(self, work_item_id):
        return self._work_items.get(work_item_id, {"id": work_item_id,
                                                   "fields": {"System.Title": f"item {work_item_id}"}})


class _BoomAdo:
    async def my_active_tickets(self, descriptor):
        raise RuntimeError("ado down")

    async def get_work_item(self, work_item_id):
        raise RuntimeError("ado down")


async def test_my_tickets_empty_when_unbound(make_user):
    user = make_user("alice", ado_descriptor=None)
    assert await hydration.my_tickets(user, ado_client=_FakeAdo()) == []


async def test_my_tickets_returns_mapped_items(make_user):
    user = make_user("alice", ado_descriptor="desc-123")
    client = _FakeAdo(tickets=[{"id": 1, "fields": {"System.Title": "Bug A", "System.State": "Active",
                                                       "System.WorkItemType": "Bug"}}])
    out = await hydration.my_tickets(user, ado_client=client)
    assert out == [{"id": 1, "title": "Bug A", "state": "Active", "type": "Bug"}]


def test_blast_radius_returns_list():
    out = hydration.blast_radius("ServerApp")
    assert isinstance(out, list)


def test_blast_radius_unknown_repo_is_empty():
    assert hydration.blast_radius("NotARepo") == []


async def test_hydrate_title_falls_back_to_task_when_no_item():
    assert await hydration.hydrate_title(None, "my task", ado_client=_FakeAdo()) == "my task"


async def test_hydrate_title_uses_work_item_title():
    client = _FakeAdo(work_items={42: {"id": 42, "fields": {"System.Title": "Real title"}}})
    assert await hydration.hydrate_title(42, "fallback", ado_client=client) == "Real title"


async def test_hydrate_title_ado_down_falls_back_to_task():
    assert await hydration.hydrate_title(42, "fallback", ado_client=_BoomAdo()) == "fallback"


async def test_prewarm_pool_records_registered_repo(session):
    repo = Repo(name="ServerApp", integration_branch="main")
    session.add(repo); session.commit()
    pool = hydration.PrewarmPool()
    result = await pool.prewarm([{"name": "ServerApp"}])
    assert result["prewarmed"][0] == {"repo": "ServerApp", "branch": "main", "status": "recorded"}
    assert pool.requested == [("ServerApp", "main")]


async def test_prewarm_pool_flags_unregistered_repo(session):
    pool = hydration.PrewarmPool()
    result = await pool.prewarm([{"name": "Ghost"}])
    assert result["prewarmed"][0]["status"] == "repo_not_registered"


async def test_prewarm_pool_accepts_bare_strings(session):
    repo = Repo(name="ClientApp", integration_branch="dev")
    session.add(repo); session.commit()
    pool = hydration.PrewarmPool()
    result = await pool.prewarm(["ClientApp"])
    assert result["prewarmed"][0]["branch"] == "dev"
