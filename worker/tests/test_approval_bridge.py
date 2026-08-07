"""ApprovalBridge always-allow persistence contract.

Regression: "always allow" used to live only in the bridge's in-memory set,
so a container restart / new thread in the same run forgot it and the
permission card re-appeared. The set must persist to Redis
(always_allow:{run_id}) and a fresh bridge instance must honor it.
"""

from __future__ import annotations

import json

import pytest

from worker.approvals import ApprovalBridge

pytestmark = pytest.mark.asyncio


class _Ctx:
    """Stands in for the SDK's ToolPermissionContext (assert isinstance is
    monkeypatched away below — the SDK isn't installed in CI)."""


@pytest.fixture
def fake_redis():
    pytest.importorskip("fakeredis.aioredis")
    import fakeredis.aioredis as fake_mod

    return fake_mod.FakeRedis(decode_responses=True)


@pytest.fixture
def patch_context(monkeypatch):
    """The SDK import inside ask() isn't available in CI; stub it."""
    import sys
    import types

    mod = types.ModuleType("claude_agent_sdk")

    class PermissionResultAllow:
        def __init__(self, updated_input=None):
            self.updated_input = updated_input

    class PermissionResultDeny:
        def __init__(self, message=""):
            self.message = message

    class ToolPermissionContext:
        pass

    mod.PermissionResultAllow = PermissionResultAllow
    mod.PermissionResultDeny = PermissionResultDeny
    mod.ToolPermissionContext = ToolPermissionContext
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", mod)
    return ToolPermissionContext()


async def _decide_watcher(redis, approval_box, decision):
    """Pretend to be the backend: wait for the request, then push a decision."""
    import asyncio

    for _ in range(100):
        entries = await redis.xrange("approvals:run-1")
        if entries:
            approval_id = entries[0][1]["approval_id"]
            approval_box.append(approval_id)
            await redis.rpush(
                f"approval:{approval_id}:decision",
                json.dumps({"decision": decision, "reason": ""}),
            )
            return
        await asyncio.sleep(0.01)
    raise AssertionError("approval request never published")


async def test_always_allow_survives_bridge_restart(fake_redis, patch_context):
    import asyncio

    bridge = ApprovalBridge("redis://fake", "run-1", "t1")
    bridge.redis = fake_redis

    box: list[str] = []
    watcher = asyncio.create_task(_decide_watcher(fake_redis, box, "always_allow"))
    result = await bridge.ask("Bash", {"command": "ls"}, patch_context)
    await watcher
    assert type(result).__name__ == "PermissionResultAllow"
    assert await fake_redis.smembers("always_allow:run-1") == {"Bash"}

    # Simulate container replacement: a brand-new bridge, same run.
    bridge2 = ApprovalBridge("redis://fake", "run-1", "t1")
    bridge2.redis = fake_redis
    assert bridge2.always_allowed == set()  # in-memory is empty

    # No new approval request may be published — the card must NOT reappear.
    await fake_redis.delete("approvals:run-1")
    result2 = await bridge2.ask("Bash", {"command": "pwd"}, patch_context)
    assert type(result2).__name__ == "PermissionResultAllow"
    assert await fake_redis.xrange("approvals:run-1") == []


async def test_always_allow_shared_across_threads(fake_redis, patch_context):
    # Another thread of the SAME run honors the run-scoped always-allow.
    await fake_redis.sadd("always_allow:run-1", "Edit")
    bridge = ApprovalBridge("redis://fake", "run-1", "t2")
    bridge.redis = fake_redis
    result = await bridge.ask("Edit", {"file_path": "a.py"}, patch_context)
    assert type(result).__name__ == "PermissionResultAllow"
    assert await fake_redis.xrange("approvals:run-1") == []


async def test_allow_once_does_not_persist(fake_redis, patch_context):
    import asyncio

    bridge = ApprovalBridge("redis://fake", "run-1", "t1")
    bridge.redis = fake_redis

    box: list[str] = []
    watcher = asyncio.create_task(_decide_watcher(fake_redis, box, "allow_once"))
    result = await bridge.ask("Bash", {"command": "ls"}, patch_context)
    await watcher
    assert type(result).__name__ == "PermissionResultAllow"
    assert await fake_redis.smembers("always_allow:run-1") == set()
    assert bridge.always_allowed == set()
