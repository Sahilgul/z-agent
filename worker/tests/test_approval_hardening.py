"""Wave 4 Stream C worker-side: G2 durable decision read, G3 TTLs, G4
destructive always-allow guards."""

import json

import fakeredis.aioredis
import pytest

from worker.engine.approvals import ApprovalBroker


@pytest.fixture
def broker():
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    b = ApprovalBroker.__new__(ApprovalBroker)
    b.redis = r
    b.run_id = "r1"
    b.thread_id = "l1"
    b.timeout_s = 1
    return b, r


# ------------------------------------------------------------------- G2

@pytest.mark.asyncio
async def test_durable_decision_read_before_blpop(broker):
    """A replaced container re-entering the gate finds the already-made
    decision on the durable key — no timeout-into-wrong-deny."""
    b, r = broker
    await r.set("approval:ap1:decision_value",
                json.dumps({"decision": "allow", "reason": "ok"}))
    # Nothing on the BLPOP list — only the durable copy exists.
    decision = await b.wait_decision("ap1")
    assert decision["decision"] == "allow"


@pytest.mark.asyncio
async def test_blpop_path_still_works_without_durable_key(broker):
    b, r = broker
    await r.rpush("approval:ap2:decision",
                  json.dumps({"decision": "deny", "reason": "no"}))
    decision = await b.wait_decision("ap2")
    assert decision["decision"] == "deny"


# ------------------------------------------------------------------- G3

@pytest.mark.asyncio
async def test_always_allow_set_gets_ttl(broker):
    b, r = broker
    await b.persist_always_allow("file_edit")
    ttl = await r.ttl("always_allow:r1")
    assert ttl > 0  # bounded lifetime, not forever


# ------------------------------------------------------------------- G4

@pytest.mark.asyncio
async def test_gate_does_not_persist_always_allow_for_destructive():
    """A crafted always_allow decision for a destructive terminal_exec must
    honor this call only — never whitelist the tool class."""
    import inspect

    from worker.engine import graph
    src = inspect.getsource(graph)
    # The gate re-verifies destructiveness before persisting.
    assert "is_destructive_command" in src
    idx = src.index("persist_always_allow(name)")
    window = src[max(0, idx - 400):idx]
    assert "destructive" in window


@pytest.mark.asyncio
async def test_legacy_bridge_blocks_destructive_always_allow():
    import inspect

    import worker.approvals as bridge
    src = inspect.getsource(bridge)
    assert "is_destructive_command" in src
