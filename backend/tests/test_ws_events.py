from unittest.mock import MagicMock

from app.core.security import issue_token
from app.db.models.user import User


class _FakeWs:
    def __init__(self, cookies=None):
        self.cookies = cookies or {}


def test_authenticate_no_token(session, make_user):
    from app.ws.events import _authenticate
    ws = _FakeWs(cookies={})
    assert _authenticate(ws) is None


def test_authenticate_invalid_token(session, make_user):
    from app.ws.events import _authenticate
    ws = _FakeWs(cookies={"zagent_token": "not.a.jwt"})
    assert _authenticate(ws) is None


def test_authenticate_user_not_found(session, make_user):
    from app.ws.events import _authenticate
    u = make_user("ghost", role="member", status="active", pin="1234")
    token = issue_token(u)
    session.delete(u); session.commit()
    ws = _FakeWs(cookies={"zagent_token": token})
    assert _authenticate(ws) is None


def test_authenticate_inactive(session, make_user):
    from app.ws.events import _authenticate
    u = make_user("dormant", role="member", status="pending", pin="1234")
    token = issue_token(u)
    ws = _FakeWs(cookies={"zagent_token": token})
    assert _authenticate(ws) is None


def test_authenticate_revoked(session, make_user):
    from app.ws.events import _authenticate
    u = make_user("rev", role="member", status="active", pin="1234")
    token = issue_token(u)
    u.token_version += 1
    session.commit()
    ws = _FakeWs(cookies={"zagent_token": token})
    assert _authenticate(ws) is None


def test_authenticate_success(session, make_user):
    from app.ws.events import _authenticate
    u = make_user("alice", role="member", status="active", pin="1234")
    token = issue_token(u)
    ws = _FakeWs(cookies={"zagent_token": token})
    user = _authenticate(ws)
    assert user is not None
    assert user.username == "alice"


def test_authenticate_decode_exception_returns_none(monkeypatch):
    from app.ws import events
    from app.core import security

    def boom(token):
        raise RuntimeError("decode blew up")
    monkeypatch.setattr(security, "decode_token", boom)
    ws = _FakeWs(cookies={"zagent_token": "anything"})
    assert events._authenticate(ws) is None


# --------------------------------------------------------------- run_events_ws loop
class _FakeApp:
    def __init__(self, relay):
        self.state = type("s", (), {"relay": relay})()


class _FakeWebSocket:
    def __init__(self, cookies, relay, run_id):
        self.cookies = cookies
        self.app = _FakeApp(relay)
        self.run_id = run_id
        self.accepted = False
        self.closed_with = None
        self.sent: list[str] = []
        self._disconnect_after = None

    async def accept(self):
        self.accepted = True

    async def close(self, code=1000):
        self.closed_with = code

    async def send_text(self, text):
        if self._disconnect_after is not None and len(self.sent) >= self._disconnect_after:
            from fastapi import WebSocketDisconnect
            raise WebSocketDisconnect()
        self.sent.append(text)


async def test_ws_close_unauthenticated(session, make_user):
    from app.ws.events import run_events_ws
    ws = _FakeWebSocket(cookies={}, relay=None, run_id="r1")
    await run_events_ws(ws, "r1")
    assert ws.closed_with == 4401
    assert not ws.accepted


async def test_ws_close_run_not_found(session, make_user):
    from app.ws.events import run_events_ws
    from tests.conftest import FakeRelay
    u = make_user("alice", role="member", status="active", pin="1234")
    token = issue_token(u)
    relay = FakeRelay()
    ws = _FakeWebSocket(cookies={"zagent_token": token}, relay=relay, run_id="ghost")
    await run_events_ws(ws, "ghost")
    assert ws.closed_with == 4404
    assert not ws.accepted


async def test_ws_close_run_owned_by_other(session, make_user):
    from app.ws.events import run_events_ws
    from tests.conftest import FakeRelay
    from app.db.models.run import Run
    u = make_user("alice", role="member", status="active", pin="1234")
    other = make_user("bob", role="member", status="active", pin="1234")
    session.add(Run(id="r1", created_by=other.id, mode="ask", stage="completed"))
    session.commit()
    token = issue_token(u)
    relay = FakeRelay()
    ws = _FakeWebSocket(cookies={"zagent_token": token}, relay=relay, run_id="r1")
    await run_events_ws(ws, "r1")
    assert ws.closed_with == 4404


async def test_ws_accepts_and_forwards_messages(session, make_user):
    import asyncio
    from app.ws.events import run_events_ws
    from tests.conftest import FakeRelay
    from app.db.models.run import Run
    u = make_user("alice", role="member", status="active", pin="1234")
    session.add(Run(id="r1", created_by=u.id, mode="ask", stage="completed"))
    session.commit()
    token = issue_token(u)
    relay = FakeRelay()
    ws = _FakeWebSocket(cookies={"zagent_token": token}, relay=relay, run_id="r1")
    ws._disconnect_after = 1  # raise WebSocketDisconnect after 1st message

    task = asyncio.create_task(run_events_ws(ws, "r1"))
    # Wait for the endpoint to subscribe, then push a message into its queue.
    for _ in range(50):
        await asyncio.sleep(0)
        if "r1" in relay.subscribers and relay.subscribers["r1"]:
            break
    queue = next(iter(relay.subscribers["r1"]))
    await queue.put({"type": "step", "event": "hello"})
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert ws.accepted
    assert len(ws.sent) == 1
    import json
    assert json.loads(ws.sent[0])["type"] == "step"
    assert "r1" not in relay.subscribers or not relay.subscribers.get("r1")
