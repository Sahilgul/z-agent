import json

from app.events import control as control_mod


def _control(fake_redis):
    c = control_mod.LaneControl.__new__(control_mod.LaneControl)
    c.redis = fake_redis
    return c


async def test_interrupt_publishes_correct_channel(fake_redis):
    c = _control(fake_redis)
    await c.interrupt("thread-1")
    channel, payload = fake_redis.published[-1]
    assert channel == "thread:thread-1:control"
    data = json.loads(payload)
    assert data["type"] == "interrupt"
    assert data["id"]  # K14: critical controls carry an ack id


async def test_nudge_publishes_text(fake_redis):
    c = _control(fake_redis)
    await c.nudge("thread-1", "go faster")
    channel, payload = fake_redis.published[-1]
    assert channel == "thread:thread-1:control"
    data = json.loads(payload)
    assert data["type"] == "nudge" and data["text"] == "go faster"


async def test_set_mode_publishes_mode(fake_redis):
    c = _control(fake_redis)
    await c.set_mode("thread-1", "acceptEdits")
    channel, payload = fake_redis.published[-1]
    assert channel == "thread:thread-1:control"
    data = json.loads(payload)
    assert data["type"] == "mode" and data["mode"] == "acceptEdits"


async def test_kill_publishes_kill(fake_redis):
    c = _control(fake_redis)
    await c.kill("thread-1")
    channel, payload = fake_redis.published[-1]
    assert channel == "thread:thread-1:control"
    data = json.loads(payload)
    assert data["type"] == "kill" and data["id"]


async def test_resolve_approval_rpushes_decision(fake_redis):
    c = _control(fake_redis)
    await c.resolve_approval("ap-1", "approved", "ok")
    assert fake_redis.lists["approval:ap-1:decision"] == [json.dumps({"decision": "approved", "reason": "ok"})]


async def test_resolve_approval_default_reason(fake_redis):
    c = _control(fake_redis)
    await c.resolve_approval("ap-1", "denied")
    assert json.loads(fake_redis.lists["approval:ap-1:decision"][0])["reason"] == ""


async def test_close(fake_redis):
    c = _control(fake_redis)
    await c.close()


def test_channel_format():
    c = _control(None)
    assert c._channel("l1") == "thread:l1:control"
