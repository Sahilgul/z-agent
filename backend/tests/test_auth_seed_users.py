from app.auth import seed_users
from app.db.models.event import Event
from app.db.models.idea import IdeaComment, IdeaThread
from app.db.models.mode import Mode
from app.db.models.run import Run
from app.db.models.thread import Thread
from app.db.models.user import User


def test_seed_creates_system_user_and_ask_mode(session):
    seed_users.seed()
    system = session.query(User).filter_by(username="system").one()
    assert system.role == "member"
    assert system.status == "active"
    # M-56: the system user is a service account that must NEVER log in.
    # pin_hash is None (was "!locked" — an invalid bcrypt hash that crashed
    # the login path with a 500). None makes the login route's
    # `pin_hash is None` guard return a clean 401.
    assert system.pin_hash is None
    mode = session.query(Mode).filter_by(name="ask").one()
    assert mode.permission_mode == "default"
    assert mode.autonomy_default == "supervised"
    assert mode.enabled is True


def test_seed_creates_plan_development_debug_modes(session):
    seed_users.seed()
    plan = session.query(Mode).filter_by(name="plan").one()
    assert plan.topology == "plan"
    assert plan.permissions == {"writable": False, "repos": []}
    assert plan.playbook_ids == ["plan/fleet-scoping"]
    dev = session.query(Mode).filter_by(name="development").one()
    assert dev.topology == "development"
    assert dev.permission_mode == "acceptEdits"
    assert dev.autonomy_default == "gated"
    assert dev.permissions["writable"] is True
    assert "development/serverapp-areas" in dev.playbook_ids
    debug = session.query(Mode).filter_by(name="debug").one()
    assert debug.topology == "debug"
    assert debug.permissions == {"writable": False, "repos": []}
    assert debug.playbook_ids == ["debug/repro-first"]


def test_seed_creates_goal_mode(session):
    """Goal mode = zero-interruption PRD->PR. autonomy_default='autonomous' is
    load-bearing: it maps to bypassPermissions so the engine's approval gate
    never fires — the mode is defined by running with no approval cards."""
    seed_users.seed()
    goal = session.query(Mode).filter_by(name="goal").one()
    assert goal.topology == "goal"
    assert goal.autonomy_default == "autonomous"
    assert goal.permission_mode == "bypassPermissions"
    assert goal.permissions == {"writable": True, "repos": []}
    assert goal.evidence_contract["tests_pass"] is True
    assert goal.enabled is True


def test_seed_creates_playbooks(session):
    from app.db.models.knowledge import Playbook
    seed_users.seed()
    names = {p.name for p in session.query(Playbook).all()}
    assert "plan/fleet-scoping" in names
    assert "development/serverapp-areas" in names
    assert "development/drizzle-transactions" in names
    assert "debug/repro-first" in names


def test_seed_is_idempotent(session):
    from app.db.models.knowledge import Playbook
    seed_users.seed()
    # L-32: idempotency was asserted for only a subset of seeded entities
    # (User/system, Mode/ask, Run/DEMO, IdeaThread/Welcome). The seed also
    # creates four more Modes, the DEMO Thread + Events, IdeaComments, and
    # Playbooks — a non-idempotent seed() would double any of them. Capture
    # counts after the first seed, run a second seed(), and assert every
    # count is unchanged.
    demo_run = session.query(Run).filter_by(
        title="DEMO: How does scribe dedupe questions?").one()
    welcome = session.query(IdeaThread).filter_by(
        title="Welcome to Collegium — what should the fleet learn first?").one()
    counts = {
        "system_user": session.query(User).filter_by(username="system").count(),
        "modes": {n: session.query(Mode).filter_by(name=n).count()
                  for n in ("ask", "plan", "development", "debug", "agent-rnd")},
        "demo_run": session.query(Run).filter_by(
            title="DEMO: How does scribe dedupe questions?").count(),
        "demo_thread": session.query(Thread).filter_by(run_id=demo_run.id).count(),
        "demo_events": session.query(Event).filter_by(run_id=demo_run.id).count(),
        "welcome": session.query(IdeaThread).filter_by(
            title="Welcome to Collegium — what should the fleet learn first?").count(),
        "welcome_comments": session.query(IdeaComment).filter_by(thread_id=welcome.id).count(),
        "playbooks": session.query(Playbook).count(),
    }
    seed_users.seed()
    demo_run2 = session.query(Run).filter_by(
        title="DEMO: How does scribe dedupe questions?").one()
    welcome2 = session.query(IdeaThread).filter_by(
        title="Welcome to Collegium — what should the fleet learn first?").one()
    assert session.query(User).filter_by(username="system").count() == counts["system_user"]
    for mode_name in ("ask", "plan", "development", "debug", "agent-rnd"):
        assert session.query(Mode).filter_by(name=mode_name).count() == counts["modes"][mode_name]
    assert session.query(Run).filter_by(
        title="DEMO: How does scribe dedupe questions?").count() == counts["demo_run"]
    assert session.query(Thread).filter_by(run_id=demo_run2.id).count() == counts["demo_thread"]
    assert session.query(Event).filter_by(run_id=demo_run2.id).count() == counts["demo_events"]
    assert session.query(IdeaThread).filter_by(
        title="Welcome to Collegium — what should the fleet learn first?").count() == counts["welcome"]
    assert session.query(IdeaComment).filter_by(thread_id=welcome2.id).count() == counts["welcome_comments"]
    assert session.query(Playbook).count() == counts["playbooks"]


def test_seed_creates_demo_run_with_events_and_thread(session):
    seed_users.seed()
    run = session.query(Run).filter_by(title="DEMO: How does scribe dedupe questions?").one()
    assert run.stage == "completed"
    assert run.cost_usd == 0.0
    assert run.repo == "ServerApp"
    thread = session.query(Thread).filter_by(run_id=run.id).one()
    assert thread.status == "completed"
    assert thread.next_seq == len(seed_users.DEMO_EVENTS)
    events = session.query(Event).filter_by(run_id=run.id).order_by(Event.seq).all()
    assert len(events) == len(seed_users.DEMO_EVENTS)
    assert events[0].type == seed_users.DEMO_EVENTS[0][0].value


def test_seed_creates_welcome_thread_with_comments(session):
    seed_users.seed()
    thread = session.query(IdeaThread).filter_by(
        title="Welcome to Collegium — what should the fleet learn first?").one()
    comments = session.query(IdeaComment).filter_by(thread_id=thread.id).all()
    assert len(comments) == 4
    assert all(c.body for c in comments)


def test_seed_demo_run_skipped_if_title_exists(session):
    seed_users.seed()
    seed_users.seed()
    runs = session.query(Run).filter_by(title="DEMO: How does scribe dedupe questions?").all()
    assert len(runs) == 1


def test_seed_welcome_thread_skipped_if_title_exists(session):
    seed_users.seed()
    seed_users.seed()
    threads = session.query(IdeaThread).filter_by(
        title="Welcome to Collegium — what should the fleet learn first?").all()
    assert len(threads) == 1
