import uuid
from datetime import datetime, timezone

from app.db.models.event import Event
from app.db.models.lane import Lane
from app.db.models.run import Run
from app.services import sessions


def _make_run(session, make_user, lane_id="lane-1"):
    u = make_user("su")
    run = Run(id="run-s", created_by=u.id, mode="ask", stage="completed")
    session.add(run)
    session.commit()
    lane = Lane(id=lane_id, run_id=run.id, persona="researcher", status="completed")
    session.add(lane)
    session.commit()
    return run, lane, u


def test_replay_events_orders_by_lane_seq(session, make_user):
    run, lane, u = _make_run(session, make_user)
    for seq, kind in [(2, "message"), (1, "thinking"), (3, "command")]:
        session.add(Event(run_id=run.id, lane_id=lane.id, seq=seq, type=kind,
                          title=f"t{seq}", payload={"k": "v"}))
    session.commit()
    out = sessions.replay_events(run.id, u.id)
    assert [e["seq"] for e in out] == [1, 2, 3]
    assert out[0]["kind"] == "thinking"
    assert out[0]["detail"] == {"k": "v"}
    assert "ts" in out[0] and out[0]["sdk_message_uuid"] is None


def test_replay_events_lane_filter(session, make_user):
    run, lane, u = _make_run(session, make_user, lane_id="lane-1")
    lane2 = Lane(id="lane-2", run_id=run.id, persona="researcher", status="completed")
    session.add(lane2)
    session.commit()
    session.add(Event(run_id=run.id, lane_id="lane-1", seq=1, type="message", title="a"))
    session.add(Event(run_id=run.id, lane_id="lane-2", seq=1, type="message", title="b"))
    session.commit()
    out = sessions.replay_events(run.id, u.id, lane_id="lane-1")
    assert len(out) == 1
    assert out[0]["lane_id"] == "lane-1"


def test_replay_events_after_seq(session, make_user):
    run, lane, u = _make_run(session, make_user)
    for seq in [1, 2, 3, 4]:
        session.add(Event(run_id=run.id, lane_id=lane.id, seq=seq, type="message", title=str(seq)))
    session.commit()
    out = sessions.replay_events(run.id, u.id, after_seq=2)
    assert [e["seq"] for e in out] == [3, 4]


def test_replay_events_scoped_to_owner(session, make_user):
    run, lane, u = _make_run(session, make_user)
    other = make_user("other")
    session.add(Event(run_id=run.id, lane_id=lane.id, seq=1, type="message", title="x"))
    session.commit()
    assert sessions.replay_events(run.id, other.id) == []
    assert sessions.replay_events("missing", u.id) == []


def test_replay_events_limit(session, make_user):
    run, lane, u = _make_run(session, make_user)
    for seq in range(5):
        session.add(Event(run_id=run.id, lane_id=lane.id, seq=seq, type="message", title=str(seq)))
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
    sub = tmp_path / "run-x" / "lane-y"
    sub.mkdir(parents=True)
    (sub / "f.txt").write_text("x")
    assert sessions.session_volume_exists("run-x", "lane-y") is True


def test_fork_point_before_last_user_message(session, make_user):
    run, lane, u = _make_run(session, make_user)
    session.add(Event(run_id=run.id, lane_id=lane.id, seq=1, type="message",
                      title="m1", sdk_message_uuid="uuid-1"))
    session.add(Event(run_id=run.id, lane_id=lane.id, seq=2, type="command", title="c"))
    session.add(Event(run_id=run.id, lane_id=lane.id, seq=3, type="message",
                      title="m2", sdk_message_uuid="uuid-2"))
    session.commit()
    assert sessions.fork_point_before_last_user_message(run.id, lane.id) == "uuid-1"


def test_fork_point_returns_none_when_one_message(session, make_user):
    run, lane, u = _make_run(session, make_user)
    session.add(Event(run_id=run.id, lane_id=lane.id, seq=1, type="message",
                      title="m1", sdk_message_uuid="uuid-1"))
    session.commit()
    assert sessions.fork_point_before_last_user_message(run.id, lane.id) is None


def test_fork_point_skips_null_uuid_rows(session, make_user):
    run, lane, u = _make_run(session, make_user)
    session.add(Event(run_id=run.id, lane_id=lane.id, seq=1, type="message",
                      title="m1", sdk_message_uuid=None))
    session.add(Event(run_id=run.id, lane_id=lane.id, seq=2, type="message",
                      title="m2", sdk_message_uuid="uuid-2"))
    session.commit()
    # the event before the last message has a null uuid -> returned as-is (None)
    assert sessions.fork_point_before_last_user_message(run.id, lane.id) is None
