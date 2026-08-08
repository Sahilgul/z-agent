"""Guardian + Responder tests: normalizers, circuit breaker
(max attempts + repeated-signature halt), comment routing (unknown PR, nudge
active thread, resume finished thread from session volume). run_manager is a fake.
"""


from collegium_contracts.triggers import TriggerEvent, TriggerSource

from app.db.models.delivery import PrLink
from app.db.models.repo import Repo
from app.db.models.run import Run
from app.db.models.thread import Thread
from app.db.models.trigger import Trigger
from app.services import guardian, triggers


class FakeThreadMgr:
    def __init__(self):
        self.spawns = []

    async def spawn(self, run, persona, prompt, persona_prompt, writable_repo,
                    context_repos, resume_session=False):
        self.spawns.append({"persona": persona, "prompt": prompt,
                            "resume_session": resume_session,
                            "writable_repo": writable_repo})
        return type("L", (), {"id": "thread-new"})()


class FakeRM:
    def __init__(self):
        self.created = []
        self.nudges = []
        self.thread_manager = FakeThreadMgr()

    async def create_run(self, source, initiated_by, mode_name, task, autonomy=None, **kw):
        import uuid
        run = type("R", (), {"id": str(uuid.uuid4())})()
        self.created.append({"id": run.id, "mode": mode_name, "task": task,
                             "autonomy": autonomy, "by": initiated_by})
        return run

    async def nudge_thread(self, run_id, thread_id, text):
        self.nudges.append({"run_id": run_id, "thread_id": thread_id, "text": text})


def _build_event(pr_id=77, build=9001, tasks=("npm test",)):
    return TriggerEvent(
        source=TriggerSource.ADO_WEBHOOK, external_id=str(build), revision=0,
        event_type="build.failed",
        payload={"pr_id": pr_id, "repo": "ServerApp", "definition": "ci-main",
                 "failed_tasks": list(tasks)})


def _comment_event(pr_id=77, comment=501, text="please add a test"):
    return TriggerEvent(
        source=TriggerSource.ADO_WEBHOOK, external_id=str(pr_id), revision=comment,
        event_type="pr.comment", changed_by_descriptor="desc-reviewer",
        payload={"pr_id": pr_id, "text": text, "author": "Reviewer"})


def _trigger_row(session, name, event_type):
    t = Trigger(name=name, source="ado_webhook",
                filter_json={"event_type": event_type}, mode="development",
                autonomy="gated", owner_resolution="system", rate_limit_per_hour=20)
    session.add(t)
    session.commit()
    return t


# ----------------------------------------------------------------- normalizers
def test_normalize_build_failed():
    body = {"resource": {"id": 9001, "result": "failed",
                         "triggerInfo": {"pr.number": "77"},
                         "repository": {"name": "ServerApp"},
                         "definition": {"name": "ci-main"},
                         "timeline": {"tasks": [
                             {"name": "npm test", "result": "failed"},
                             {"name": "build", "result": "succeeded"}]}}}
    ev = triggers.normalize_ado_build(body)
    assert ev.event_type == "build.failed" and ev.external_id == "9001"
    assert ev.payload["pr_id"] == 77
    assert ev.payload["failed_tasks"] == ["npm test"]


def test_normalize_pr_comment():
    body = {"resource": {"pullRequest": {"pullRequestId": 77},
                         "comment": {"id": 501, "content": "add a test",
                                     "author": {"id": "desc-r", "displayName": "Reviewer"}}}}
    ev = triggers.normalize_ado_pr_comment(body)
    assert ev.event_type == "pr.comment"
    assert ev.idempotency_key == ("ado_webhook", "77", 501)
    assert ev.payload["text"] == "add a test"


# ---------------------------------------------------------------- circuit breaker
def test_failure_signature_stable_and_distinct():
    a = guardian.failure_signature({"repo": "S", "definition": "ci", "failed_tasks": ["t1", "t2"]})
    b = guardian.failure_signature({"repo": "S", "definition": "ci", "failed_tasks": ["t2", "t1"]})
    c = guardian.failure_signature({"repo": "S", "definition": "ci", "failed_tasks": ["t3"]})
    assert a == b and a != c


async def test_guardian_halts_on_max_attempts(session, monkeypatch):
    monkeypatch.setattr(triggers.get_settings(), "guardian_max_attempts", 2)
    monkeypatch.setattr(guardian.get_settings(), "guardian_max_attempts", 2)
    _trigger_row(session, "guardian-ci-failure", "build.failed")
    rm = FakeRM()
    from app.db.models.user import User
    session.add(User(username="system", display_name="sys", status="active", pin_hash="!"))
    session.commit()
    for build in (1, 2):
        out = await triggers.process(_build_event(build=build, tasks=(f"t{build}",)), rm)
        assert out["verdicts"][0]["status"] == "started"
    out = await triggers.process(_build_event(build=3, tasks=("t3",)), rm)
    assert out["verdicts"][0]["status"] == "halted"
    assert out["verdicts"][0]["reason"] == "max_attempts"
    assert len(rm.created) == 2


async def test_guardian_halts_on_repeated_signature(session, monkeypatch):
    _trigger_row(session, "guardian-ci-failure", "build.failed")
    from app.db.models.user import User
    session.add(User(username="system", display_name="sys", status="active", pin_hash="!"))
    session.commit()
    rm = FakeRM()
    first = await triggers.process(_build_event(build=1, tasks=("npm test",)), rm)
    assert first["verdicts"][0]["status"] == "started"
    second = await triggers.process(_build_event(build=2, tasks=("npm test",)), rm)
    assert second["verdicts"][0]["status"] == "halted"
    assert second["verdicts"][0]["reason"] == "repeated_signature"
    assert len(rm.created) == 1


async def test_guardian_run_is_gated_and_system_owned(session):
    _trigger_row(session, "guardian-ci-failure", "build.failed")
    from app.db.models.user import User
    sys_user = User(username="system", display_name="sys", status="active", pin_hash="!")
    session.add(sys_user)
    session.commit()
    rm = FakeRM()
    out = await triggers.process(_build_event(), rm)
    assert out["verdicts"][0]["status"] == "started"
    assert rm.created[0]["autonomy"] == "gated"
    assert rm.created[0]["by"] == sys_user.id
    assert "npm test" in rm.created[0]["task"]


# ------------------------------------------------------------------- responder
async def test_responder_ignores_unknown_pr(session):
    _trigger_row(session, "responder-pr-comment", "pr.comment")
    out = await triggers.process(_comment_event(pr_id=999), FakeRM())
    assert out["verdicts"][0]["status"] == "ignored"
    assert out["verdicts"][0]["reason"] == "unknown_pr"


async def test_responder_nudges_active_thread(session, make_user):
    _trigger_row(session, "responder-pr-comment", "pr.comment")
    u = make_user()
    session.add(Run(id="r1", created_by=u.id, mode="development", stage="developing"))
    session.add(Thread(id="thread-1", run_id="r1", persona="developer", status="running"))
    session.add(PrLink(run_id="r1", repo="ServerApp", branch="agent/r1-x", ado_pr_id=77))
    session.commit()
    rm = FakeRM()
    out = await triggers.process(_comment_event(), rm)
    assert out["verdicts"][0]["status"] == "nudged"
    assert rm.nudges[0]["thread_id"] == "thread-1"
    assert "please add a test" in rm.nudges[0]["text"]


async def test_responder_resumes_finished_thread_from_session_volume(session, make_user):
    _trigger_row(session, "responder-pr-comment", "pr.comment")
    u = make_user()
    session.add(Run(id="r1", created_by=u.id, mode="development", stage="pr_ready"))
    session.add(Thread(id="thread-old", run_id="r1", persona="developer", status="completed"))
    session.add(Repo(name="ServerApp", integration_branch="main", status="ready"))
    session.add(PrLink(run_id="r1", repo="ServerApp", branch="agent/r1-x", ado_pr_id=77))
    session.commit()
    rm = FakeRM()
    out = await triggers.process(_comment_event(), rm)
    assert out["verdicts"][0]["status"] == "resumed"
    spawn = rm.thread_manager.spawns[0]
    assert spawn["persona"] == "responder"
    assert spawn["resume_session"] is True
    assert spawn["writable_repo"].name == "ServerApp"


async def test_responder_ignores_terminal_run(session, make_user):
    _trigger_row(session, "responder-pr-comment", "pr.comment")
    u = make_user()
    session.add(Run(id="r1", created_by=u.id, mode="development", stage="completed"))
    session.add(PrLink(run_id="r1", repo="ServerApp", branch="agent/r1-x", ado_pr_id=77))
    session.commit()
    out = await triggers.process(_comment_event(), FakeRM())
    assert out["verdicts"][0]["status"] == "ignored"
    assert out["verdicts"][0]["reason"] == "run_terminal"


async def test_responder_skips_owner_resolution(session, make_user):
    """The comment author needs NO Collegium identity — a reviewer with an unbound
    descriptor still gets answered (attribution settled at run creation)."""
    _trigger_row(session, "responder-pr-comment", "pr.comment")
    u = make_user()
    session.add(Run(id="r1", created_by=u.id, mode="development", stage="developing"))
    session.add(Thread(id="thread-1", run_id="r1", persona="developer", status="running"))
    session.add(PrLink(run_id="r1", repo="ServerApp", branch="agent/r1-x", ado_pr_id=77))
    session.commit()
    out = await triggers.process(
        _comment_event(), FakeRM())  # descriptor 'desc-reviewer' is unbound
    assert out["verdicts"][0]["status"] == "nudged"
