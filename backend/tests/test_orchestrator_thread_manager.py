import pytest

from app.db.models.repo import Repo
from app.db.models.run import Run
from app.db.models.thread import Thread
from app.gateway.litellm import VirtualKey
from app.orchestrator import thread_manager
from app.orchestrator.thread_manager import ThreadManager, ThreadSpawnError


class _FakeIngest:
    def __init__(self): self.registered = []
    def register_run(self, run_id): self.registered.append(run_id)


class _FakeRelay:
    def __init__(self):
        self.published = []
        self.notes = []
    async def publish_thread_status(self, run_id, thread_id, status):
        self.published.append((run_id, thread_id, status))
    async def publish_note(self, run_id, text):
        self.notes.append((run_id, text))


class _FakeGateway:
    def __init__(self, spend=1.5, fail=False):
        self.spend = spend
        self.fail = fail
        self.minted = []
        self.deleted = []
    async def mint_key(self, alias, max_budget_usd, models=None):
        if self.fail:
            raise RuntimeError("gateway down")
        self.minted.append((alias, max_budget_usd, models))
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


async def test_spawn_creates_thread_and_starts_container(session, make_user, monkeypatch):
    run = _make_run(session, make_user)
    ingest, relay, gw = _FakeIngest(), _FakeRelay(), _FakeGateway()
    lm = ThreadManager(ingest, relay, gw)

    async def fake_acquire(repo):
        return True, ""
    monkeypatch.setattr(thread_manager.capacity, "try_acquire", fake_acquire)
    monkeypatch.setattr(thread_manager.sandbox_manager, "run_thread_container",
                        lambda *a, **k: "container-xyz")

    thread = await lm.spawn(run, "researcher", "task", "persona", None, [])
    assert thread.id
    assert run.id in ingest.registered
    assert relay.published[-1][0] == run.id
    session.expire_all()
    row = session.get(Thread, thread.id)
    assert row.status == "running"
    assert row.container_id == "container-xyz"
    assert row.gateway_key == "vk-1"


async def test_spawn_scopes_key_and_context_to_model(session, make_user, monkeypatch):
    """Model selection: the lane's virtual key is scoped to exactly its model,
    and the choice rides spawn_context so kill/replace replays it."""
    run = _make_run(session, make_user)
    gw = _FakeGateway()
    lm = ThreadManager(_FakeIngest(), _FakeRelay(), gw)

    async def fake_acquire(repo):
        return True, ""
    monkeypatch.setattr(thread_manager.capacity, "try_acquire", fake_acquire)
    monkeypatch.setattr(thread_manager.sandbox_manager, "run_thread_container",
                        lambda *a, **k: "container-xyz")

    thread = await lm.spawn(run, "GLM 5.2", "task", "persona", None, [],
                            model="glm-5.2")
    assert gw.minted[0][2] == ["glm-5.2"]  # key scoped to the lane's model
    session.expire_all()
    row = session.get(Thread, thread.id)
    assert row.spawn_context["model"] == "glm-5.2"


async def test_spawn_stores_reasoning_in_context(session, make_user, monkeypatch):
    """The composer's reasoning choice rides spawn_context so a kill/replace
    replays it and thread_env injects it as REASONING_EFFORT."""
    run = _make_run(session, make_user)
    lm = ThreadManager(_FakeIngest(), _FakeRelay(), _FakeGateway())

    async def fake_acquire(repo):
        return True, ""
    monkeypatch.setattr(thread_manager.capacity, "try_acquire", fake_acquire)
    monkeypatch.setattr(thread_manager.sandbox_manager, "run_thread_container",
                        lambda *a, **k: "container-xyz")

    thread = await lm.spawn(run, "GLM 5.2", "task", "persona", None, [],
                            model="glm-5.2", reasoning="high")
    session.expire_all()
    assert session.get(Thread, thread.id).spawn_context["reasoning"] == "high"

    # No choice → no key at all (the worker sends no override; the request
    # stays byte-identical to pre-feature traffic).
    thread2 = await lm.spawn(run, "researcher", "task", "persona", None, [])
    session.expire_all()
    assert "reasoning" not in session.get(Thread, thread2.id).spawn_context


async def test_spawn_rejects_reasoning_the_model_lacks(session, make_user, monkeypatch):
    """Fail-closed at spawn too (kill/replace replays bypass create_run)."""
    run = _make_run(session, make_user)
    lm = ThreadManager(_FakeIngest(), _FakeRelay(), _FakeGateway())

    with pytest.raises(ThreadSpawnError, match="not 'xhigh'"):
        await lm.spawn(run, "researcher", "task", "persona", None, [],
                       model="kimi-k2.6", reasoning="xhigh")
    assert session.query(Thread).count() == 0


async def test_spawn_rejects_off_for_always_thinking_models(session, make_user,
                                                            monkeypatch):
    """No current fleet model is always-thinking (K3 took "none" in the
    2026-08-08 probe), but the spawn-side guard stays for future ones —
    exercise it with a synthetic registry entry."""
    from app.core.models import ModelOption
    lm = ThreadManager(_FakeIngest(), _FakeRelay(), _FakeGateway())
    fake = ModelOption(alias="always-thinks", label="t", price_in_per_mtok=1,
                       price_out_per_mtok=2, reasoning_efforts=["low"],
                       supports_thinking_off=False)
    monkeypatch.setattr(lm.settings, "available_models",
                        [*lm.settings.available_models, fake])
    run = _make_run(session, make_user)

    with pytest.raises(ThreadSpawnError, match="always thinks"):
        await lm.spawn(run, "researcher", "task", "persona", None, [],
                       model="always-thinks", reasoning="off")
    assert session.query(Thread).count() == 0


async def test_spawn_defaults_to_gateway_model(session, make_user, monkeypatch):
    run = _make_run(session, make_user)
    gw = _FakeGateway()
    lm = ThreadManager(_FakeIngest(), _FakeRelay(), gw)

    async def fake_acquire(repo):
        return True, ""
    monkeypatch.setattr(thread_manager.capacity, "try_acquire", fake_acquire)
    monkeypatch.setattr(thread_manager.sandbox_manager, "run_thread_container",
                        lambda *a, **k: "container-xyz")

    thread = await lm.spawn(run, "researcher", "task", "persona", None, [])
    assert gw.minted[0][2] == ["kimi-k2.6"]
    session.expire_all()
    assert session.get(Thread, thread.id).spawn_context["model"] == "kimi-k2.6"


async def test_spawn_blind_lane_embeds_notes_never_stages(session, make_user, monkeypatch):
    """Blind lane (GLM/DeepSeek): the Kimi pre-pass description is embedded in
    the prompt TEXT (before the spawn_context snapshot, so kill/replace
    replays it), and no files are staged — the worker never sees IMAGES_DIR."""
    run = _make_run(session, make_user)
    lm = ThreadManager(_FakeIngest(), _FakeRelay(), _FakeGateway())

    async def fake_acquire(repo):
        return True, ""
    monkeypatch.setattr(thread_manager.capacity, "try_acquire", fake_acquire)
    captured = {}
    monkeypatch.setattr(
        thread_manager.sandbox_manager, "run_thread_container",
        lambda run, thread, prompt, *a, **k: captured.setdefault("prompt", prompt) or "cid")
    # Staging must NOT run for a blind lane — explode if it does.
    import app.sandbox.manager as sm
    monkeypatch.setattr(sm, "session_subpath",
                        lambda *a: (_ for _ in ()).throw(AssertionError("staged for blind lane")))

    thread = await lm.spawn(run, "GLM 5.2", "task", "persona", None, [],
                            model="glm-5.2",
                            images=["/tmp/attach-1.png"],
                            image_notes="\n\n<attached-images>red square</attached-images>")
    assert "<attached-images>red square</attached-images>" in captured["prompt"]
    session.expire_all()
    ctx = session.get(Thread, thread.id).spawn_context
    assert ctx["images"] == ["/tmp/attach-1.png"]  # replayable
    assert "<attached-images>" in ctx["prompt"]    # embedded pre-snapshot


async def test_spawn_vision_lane_stages_images_prompt_untouched(
        session, make_user, monkeypatch, tmp_path):
    """Vision lane (Kimi): attachments are copied into the session volume the
    container mounts, the prompt stays clean (the model sees the image
    itself), and spawn_context carries the paths for kill/replace restaging."""
    run = _make_run(session, make_user)
    lm = ThreadManager(_FakeIngest(), _FakeRelay(), _FakeGateway())

    async def fake_acquire(repo):
        return True, ""
    monkeypatch.setattr(thread_manager.capacity, "try_acquire", fake_acquire)
    captured = {}
    monkeypatch.setattr(
        thread_manager.sandbox_manager, "run_thread_container",
        lambda run, thread, prompt, *a, **k: captured.setdefault("prompt", prompt) or "cid")

    src = tmp_path / "image-1.png"
    src.write_bytes(b"\x89PNG-fake")
    session_root = tmp_path / "sessions"
    import app.sandbox.manager as sm
    monkeypatch.setattr(sm, "session_subpath",
                        lambda run_id, thread_id: session_root / run_id / thread_id)

    thread = await lm.spawn(run, "Kimi K2.6", "task", "persona", None, [],
                            model="kimi-k2.6", images=[str(src)],
                            image_notes="unused-for-vision")
    staged = session_root / run.id / thread.id / "images" / "image-1.png"
    assert staged.read_bytes() == b"\x89PNG-fake"
    assert "unused-for-vision" not in captured["prompt"]  # notes are for blind lanes
    session.expire_all()
    assert session.get(Thread, thread.id).spawn_context["images"] == [str(src)]


async def test_spawn_unknown_model_refused_before_capacity(session, make_user, monkeypatch):
    """Fail-closed: an unregistered alias never spawns — no thread row, no
    capacity reservation, no key. The engine never substitutes a model."""
    run = _make_run(session, make_user)
    gw = _FakeGateway()
    lm = ThreadManager(_FakeIngest(), _FakeRelay(), gw)

    acquired = []

    async def fake_acquire(repo):
        acquired.append(repo)
        return True, ""
    monkeypatch.setattr(thread_manager.capacity, "try_acquire", fake_acquire)

    with pytest.raises(ThreadSpawnError, match="not in the registry"):
        await lm.spawn(run, "researcher", "task", "persona", None, [],
                       model="gpt-9-turbo")
    assert acquired == []  # validation precedes the capacity reservation
    assert gw.minted == []
    assert session.query(Thread).count() == 0


async def test_spawn_capacity_denied_raises(session, make_user, monkeypatch):
    run = _make_run(session, make_user)
    lm = ThreadManager(_FakeIngest(), _FakeRelay(), _FakeGateway())

    async def fake_acquire(repo):
        return False, "cap reached"
    monkeypatch.setattr(thread_manager.capacity, "try_acquire", fake_acquire)
    with pytest.raises(ThreadSpawnError, match="cap reached"):
        await lm.spawn(run, "researcher", "task", "persona", None, [])


async def test_spawn_gateway_failure_marks_thread_failed(session, make_user, monkeypatch):
    run = _make_run(session, make_user)
    lm = ThreadManager(_FakeIngest(), _FakeRelay(), _FakeGateway(fail=True))

    async def fake_acquire(repo):
        return True, ""
    monkeypatch.setattr(thread_manager.capacity, "try_acquire", fake_acquire)
    with pytest.raises(ThreadSpawnError, match="gateway key mint failed"):
        await lm.spawn(run, "researcher", "task", "persona", None, [])
    threads = session.query(Thread).all()
    assert threads[0].status == "failed"


async def test_spawn_gateway_failure_releases_writable_repo_lock(session, make_user, monkeypatch):
    # M-65: a gateway failure on a WRITABLE thread must release the per-repo
    # write lock so the next spawn on the same repo isn't blocked forever. The
    # old test used writable_repo=None, so the writable lock was never exercised
    # and a leak would go undetected.
    run = _make_run(session, make_user)
    lm = ThreadManager(_FakeIngest(), _FakeRelay(), _FakeGateway(fail=True))
    monkeypatch.setattr(thread_manager.sandbox_manager, "run_thread_container",
                        lambda *a, **k: "cid")
    # Use the REAL capacity (no try_acquire monkeypatch) so the lock state is
    # observable; reset the singleton to isolate from other tests.
    thread_manager.capacity._reserved = 0
    thread_manager.capacity._reserved_writable = set()
    repo = Repo(name="ServerApp", integration_branch="main")
    session.add(repo); session.commit()
    with pytest.raises(ThreadSpawnError, match="gateway key mint failed"):
        await lm.spawn(run, "developer", "task", "persona", repo, [])
    session.expire_all()
    assert session.query(Thread).filter_by(repo_scope="ServerApp").one().status == "failed"
    # M-65: the writable-repo lock is released — a fresh try_acquire for the
    # same repo succeeds (a leak would block the next spawn forever).
    ok, _ = await thread_manager.capacity.try_acquire("ServerApp")
    assert ok is True
    thread_manager.capacity.release("ServerApp")


async def test_spawn_container_failure_marks_thread_failed(session, make_user, monkeypatch):
    run = _make_run(session, make_user)
    lm = ThreadManager(_FakeIngest(), _FakeRelay(), _FakeGateway())

    async def fake_acquire(repo):
        return True, ""
    monkeypatch.setattr(thread_manager.capacity, "try_acquire", fake_acquire)

    def boom(*a, **k):
        raise RuntimeError("docker exploded")
    monkeypatch.setattr(thread_manager.sandbox_manager, "run_thread_container", boom)
    with pytest.raises(ThreadSpawnError, match="container start failed"):
        await lm.spawn(run, "researcher", "task", "persona", None, [])
    assert session.query(Thread).first().status == "failed"


async def test_spawn_resume_from_thread_inherits_session_id(session, make_user, monkeypatch):
    """A mode-switch respawn must inherit the prior thread's session_id so the
    SDK picks up the conversation, and mount the prior thread's session volume."""
    run = _make_run(session, make_user)
    prior = Thread(id="l-old", run_id=run.id, persona="researcher", status="completed",
                 session_id="sess-prior-abc")
    session.add(prior); session.commit()

    ingest, relay, gw = _FakeIngest(), _FakeRelay(), _FakeGateway()
    lm = ThreadManager(ingest, relay, gw)

    async def fake_acquire(repo):
        return True, ""
    monkeypatch.setattr(thread_manager.capacity, "try_acquire", fake_acquire)

    captured = {}
    def fake_run_container(run, thread, prompt, persona_prompt, permission_mode,
                            writable_repo, context_repos, resume_from_thread_id=None,
                            preserve_workspace=False):
        captured["resume_from_thread_id"] = resume_from_thread_id
        captured["session_id"] = thread.session_id
        return "container-new"
    monkeypatch.setattr(thread_manager.sandbox_manager, "run_thread_container", fake_run_container)

    thread = await lm.spawn(run, "researcher", "task", "persona", None, [],
                          resume_from_thread_id="l-old")
    assert thread.session_id == "sess-prior-abc"
    assert captured["resume_from_thread_id"] == "l-old"
    assert captured["session_id"] == "sess-prior-abc"


async def test_spawn_persists_context_repos_in_spawn_context(session, make_user, monkeypatch):
    """A replacement thread (mode switch or turn-X @mention expansion)
    remounts the exact same repo set the original spawn used. The mount
    snapshot is stored as names in spawn_context so it survives a session
    close and replays identically across replacements."""
    run = _make_run(session, make_user)
    ingest, relay, gw = _FakeIngest(), _FakeRelay(), _FakeGateway()
    lm = ThreadManager(ingest, relay, gw)

    async def fake_acquire(repo):
        return True, ""
    monkeypatch.setattr(thread_manager.capacity, "try_acquire", fake_acquire)
    monkeypatch.setattr(thread_manager.sandbox_manager, "run_thread_container",
                        lambda *a, **k: "container-xyz")

    server = Repo(name="ServerApp", integration_branch="main")
    client = Repo(name="ClientApp", integration_branch="main")
    session.add_all([server, client]); session.commit()

    thread = await lm.spawn(run, "researcher", "task", "persona", None,
                          context_repos=[server, client])
    session.expire_all()
    row = session.get(Thread, thread.id)
    assert row.spawn_context["context_repos"] == ["ServerApp", "ClientApp"]


async def test_spawn_with_no_context_repos_stores_empty_list(session, make_user, monkeypatch):
    """An empty context_repos list still persists (no KeyError on lookup
    during replacement), and a None-style absence is never produced."""
    run = _make_run(session, make_user)
    lm = ThreadManager(_FakeIngest(), _FakeRelay(), _FakeGateway())

    async def fake_acquire(repo):
        return True, ""
    monkeypatch.setattr(thread_manager.capacity, "try_acquire", fake_acquire)
    monkeypatch.setattr(thread_manager.sandbox_manager, "run_thread_container",
                        lambda *a, **k: "container-xyz")

    thread = await lm.spawn(run, "researcher", "task", "persona", None, [])
    session.expire_all()
    row = session.get(Thread, thread.id)
    assert row.spawn_context["context_repos"] == []


async def test_settle_cost_updates_thread_and_run(session, make_user):
    run = _make_run(session, make_user)
    ingest, relay, gw = _FakeIngest(), _FakeRelay(), _FakeGateway(spend=2.25)
    lm = ThreadManager(ingest, relay, gw)
    thread = Thread(id="l1", run_id=run.id, persona="researcher", status="completed",
                gateway_key="vk-1", budget_usd=5.0)
    session.add(thread)
    session.commit()
    spend = await lm.settle_cost("l1")
    assert spend == 2.25
    session.expire_all()
    assert session.get(Thread, "l1").cost_usd == 2.25
    assert session.get(Run, run.id).cost_usd == 2.25


async def test_settle_cost_no_thread_returns_zero(session):
    lm = ThreadManager(_FakeIngest(), _FakeRelay(), _FakeGateway())
    assert await lm.settle_cost("ghost") == 0.0


async def test_settle_cost_no_gateway_key_returns_zero(session, make_user):
    run = _make_run(session, make_user)
    lm = ThreadManager(_FakeIngest(), _FakeRelay(), _FakeGateway())
    thread = Thread(id="l1", run_id=run.id, persona="researcher", status="completed")
    session.add(thread)
    session.commit()
    assert await lm.settle_cost("l1") == 0.0


async def test_release_key_calls_gateway_delete(session, make_user):
    run = _make_run(session, make_user)
    gw = _FakeGateway()
    lm = ThreadManager(_FakeIngest(), _FakeRelay(), gw)
    thread = Thread(id="l1", run_id=run.id, persona="researcher", status="completed", gateway_key="vk-9")
    session.add(thread)
    session.commit()
    await lm.release_key("l1")
    assert "vk-9" in gw.deleted


async def test_release_key_swallows_gateway_error(session, make_user):
    run = _make_run(session, make_user)
    gw = _FakeGateway(fail=True)
    lm = ThreadManager(_FakeIngest(), _FakeRelay(), gw)
    thread = Thread(id="l1", run_id=run.id, persona="researcher", status="completed", gateway_key="vk-9")
    session.add(thread)
    session.commit()
    await lm.release_key("l1")  # should not raise


async def test_release_key_missing_thread_is_noop(session):
    lm = ThreadManager(_FakeIngest(), _FakeRelay(), _FakeGateway())
    await lm.release_key("ghost")


def test_thread_spawn_error_is_runtime_error():
    assert issubclass(ThreadSpawnError, RuntimeError)


# --------------------------------------------------------------- spawn_many (width swarm)
def _specs(n):
    return [{"persona": "explorer", "prompt": f"slice {i}",
             "persona_prompt": "p", "thread_hint": f"explorer-{i}"} for i in range(n)]


async def test_spawn_many_spawns_all_specs(session, make_user, monkeypatch):
    run = _make_run(session, make_user)
    lm = ThreadManager(_FakeIngest(), _FakeRelay(), _FakeGateway())
    monkeypatch.setattr(thread_manager.sandbox_manager, "run_thread_container",
                        lambda *a, **k: "cid")
    threads = await lm.spawn_many(run, _specs(3), [], queue_poll_seconds=0.001)
    assert len(threads) == 3
    session.expire_all()
    assert session.query(Thread).filter_by(run_id=run.id, status="running").count() == 3


async def test_spawn_many_queues_past_cap_and_announces_once(session, make_user, monkeypatch):
    """Over-cap requests queue deterministically AND the UI says so (one
    queued note per waiting thread, not a spam loop)."""
    run = _make_run(session, make_user)
    relay = _FakeRelay()
    lm = ThreadManager(_FakeIngest(), relay, _FakeGateway())
    monkeypatch.setattr(thread_manager.sandbox_manager, "run_thread_container",
                        lambda *a, **k: "cid")
    attempts = iter([(False, "global thread cap (12) reached — queued"), (True, "")])

    async def fake_acquire(repo):
        return next(attempts, (True, ""))
    monkeypatch.setattr(thread_manager.capacity, "try_acquire", fake_acquire)

    threads = await lm.spawn_many(run, _specs(1), [], queue_poll_seconds=0.001)
    assert len(threads) == 1
    # W-H7: the queued notice is a run-scoped NOTE now (the old fake-id
    # thread_status named a thread that didn't exist yet, so every UI client
    # dropped it silently). One note per waiting thread, mentioning the persona.
    notes = [n for n in relay.notes if "queued" in n[1]]
    assert len(notes) == 1
    assert "explorer" in notes[0][1]


async def test_spawn_many_skips_thread_on_non_capacity_failure(session, make_user, monkeypatch):
    """A gateway/container failure sinks ONE thread, never the swarm."""
    run = _make_run(session, make_user)
    lm = ThreadManager(_FakeIngest(), _FakeRelay(), _FakeGateway(fail=True))
    monkeypatch.setattr(thread_manager.sandbox_manager, "run_thread_container",
                        lambda *a, **k: "cid")
    threads = await lm.spawn_many(run, _specs(2), [], queue_poll_seconds=0.001)
    assert threads == []
    session.expire_all()
    assert session.query(Thread).filter_by(status="failed").count() == 2


async def test_finish_thread_stops_container_stamps_completed_releases_key(
        session, make_user, monkeypatch):
    """Blueprint node-end: the idle container dies NOW (freeing the capacity
    slot and the per-repo write lock — 'idle' is an ACTIVE status), then the
    row is stamped completed and the gateway key released."""
    run = _make_run(session, make_user)
    session.add(Thread(id="l1", run_id=run.id, persona="developer", status="idle",
                       container_id="c-1", gateway_key="vk-1"))
    session.commit()
    gw = _FakeGateway()
    relay = _FakeRelay()
    lm = ThreadManager(_FakeIngest(), relay, gw)
    stopped = []
    monkeypatch.setattr(thread_manager.sandbox_manager, "stop_container",
                        lambda cid: stopped.append(cid))
    await lm.finish_thread("l1")
    assert stopped == ["c-1"]  # container dies BEFORE the stamp (order matters)
    session.expire_all()
    row = session.get(Thread, "l1")
    assert row.status == "completed"
    assert row.finished_at is not None
    assert gw.deleted == ["vk-1"]
    # W8-L2: node-end fans the status out — the console's tile goes dark
    # immediately instead of waiting on the next threads poll.
    assert (run.id, "l1", "completed") in relay.published


async def test_finish_thread_leaves_terminal_threads_alone(
        session, make_user, monkeypatch):
    """An honest 'failed' must never be rewritten to a cosmetic 'completed'."""
    run = _make_run(session, make_user)
    session.add(Thread(id="l2", run_id=run.id, persona="developer", status="failed",
                       container_id="c-2"))
    session.commit()
    lm = ThreadManager(_FakeIngest(), _FakeRelay(), _FakeGateway())
    stopped = []
    monkeypatch.setattr(thread_manager.sandbox_manager, "stop_container",
                        lambda cid: stopped.append(cid))
    await lm.finish_thread("l2")
    assert stopped == []  # already terminal: no stop, no re-stamp
    session.expire_all()
    assert session.get(Thread, "l2").status == "failed"
