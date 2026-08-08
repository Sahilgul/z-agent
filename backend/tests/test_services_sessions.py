from datetime import UTC, datetime, timedelta

from app.db.models.event import Event
from app.db.models.run import Run
from app.db.models.thread import Thread
from app.services import sessions


def _make_run(session, make_user, thread_id="thread-1"):
    u = make_user("su")
    run = Run(id="run-s", created_by=u.id, mode="ask", stage="completed")
    session.add(run)
    session.commit()
    thread = Thread(id=thread_id, run_id=run.id, persona="researcher", status="completed")
    session.add(thread)
    session.commit()
    return run, thread, u


def test_replay_events_orders_by_thread_seq(session, make_user):
    run, thread, u = _make_run(session, make_user)
    for seq, kind in [(2, "message"), (1, "thinking"), (3, "command")]:
        session.add(Event(run_id=run.id, thread_id=thread.id, seq=seq, type=kind,
                          title=f"t{seq}", payload={"k": "v"}))
    session.commit()
    out = sessions.replay_events(run.id, u.id)
    assert [e["seq"] for e in out] == [1, 2, 3]
    assert out[0]["kind"] == "thinking"
    assert out[0]["detail"] == {"k": "v"}


def test_replay_events_merged_interleaves_thread_generations_by_time(session, make_user):
    """Reloaded chats must not cluster by thread. Thread generations (mode
    switch, mention expansion, kill-replace) are separate Thread rows with
    independent seq counters, and (thread_id, seq) ordering rendered each
    generation as one UUID-sorted block — messages sent earlier appeared after
    later ones. Reproduces the exact reported scramble: t3's messages were sent
    BETWEEN t1's and t2's, but UUID order (t1 < t2 < t3) used to put them last."""
    u = make_user("chrono")
    run = Run(id="run-chrono", created_by=u.id, mode="ask", stage="completed")
    session.add(run)
    t1 = Thread(id="aaaa-1", run_id=run.id, persona="lead", status="replaced")
    t2 = Thread(id="bbbb-2", run_id=run.id, persona="lead", status="replaced")
    t3 = Thread(id="cccc-3", run_id=run.id, persona="lead", status="completed")
    session.add_all([t1, t2, t3])
    base = datetime(2026, 8, 6, 22, 53, 7, tzinfo=UTC)

    def ev(thread_id, seq, at_seconds, title):
        session.add(Event(run_id=run.id, thread_id=thread_id, seq=seq, type="message",
                          title=title, ts=base + timedelta(seconds=at_seconds)))

    ev("aaaa-1", 0, 0, "test message 1")
    ev("aaaa-1", 1, 8, "reply 1")
    ev("cccc-3", 0, 17, "test message 2")
    ev("cccc-3", 1, 25, "test message 3")
    ev("bbbb-2", 0, 32, "test message 4")
    ev("bbbb-2", 1, 38, "reply 4")
    session.commit()

    out = sessions.replay_events(run.id, u.id)
    assert [e["title"] for e in out] == [
        "test message 1", "reply 1", "test message 2",
        "test message 3", "test message 4", "reply 4",
    ]
    # A lane-filtered view keeps strict per-thread seq order (unchanged).
    lane = sessions.replay_events(run.id, u.id, thread_id="cccc-3")
    assert [e["title"] for e in lane] == ["test message 2", "test message 3"]
    assert "ts" in out[0] and out[0]["sdk_message_uuid"] is None


def test_replay_events_thread_filter(session, make_user):
    run, thread, u = _make_run(session, make_user, thread_id="thread-1")
    thread2 = Thread(id="thread-2", run_id=run.id, persona="researcher", status="completed")
    session.add(thread2)
    session.commit()
    session.add(Event(run_id=run.id, thread_id="thread-1", seq=1, type="message", title="a"))
    session.add(Event(run_id=run.id, thread_id="thread-2", seq=1, type="message", title="b"))
    session.commit()
    out = sessions.replay_events(run.id, u.id, thread_id="thread-1")
    assert len(out) == 1
    assert out[0]["thread_id"] == "thread-1"


def test_replay_events_after_seq(session, make_user):
    run, thread, u = _make_run(session, make_user)
    for seq in [1, 2, 3, 4]:
        session.add(Event(run_id=run.id, thread_id=thread.id, seq=seq, type="message", title=str(seq)))
    session.commit()
    out = sessions.replay_events(run.id, u.id, after_seq=2)
    assert [e["seq"] for e in out] == [3, 4]


def test_replay_events_scoped_to_owner(session, make_user):
    run, thread, u = _make_run(session, make_user)
    other = make_user("other")
    session.add(Event(run_id=run.id, thread_id=thread.id, seq=1, type="message", title="x"))
    session.commit()
    assert sessions.replay_events(run.id, other.id) == []
    assert sessions.replay_events("missing", u.id) == []


def test_replay_events_limit(session, make_user):
    run, thread, u = _make_run(session, make_user)
    for seq in range(5):
        session.add(Event(run_id=run.id, thread_id=thread.id, seq=seq, type="message", title=str(seq)))
    session.commit()
    out = sessions.replay_events(run.id, u.id, limit=3)
    assert len(out) == 3


def test_session_volume_exists_false_when_missing(tmp_path, monkeypatch):
    from app.core.config import get_settings
    monkeypatch.setattr(get_settings(), "sessions_dir", tmp_path)
    assert sessions.session_volume_exists("nope", "nope") is False


def test_session_volume_exists_true_when_populated(tmp_path, monkeypatch):
    from app.core.config import get_settings
    monkeypatch.setattr(get_settings(), "sessions_dir", tmp_path)
    sub = tmp_path / "run-x" / "thread-y"
    sub.mkdir(parents=True)
    (sub / "f.txt").write_text("x")
    assert sessions.session_volume_exists("run-x", "thread-y") is True


def test_fork_point_before_last_user_message(session, make_user):
    run, thread, u = _make_run(session, make_user)
    session.add(Event(run_id=run.id, thread_id=thread.id, seq=1, type="message",
                      title="m1", sdk_message_uuid="uuid-1"))
    session.add(Event(run_id=run.id, thread_id=thread.id, seq=2, type="command", title="c"))
    session.add(Event(run_id=run.id, thread_id=thread.id, seq=3, type="message",
                      title="m2", sdk_message_uuid="uuid-2"))
    session.commit()
    assert sessions.fork_point_before_last_user_message(run.id, thread.id) == "uuid-1"


def test_fork_point_returns_none_when_one_message(session, make_user):
    run, thread, u = _make_run(session, make_user)
    session.add(Event(run_id=run.id, thread_id=thread.id, seq=1, type="message",
                      title="m1", sdk_message_uuid="uuid-1"))
    session.commit()
    assert sessions.fork_point_before_last_user_message(run.id, thread.id) is None


def test_fork_point_skips_null_uuid_rows(session, make_user):
    run, thread, u = _make_run(session, make_user)
    session.add(Event(run_id=run.id, thread_id=thread.id, seq=1, type="message",
                      title="m1", sdk_message_uuid=None))
    session.add(Event(run_id=run.id, thread_id=thread.id, seq=2, type="message",
                      title="m2", sdk_message_uuid="uuid-2"))
    session.commit()
    # the event before the last message has a null uuid -> returned as-is (None)
    assert sessions.fork_point_before_last_user_message(run.id, thread.id) is None
