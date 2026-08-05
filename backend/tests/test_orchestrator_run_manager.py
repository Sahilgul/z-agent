import asyncio

import pytest
from zagent_contracts import RunStage

from app.db.models.thread import Thread
from app.db.models.mode import Mode
from app.db.models.run import Plan, Run
from app.orchestrator import run_manager
from app.orchestrator.run_manager import RunManager


class _FakeIngest:
    def __init__(self):
        self.registered = []
        self.unregistered = []
    def register_run(self, run_id): self.registered.append(run_id)
    def unregister_run(self, run_id): self.unregistered.append(run_id)


class _FakeRelay:
    def __init__(self):
        self.stages = []
        self.threads = []
    async def publish_run_stage(self, run_id, stage, actions):
        self.stages.append((run_id, stage, actions))
    async def publish_thread_status(self, run_id, thread_id, status):
        self.threads.append((run_id, thread_id, status))


class _FakeControl:
    def __init__(self):
        self.interrupted = []
        self.killed = []
        self.nudged = []
    async def interrupt(self, thread_id): self.interrupted.append(thread_id)
    async def kill(self, thread_id): self.killed.append(thread_id)
    async def nudge(self, thread_id, text): self.nudged.append((thread_id, text))


class _FakeLaneManager:
    async def release_key(self, thread_id: str) -> None:  # H-36 test stub
        pass


class _FakeApprovals:
    def __init__(self):
        self.registered = []
        self.unregistered = []
    def register_run(self, run_id): self.registered.append(run_id)
    def unregister_run(self, run_id): self.unregistered.append(run_id)


def _seed_mode(session, name="ask", autonomy_default="supervised", enabled=True):
    session.add(Mode(name=name, autonomy_default=autonomy_default, enabled=enabled,
                     persona_prompt="p", permission_mode="default"))
    session.commit()


def _make_manager():
    ingest, relay, control = _FakeIngest(), _FakeRelay(), _FakeControl()
    rm = RunManager(ingest, relay, _FakeLaneManager(), control)
    return rm, ingest, relay, control


async def test_create_run_persists_and_registers(session, make_user, monkeypatch):
    u = make_user("a")
    _seed_mode(session)
    rm, ingest, relay, _ = _make_manager()
    async def noop(*a, **k): pass
    monkeypatch.setattr(rm, "_execute", noop)
    run = await rm.create_run(source="button", initiated_by=u.id, mode_name="ask", task="do it")
    assert run.stage == RunStage.QUEUED.value
    assert run.created_by == u.id
    assert run.mode == "ask"
    assert run.id in ingest.registered
    assert relay.stages[0][1] == RunStage.QUEUED.value
    rm._tasks[run.id].cancel()
    try: await rm._tasks[run.id]
    except asyncio.CancelledError: pass


async def test_create_run_unknown_mode_raises(session, make_user):
    u = make_user("a")
    rm, _, _, _ = _make_manager()
    with pytest.raises(ValueError, match="unknown or disabled mode"):
        await rm.create_run(source="button", initiated_by=u.id, mode_name="ghost", task="x")


async def test_create_run_registers_with_approval_service(session, make_user, monkeypatch):
    """Regression: ApprovalService.register_run was never called, so the
    approvals consumer idled on an empty stream set and no Approval row was
    ever created — engine approval cards timed out into denies unanswered."""
    u = make_user("a")
    _seed_mode(session)
    approvals = _FakeApprovals()
    rm, ingest, _, _ = _make_manager()
    rm.approvals = approvals
    async def noop(*a, **k): pass
    monkeypatch.setattr(rm, "_execute", noop)
    run = await rm.create_run(source="button", initiated_by=u.id, mode_name="ask", task="do it")
    assert run.id in approvals.registered
    assert run.id in ingest.registered
    rm._tasks[run.id].cancel()
    try: await rm._tasks[run.id]
    except asyncio.CancelledError: pass


async def test_abandon_run_unregisters_from_approval_service(session, make_user, monkeypatch):
    u = make_user("a")
    approvals = _FakeApprovals()
    rm, _, _, _ = _make_manager()
    rm.approvals = approvals
    run = Run(id="r1", created_by=u.id, mode="ask", stage=RunStage.INVESTIGATING.value)
    session.add(run)
    session.commit()
    monkeypatch.setattr(run_manager.sandbox_manager, "shred_workspace", lambda *a, **k: None)
    await rm.abandon_run("r1")
    assert "r1" in approvals.unregistered


async def test_create_run_disabled_mode_raises(session, make_user):
    u = make_user("a")
    _seed_mode(session, name="ask", enabled=False)
    rm, _, _, _ = _make_manager()
    with pytest.raises(ValueError, match="unknown or disabled mode"):
        await rm.create_run(source="button", initiated_by=u.id, mode_name="ask", task="x")


async def test_create_run_autonomy_override(session, make_user, monkeypatch):
    u = make_user("a")
    _seed_mode(session, autonomy_default="supervised")
    rm, _, _, _ = _make_manager()
    async def noop(*a, **k): pass
    monkeypatch.setattr(rm, "_execute", noop)
    run = await rm.create_run(source="button", initiated_by=u.id, mode_name="ask",
                              task="x", autonomy="autonomous")
    assert run.autonomy == "autonomous"
    rm._tasks[run.id].cancel()
    try: await rm._tasks[run.id]
    except asyncio.CancelledError: pass


async def test_stop_run_interrupts_and_cancels_task(session, make_user):
    u = make_user("a")
    _seed_mode(session)
    rm, ingest, relay, control = _make_manager()
    run = Run(id="r1", created_by=u.id, mode="ask", stage=RunStage.INVESTIGATING.value)
    thread = Thread(id="l1", run_id="r1", persona="researcher", status="running")
    session.add_all([run, thread])
    session.commit()
    rm._tasks["r1"] = asyncio.create_task(asyncio.sleep(100))
    await rm.stop_run("r1")
    await asyncio.sleep(0)
    # M-62: assert the cancelled task actually raises CancelledError. The
    # old test verified the run/thread status but never that the task was
    # cancelled, so a stop_run that failed to cancel the task would pass.
    with pytest.raises(asyncio.CancelledError):
        await rm._tasks["r1"]
    session.expire_all()
    assert session.get(Run, "r1").stage == RunStage.INTERRUPTED.value
    # C-15: stop_run must write the Thread DB status to "stopped" so the
    # capacity semaphore releases the slot — the relay-only publish left the
    # row at "running" and the slot leaked forever.
    assert session.get(Thread, "l1").status == "stopped"
    assert session.get(Thread, "l1").finished_at is not None
    assert "l1" in control.interrupted
    assert relay.threads[-1] == ("r1", "l1", "stopped")
    assert relay.stages[-1][1] == RunStage.INTERRUPTED.value
    assert rm._tasks["r1"].cancelled() or rm._tasks["r1"].done()


async def test_abandon_run_kills_and_shreds(session, make_user, monkeypatch):
    u = make_user("a")
    rm, ingest, relay, control = _make_manager()
    run = Run(id="r1", created_by=u.id, mode="ask", stage=RunStage.INVESTIGATING.value)
    thread = Thread(id="l1", run_id="r1", persona="researcher", status="running", container_id="c1")
    session.add_all([run, thread])
    session.commit()
    stopped = []
    shredded = []
    monkeypatch.setattr(run_manager.sandbox_manager, "stop_container", lambda cid: stopped.append(cid))
    monkeypatch.setattr(run_manager.sandbox_manager, "shred_workspace", lambda rid: shredded.append(rid))
    rm._tasks["r1"] = asyncio.create_task(asyncio.sleep(100))
    await rm.abandon_run("r1")
    session.expire_all()
    assert session.get(Run, "r1").stage == RunStage.ABANDONED.value
    assert "l1" in control.killed
    assert stopped == ["c1"]
    assert shredded == ["r1"]
    assert "r1" in ingest.unregistered
    assert relay.stages[-1][1] == RunStage.ABANDONED.value


async def test_abandon_run_cancels_task_before_shred(session, make_user, monkeypatch):
    """G-11: abandon_run must cancel the run task BEFORE shredding the
    workspace — cancelling first stops the worker from writing into the
    workspace mid-shred. The existing test verified the actions happened
    but not the ordering. Use a fake task that records when cancel() is
    called and assert cancel precedes shred."""
    u = make_user("a")
    rm, ingest, relay, control = _make_manager()
    run = Run(id="r1", created_by=u.id, mode="ask", stage=RunStage.INVESTIGATING.value)
    thread = Thread(id="l1", run_id="r1", persona="researcher", status="running", container_id="c1")
    session.add_all([run, thread]); session.commit()

    order: list[str] = []

    class _FakeTask:
        def __init__(self) -> None:
            self._done = False
        def done(self) -> bool:
            return self._done
        def cancel(self) -> bool:
            order.append("cancel")
            self._done = True
            return True

    rm._tasks["r1"] = _FakeTask()
    monkeypatch.setattr(run_manager.sandbox_manager, "stop_container", lambda cid: None)

    def _shred(rid):
        order.append("shred")
    monkeypatch.setattr(run_manager.sandbox_manager, "shred_workspace", _shred)

    await rm.abandon_run("r1")
    # The run task was cancelled, then the workspace was shredded — in that order.
    assert order == ["cancel", "shred"]


async def test_nudge_thread_sets_running(session, make_user):
    u = make_user("a")
    rm, _, relay, control = _make_manager()
    run = Run(id="r1", created_by=u.id, mode="ask", stage=RunStage.INVESTIGATING.value)
    thread = Thread(id="l1", run_id="r1", persona="researcher", status="idle")
    session.add_all([run, thread])
    session.commit()
    await rm.nudge_thread("r1", "l1", "hurry up")
    session.expire_all()
    assert session.get(Thread, "l1").status == "running"
    assert control.nudged == [("l1", "hurry up")]
    assert relay.threads[-1] == ("r1", "l1", "running")


async def test_nudge_thread_missing_thread_is_noop(session, make_user):
    u = make_user("a")
    rm, _, _, control = _make_manager()
    run = Run(id="r1", created_by=u.id, mode="ask", stage=RunStage.INVESTIGATING.value)
    session.add(run); session.commit()
    await rm.nudge_thread("r1", "ghost", "x")
    # H-50: a missing thread has no worker — the nudge must be a true no-op,
    # not publish a "running" status for a ghost thread. The old test
    # asserted control.nudged == [("ghost", "x")] and codified the bug.
    assert control.nudged == []


async def test_nudge_thread_refuses_to_resurrect_terminal_thread(session, make_user):
    """C-16: nudge_thread must NOT flip a terminal thread back to "running".
    The old code set status="running" unconditionally, resurrecting
    stopped/completed/replaced/timed_out threads and leaking capacity."""
    from app.orchestrator.semaphores import ACTIVE_STATUSES
    u = make_user("a")
    rm, _, relay, control = _make_manager()
    run = Run(id="r1", created_by=u.id, mode="ask", stage=RunStage.INVESTIGATING.value)
    for terminal in ("stopped", "completed", "replaced", "timed_out", "failed"):
        session.add(Thread(
            id=f"l-{terminal}", run_id="r1", persona="researcher", status=terminal))
    session.add(run); session.commit()
    for terminal in ("stopped", "completed", "replaced", "timed_out", "failed"):
        tid = f"l-{terminal}"
        await rm.nudge_thread("r1", tid, "hurry up")
        session.expire_all()
        # status unchanged — the dead thread was NOT resurrected
        assert session.get(Thread, tid).status == terminal, (
            f"nudge resurrected a {terminal} thread")
        # the control channel was NOT nudged (the thread is dead)
        assert (tid, "hurry up") not in control.nudged
    # relay should not publish "running" for any terminal thread
    assert not any(t == "running" for _, _, t in relay.threads)
    # sanity: ACTIVE_STATUSES does not include any terminal status
    assert all(terminal not in ACTIVE_STATUSES
               for terminal in ("stopped", "completed", "replaced", "timed_out", "failed"))


async def test_nudge_thread_resumes_input_required_thread(session, make_user):
    """input_required is not terminal: the worker is parked in its idle nudge
    loop (blocked-escalation) with a live container subscribed to the control
    channel. Refusing the nudge stranded the run — the user's reply was
    persisted but never delivered, and the thread polled forever."""
    from app.orchestrator.semaphores import ACTIVE_STATUSES
    assert "input_required" not in ACTIVE_STATUSES  # capacity accounting untouched
    u = make_user("a")
    rm, _, relay, control = _make_manager()
    run = Run(id="r1", created_by=u.id, mode="ask", stage=RunStage.INVESTIGATING.value)
    thread = Thread(id="l1", run_id="r1", persona="researcher", status="input_required")
    session.add_all([run, thread])
    session.commit()
    await rm.nudge_thread("r1", "l1", "use option B instead")
    session.expire_all()
    assert session.get(Thread, "l1").status == "running"
    assert control.nudged == [("l1", "use option B instead")]
    assert relay.threads[-1] == ("r1", "l1", "running")


async def test_reconcile_on_boot_interrupts_active_runs(session, make_user):
    u = make_user("a")
    rm, _, _, _ = _make_manager()
    for stage in (RunStage.PROVISIONING.value, RunStage.INVESTIGATING.value,
                  RunStage.DEVELOPING.value, RunStage.VERIFYING.value,
                  RunStage.COMPLETED.value):
        r = Run(id=f"r-{stage}", created_by=u.id, mode="ask", stage=stage)
        session.add(r)
    session.commit()
    count = await rm.reconcile_on_boot()
    assert count == 4  # completed is not in the active set
    session.expire_all()
    assert session.get(Run, f"r-{RunStage.COMPLETED.value}").stage == RunStage.COMPLETED.value
    assert session.get(Run, f"r-{RunStage.INVESTIGATING.value}").stage == RunStage.INTERRUPTED.value


async def test_reconcile_on_boot_interrupts_planning_and_queued_runs(session, make_user):
    """G-13: the existing reconcile test covered PROVISIONING/INVESTIGATING/
    DEVELOPING/VERIFYING but NOT PLANNING (or QUEUED — added by H-40). A run
    sitting in PLANNING across a crash must be interrupted on boot too, or it
    strands as a zombie (capacity leak + a stuck inbox). Seed PLANNING +
    QUEUED runs with live threads and assert both are interrupted and their
    threads stopped."""
    u = make_user("a")
    rm, _, _, _ = _make_manager()
    for stage in (RunStage.QUEUED.value, RunStage.PLANNING.value):
        r = Run(id=f"r-{stage}", created_by=u.id, mode="plan", stage=stage)
        session.add(r)
        session.add(Thread(id=f"l-{stage}", run_id=f"r-{stage}", persona="planner",
                          status="running"))
    session.commit()
    count = await rm.reconcile_on_boot()
    assert count == 2
    session.expire_all()
    for stage in (RunStage.QUEUED.value, RunStage.PLANNING.value):
        assert session.get(Run, f"r-{stage}").stage == RunStage.INTERRUPTED.value
        assert session.get(Thread, f"l-{stage}").status == "stopped"


async def test_execute_failure_path_marks_failed(session, make_user, monkeypatch):
    u = make_user("a")
    _seed_mode(session)
    rm, _, relay, _ = _make_manager()
    run = Run(id="r1", created_by=u.id, mode="ask", stage=RunStage.QUEUED.value)
    session.add(run); session.commit()

    class BoomBlueprint:
        async def execute(self, ctx):
            raise RuntimeError("agent crashed")
    monkeypatch.setattr(run_manager, "blueprint_for", lambda name: BoomBlueprint())
    await rm._execute("r1", "task", None)
    session.expire_all()
    assert session.get(Run, "r1").stage == RunStage.FAILED.value
    assert session.get(Run, "r1").finished_at is not None
    assert relay.stages[-1][1] == RunStage.FAILED.value


async def test_guarded_execute_reraises_cancelled_not_marks_failed(session, make_user, monkeypatch):
    """G-14: _guarded_execute must RE-RAISE asyncio.CancelledError (not
    swallow it as a failure). A CancelledError means the run's task was
    cancelled (stop/abandon/shutdown) — marking the run FAILED would
    overwrite the intended terminal state (ABANDONED/STOPPED) and confuse
    the audit. Assert CancelledError propagates and the run is NOT flipped
    to FAILED."""
    u = make_user("a")
    _seed_mode(session)
    rm, _, relay, _ = _make_manager()
    run = Run(id="r1", created_by=u.id, mode="ask", stage=RunStage.QUEUED.value)
    session.add(run); session.commit()

    class CancelledBlueprint:
        async def execute(self, ctx):
            raise asyncio.CancelledError()
    monkeypatch.setattr(run_manager, "blueprint_for", lambda name: CancelledBlueprint())

    with pytest.raises(asyncio.CancelledError):
        await rm._execute("r1", "task", None)
    session.expire_all()
    # The run stays QUEUED — CancelledError did NOT flip it to FAILED.
    assert session.get(Run, "r1").stage == RunStage.QUEUED.value
    assert session.get(Run, "r1").finished_at is None


# --------------------------------------------------------------- switch_mode
async def test_switch_mode_updates_run_mode(session, make_user):
    u = make_user("a")
    _seed_mode(session, name="ask")
    _seed_mode(session, name="plan")
    rm, _, _, _ = _make_manager()
    run = Run(id="r1", created_by=u.id, mode="ask", stage=RunStage.INVESTIGATING.value, title="t")
    session.add(run); session.commit()
    await rm.switch_mode("r1", "plan")
    session.expire_all()
    assert session.get(Run, "r1").mode == "plan"


async def test_switch_mode_rejects_unknown_mode(session, make_user):
    u = make_user("a")
    _seed_mode(session, name="ask")
    rm, _, _, _ = _make_manager()
    run = Run(id="r1", created_by=u.id, mode="ask", stage=RunStage.INVESTIGATING.value, title="t")
    session.add(run); session.commit()
    with pytest.raises(ValueError, match="unknown or disabled mode"):
        await rm.switch_mode("r1", "ghost")


async def test_switch_mode_rejects_disabled_mode(session, make_user):
    u = make_user("a")
    _seed_mode(session, name="ask")
    _seed_mode(session, name="plan", enabled=False)
    rm, _, _, _ = _make_manager()
    run = Run(id="r1", created_by=u.id, mode="ask", stage=RunStage.INVESTIGATING.value, title="t")
    session.add(run); session.commit()
    with pytest.raises(ValueError, match="unknown or disabled mode"):
        await rm.switch_mode("r1", "plan")


# --------------------------------------------------------------- plan HITL chains
async def test_continue_to_development_runs_development_blueprint(session, make_user, monkeypatch):
    u = make_user("a")
    _seed_mode(session, name="development")
    rm, _, _, _ = _make_manager()
    run = Run(id="r1", created_by=u.id, mode="plan", stage=RunStage.AWAITING_USER.value, title="t", repo="ServerApp")
    session.add(run); session.commit()

    class SpyBlueprint:
        ran = False
        async def execute(self, ctx):
            SpyBlueprint.ran = True

    monkeypatch.setattr(run_manager, "blueprint_for", lambda name: SpyBlueprint())
    await rm.continue_to_development("r1")
    # M-64: await the actual fire-and-forget task instead of sleep(0) — a
    # single yield was flaky (the task might not complete in one loop tick).
    await rm._tasks["r1"]
    assert SpyBlueprint.ran is True
    assert "r1" in rm._tasks


async def test_replan_injects_critic_notes(session, make_user, monkeypatch):
    u = make_user("a")
    _seed_mode(session, name="plan")
    rm, _, _, _ = _make_manager()
    run = Run(id="r1", created_by=u.id, mode="plan", stage=RunStage.PLANNING.value, title="t", repo="ServerApp")
    session.add(run); session.commit()
    captured = {}

    class SpyBlueprint:
        async def execute(self, ctx):
            captured["notes"] = ctx.artifacts.get("critic_notes")

    monkeypatch.setattr(run_manager, "blueprint_for", lambda name: SpyBlueprint())
    await rm.replan("r1", notes="fix citations")
    # M-64: await the actual task instead of sleep(0) (flaky single-yield).
    await rm._tasks["r1"]
    assert captured["notes"] == "fix citations"


async def test_create_pr_calls_delivery_and_stages_pr_ready(session, make_user, monkeypatch):
    from app.db.models.delivery import PrLink
    u = make_user("a")
    rm, _, relay, _ = _make_manager()
    run = Run(id="r1", created_by=u.id, mode="development", stage=RunStage.VERIFYING.value,
              title="ship it", repo="ServerApp")
    session.add(run); session.commit()
    from app.services import delivery

    async def fake_open(run_id, repo_name, ws, ado_client=None):
        return PrLink(run_id=run_id, repo=repo_name, branch="agent/x", ado_pr_id=7, status="open")
    monkeypatch.setattr(delivery, "open_pr", fake_open)
    await rm.create_pr("r1")
    session.expire_all()
    assert session.get(Run, "r1").stage == RunStage.PR_READY.value


async def test_create_pr_falls_back_to_computed_workspace(session, make_user, monkeypatch):
    from app.db.models.delivery import PrLink
    u = make_user("a")
    rm, _, _, _ = _make_manager()
    run = Run(id="r1", created_by=u.id, mode="development", stage=RunStage.VERIFYING.value,
              title="t", repo="ServerApp")
    session.add(run); session.commit()
    from app.services import delivery
    seen = {}

    async def fake_open(run_id, repo_name, ws, ado_client=None):
        seen["workspace"] = ws
        return PrLink(run_id=run_id, repo=repo_name, branch="b", ado_pr_id=1, status="open")
    monkeypatch.setattr(delivery, "open_pr", fake_open)
    await rm.create_pr("r1")
    # L-29: the old `assert "r1" in seen["workspace"]` was a substring check
    # that passed for any path containing "r1" (e.g. a workspaces dir named
    # "workspaces-r1"). The workspace is keyed by the run id as a path
    # component, so assert that precisely.
    from pathlib import Path
    assert "r1" in Path(seen["workspace"]).parts


async def test_merge_pr_calls_delivery_and_completes(session, make_user, monkeypatch):
    u = make_user("a")
    rm, _, relay, _ = _make_manager()
    run = Run(id="r1", created_by=u.id, mode="development", stage=RunStage.PR_READY.value,
              title="t", repo="ServerApp")
    session.add(run); session.commit()
    from app.services import delivery

    async def fake_merge(run_id, user_id, ado_client=None):
        return {"link": None, "handoff_url": None}
    monkeypatch.setattr(delivery, "merge_pr", fake_merge)
    handoff = await rm.merge_pr("r1", u.id)
    assert handoff is None
    session.expire_all()
    assert session.get(Run, "r1").stage == RunStage.COMPLETED.value
    assert session.get(Run, "r1").finished_at is not None


async def test_merge_pr_handoff_keeps_run_pr_ready(session, make_user, monkeypatch):
    """merge_native_ui path: no completion happened, so the run must NOT move
    to completed — the human finishes in ADO; the handoff URL reaches the API."""
    u = make_user("a")
    rm, _, _, _ = _make_manager()
    run = Run(id="r1", created_by=u.id, mode="development", stage=RunStage.PR_READY.value,
              title="t", repo="ServerApp")
    session.add(run); session.commit()
    from app.services import delivery

    async def fake_merge(run_id, user_id, ado_client=None):
        return {"link": None, "handoff_url": "https://dev.azure.com/o/p/_git/r/pullrequest/9"}
    monkeypatch.setattr(delivery, "merge_pr", fake_merge)
    handoff = await rm.merge_pr("r1", u.id)
    assert handoff.endswith("pullrequest/9")
    session.expire_all()
    stayed = session.get(Run, "r1")
    assert stayed.stage == RunStage.PR_READY.value
    assert stayed.finished_at is None


# --------------------------------------------------------------- thread controls
async def test_stop_thread_interrupts_and_marks_stopped(session, make_user):
    u = make_user("a")
    rm, _, relay, control = _make_manager()
    run = Run(id="r1", created_by=u.id, mode="agent-rnd", stage=RunStage.INVESTIGATING.value)
    thread = Thread(id="l1", run_id="r1", persona="explorer", status="running")
    session.add_all([run, thread]); session.commit()
    await rm.stop_thread("r1", "l1")
    session.expire_all()
    stopped = session.get(Thread, "l1")
    assert stopped.status == "stopped"
    assert stopped.finished_at is not None
    assert control.interrupted == ["l1"]
    assert relay.threads[-1] == ("r1", "l1", "stopped")


async def test_pin_finding_records_event(session, make_user):
    u = make_user("a")
    rm, _, relay, _ = _make_manager()
    run = Run(id="r1", created_by=u.id, mode="agent-rnd", stage=RunStage.INVESTIGATING.value)
    thread = Thread(id="l1", run_id="r1", persona="explorer", status="idle", next_seq=3)
    session.add_all([run, thread]); session.commit()
    await rm.pin_finding("r1", "l1", "dedupe key is normalize()")
    session.expire_all()
    from app.db.models.event import Event
    ev = session.query(Event).filter_by(run_id="r1", type="pin").one()
    assert ev.thread_id == "l1"
    assert ev.payload["note"] == "dedupe key is normalize()"
    assert relay.threads[-1] == ("r1", "l1", "pinned")


async def test_pin_finding_wrong_run_raises(session, make_user):
    u = make_user("a")
    rm, _, _, _ = _make_manager()
    run = Run(id="r1", created_by=u.id, mode="agent-rnd", stage=RunStage.INVESTIGATING.value)
    thread = Thread(id="l1", run_id="other-run", persona="explorer", status="idle")
    session.add_all([run, thread]); session.commit()
    with pytest.raises(ValueError, match="thread not found"):
        await rm.pin_finding("r1", "l1")


async def test_kill_replace_respawns_with_original_context(session, make_user, monkeypatch):
    u = make_user("a")
    rm, _, relay, control = _make_manager()
    run = Run(id="r1", created_by=u.id, mode="agent-rnd", stage=RunStage.INVESTIGATING.value)
    thread = Thread(id="l1", run_id="r1", persona="explorer", status="running",
                spawn_context={"prompt": "trace the webhook leg", "persona_prompt": "be an explorer"})
    session.add_all([run, thread]); session.commit()

    captured = {}

    class _Replacement:
        id = "thread-new"

    async def fake_spawn(run, persona, prompt, persona_prompt, writable_repo, context_repos,
                         resume_session=False, resume_from_thread_id=None):
        captured.update({"persona": persona, "prompt": prompt,
                         "persona_prompt": persona_prompt,
                         "resume_from_thread_id": resume_from_thread_id})
        return _Replacement()
    rm.thread_manager.spawn = fake_spawn

    replacement = await rm.kill_replace_thread("r1", "l1")
    assert replacement.id == "thread-new"
    assert captured["prompt"] == "trace the webhook leg"
    assert captured["persona_prompt"] == "be an explorer"
    session.expire_all()
    assert session.get(Thread, "l1").status == "replaced"
    assert relay.threads[-1] == ("r1", "thread-new", "running")


async def test_kill_replace_passes_resume_from_thread_id(session, make_user, monkeypatch):
    """kill_replace must mount the old thread's session volume and inherit its
    session_id so the replacement actually resumes — the docstring's claim
    that was not true before resume_from_thread_id existed."""
    u = make_user("a")
    rm, _, _, _ = _make_manager()
    run = Run(id="r1", created_by=u.id, mode="agent-rnd", stage=RunStage.INVESTIGATING.value)
    thread = Thread(id="l1", run_id="r1", persona="explorer", status="running",
                session_id="sess-old-123",
                spawn_context={"prompt": "trace the webhook leg", "persona_prompt": "be an explorer"})
    session.add_all([run, thread]); session.commit()

    captured = {}

    class _Replacement:
        id = "thread-new"

    async def fake_spawn(run, persona, prompt, persona_prompt, writable_repo, context_repos,
                         resume_session=False, resume_from_thread_id=None):
        captured["resume_from_thread_id"] = resume_from_thread_id
        return _Replacement()
    rm.thread_manager.spawn = fake_spawn

    await rm.kill_replace_thread("r1", "l1")
    assert captured["resume_from_thread_id"] == "l1"


async def test_kill_replace_waits_for_old_container_before_spawn(session, make_user, monkeypatch):
    """G-12: kill_replace must WAIT for the old container to die (H-37) BEFORE
    spawning the replacement — otherwise two containers mount the same session
    volume at once (corruption). The existing tests verified resume_from_thread_id
    is passed but not the wait-before-spawn ordering. Record the call order and
    assert wait_for_container_exit precedes spawn."""
    u = make_user("a")
    rm, _, _, _ = _make_manager()
    run = Run(id="r1", created_by=u.id, mode="agent-rnd", stage=RunStage.INVESTIGATING.value)
    thread = Thread(id="l1", run_id="r1", persona="explorer", status="running",
                container_id="c-old",
                spawn_context={"prompt": "trace the webhook leg"})
    session.add_all([run, thread]); session.commit()

    order: list[str] = []

    class _Replacement:
        id = "thread-new"

    async def fake_spawn(run, persona, prompt, persona_prompt, writable_repo, context_repos,
                         resume_session=False, resume_from_thread_id=None):
        order.append("spawn")
        return _Replacement()
    rm.thread_manager.spawn = fake_spawn

    def _wait(cid):
        order.append("wait_for_exit")
    monkeypatch.setattr(run_manager.sandbox_manager, "wait_for_container_exit", _wait)

    await rm.kill_replace_thread("r1", "l1")
    # The old container must be gone BEFORE the replacement spawns.
    assert order == ["wait_for_exit", "spawn"]


async def test_kill_replace_wrong_run_raises(session, make_user):
    u = make_user("a")
    rm, _, _, _ = _make_manager()
    run = Run(id="r1", created_by=u.id, mode="agent-rnd", stage=RunStage.INVESTIGATING.value)
    thread = Thread(id="l1", run_id="other-run", persona="explorer", status="running")
    session.add_all([run, thread]); session.commit()
    with pytest.raises(ValueError, match="thread not found"):
        await rm.kill_replace_thread("r1", "l1")


# --------------------------------------------------------------- start_plan (debug -> plan promotion)
async def test_start_plan_chains_into_plan_blueprint_with_seed(session, make_user, monkeypatch):
    u = make_user("a")
    _seed_mode(session, name="plan")
    rm, _, _, _ = _make_manager()
    run = Run(id="r1", created_by=u.id, mode="debug", stage=RunStage.AWAITING_USER.value,
              title="Bug: dedupe", repo="ServerApp")
    plan = Plan(run_id="r1", structured={"title": "Fix dedupe", "steps": [{"index": 0, "title": "s0"}]},
                status="draft")
    session.add_all([run, plan]); session.commit()
    captured = {}

    class SpyBlueprint:
        async def execute(self, ctx):
            captured["seed"] = ctx.artifacts.get("seed_plan")
            captured["notes"] = ctx.artifacts.get("critic_notes")

    monkeypatch.setattr(run_manager, "blueprint_for", lambda name: SpyBlueprint())
    await rm.start_plan("r1")
    await asyncio.sleep(0)
    assert captured["seed"] == plan.structured
    assert "promoted from debug" in captured["notes"]


async def test_start_plan_with_no_draft_seeds_none(session, make_user, monkeypatch):
    u = make_user("a")
    _seed_mode(session, name="plan")
    rm, _, _, _ = _make_manager()
    run = Run(id="r1", created_by=u.id, mode="debug", stage=RunStage.AWAITING_USER.value,
              title="t", repo="ServerApp")
    session.add(run); session.commit()
    captured = {}

    class SpyBlueprint:
        async def execute(self, ctx):
            captured["seed"] = ctx.artifacts.get("seed_plan")

    monkeypatch.setattr(run_manager, "blueprint_for", lambda name: SpyBlueprint())
    await rm.start_plan("r1")
    await asyncio.sleep(0)
    assert captured["seed"] is None


# --------------------------------------------------------------- title hydration (B4)
async def test_create_run_hydrates_generic_title_from_work_item(session, make_user, monkeypatch):
    u = make_user("a")
    _seed_mode(session)
    rm, _, _, _ = _make_manager()
    async def noop(*a, **k): pass
    monkeypatch.setattr(rm, "_execute", noop)
    from app.services import hydration
    async def fake_hydrate(work_item_id, task, ado_client=None):
        return "Bug: dedupe drift on normalize"
    monkeypatch.setattr(hydration, "hydrate_title", fake_hydrate)
    run = await rm.create_run(source="button", initiated_by=u.id, mode_name="ask",
                              task="", work_item_id=42)
    assert run.title == "Bug: dedupe drift on normalize"
    rm._tasks[run.id].cancel()
    try: await rm._tasks[run.id]
    except asyncio.CancelledError: pass


async def test_create_run_keeps_typed_title(session, make_user, monkeypatch):
    u = make_user("a")
    _seed_mode(session)
    rm, _, _, _ = _make_manager()
    async def noop(*a, **k): pass
    monkeypatch.setattr(rm, "_execute", noop)
    from app.services import hydration
    async def boom(*a, **k):
        raise AssertionError("hydrate_title must not run for a real typed title")
    monkeypatch.setattr(hydration, "hydrate_title", boom)
    run = await rm.create_run(source="button", initiated_by=u.id, mode_name="ask",
                              task="Investigate dedupe drift", work_item_id=42)
    assert run.title == "Investigate dedupe drift"
    rm._tasks[run.id].cancel()
    try: await rm._tasks[run.id]
    except asyncio.CancelledError: pass


# --------------------------------------------------------------- chained failure handling (B5)
async def test_chained_blueprint_failure_marks_run_failed(session, make_user, monkeypatch):
    """B5: a chained blueprint (approve/reject/start_plan chain) that raises must
    transition the run to FAILED + relay — never die silently mid-stage."""
    u = make_user("a")
    _seed_mode(session, name="development")
    rm, _, relay, _ = _make_manager()
    run = Run(id="r1", created_by=u.id, mode="plan", stage=RunStage.AWAITING_USER.value,
              title="t", repo="ServerApp")
    session.add(run); session.commit()

    class BoomBlueprint:
        async def execute(self, ctx):
            raise RuntimeError("no approved plan to develop")

    monkeypatch.setattr(run_manager, "blueprint_for", lambda name: BoomBlueprint())
    await rm.continue_to_development("r1")
    task = rm._tasks["r1"]
    await task  # guarded: the exception is handled inside, the task itself completes
    session.expire_all()
    assert session.get(Run, "r1").stage == RunStage.FAILED.value
    assert session.get(Run, "r1").finished_at is not None
    assert relay.stages[-1] == ("r1", RunStage.FAILED.value, [])
