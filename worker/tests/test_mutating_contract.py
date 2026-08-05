"""Contract tests for mutating tools + approval gate.

Validates the MULTI-ACTOR CONTRACTS:
  read-before-edit (content-hash guard) — edit/write refused on mismatch.
  two-phase verbatim approval — always_allow persists the class only;
     destructive never gets always_allow; timeout = deny.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from worker.engine.approvals import ApprovalGate
from worker.engine.tools import (
    ALL_TOOLS,
    call_any_tool,
    call_mutating_tool,
    capability_of,
    content_hash,
    is_destructive_command,
    needs_approval,
)

# ----------------------------------------------------------- content hash (read-before-edit)

def test_content_hash_is_stable_and_truncated():
    h1 = content_hash("hello world")
    h2 = content_hash("hello world")
    h3 = content_hash("hello world!")
    assert h1 == h2
    assert h1 != h3
    assert len(h1) == 16


@pytest.mark.asyncio
async def test_file_edit_with_matching_hash_succeeds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path))
    f = tmp_path / "x.txt"
    f.write_text("foo\nbar\n")
    h = content_hash("foo\nbar\n")
    r = await call_mutating_tool("file_edit", {
        "file_path": str(f), "old_string": "foo", "new_string": "FOO", "expected_hash": h,
    })
    assert r["ok"] is True
    assert "FOO" in f.read_text()


@pytest.mark.asyncio
async def test_file_edit_refuses_on_hash_mismatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The file changed since read -> refuse, return current content."""
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path))
    f = tmp_path / "x.txt"
    f.write_text("foo\nbar\n")
    r = await call_mutating_tool("file_edit", {
        "file_path": str(f), "old_string": "foo", "new_string": "FOO",
        "expected_hash": "wronghash00000000",
    })
    assert r["ok"] is False
    assert "hash mismatch" in r["output"]
    # The current content is returned so the agent can re-read
    assert "foo" in r["output"]


@pytest.mark.asyncio
async def test_file_edit_without_hash_proceeds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """No hash guard = no read-before-edit check (legacy/ask-mode path)."""
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path))
    f = tmp_path / "x.txt"
    f.write_text("foo\n")
    r = await call_mutating_tool("file_edit", {
        "file_path": str(f), "old_string": "foo", "new_string": "FOO",
    })
    assert r["ok"] is True


@pytest.mark.asyncio
async def test_file_edit_ambiguous_rejects(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path))
    f = tmp_path / "x.txt"
    f.write_text("dup\ndup\n")
    r = await call_mutating_tool("file_edit", {
        "file_path": str(f), "old_string": "dup", "new_string": "x",
    })
    assert r["ok"] is False
    assert "2 times" in r["output"]


@pytest.mark.asyncio
async def test_file_write_create_new_requires_no_hash(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path))
    f = tmp_path / "new.txt"
    r = await call_mutating_tool("file_write", {
        "file_path": str(f), "content": "hello",
    })
    assert r["ok"] is True
    assert f.read_text() == "hello"


@pytest.mark.asyncio
async def test_file_write_overwrite_existing_requires_hash(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Overwriting an existing file without the expected hash is refused."""
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path))
    f = tmp_path / "x.txt"
    f.write_text("old content")
    r = await call_mutating_tool("file_write", {
        "file_path": str(f), "content": "new content",
    })
    assert r["ok"] is False
    assert "already exists" in r["output"]


@pytest.mark.asyncio
async def test_file_write_overwrite_with_matching_hash(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path))
    f = tmp_path / "x.txt"
    f.write_text("old content")
    h = content_hash("old content")
    r = await call_mutating_tool("file_write", {
        "file_path": str(f), "content": "new content", "expected_hash": h,
    })
    assert r["ok"] is True
    assert f.read_text() == "new content"


# ----------------------------------------------------------- destructive detection

def test_destructive_command_detection():
    assert is_destructive_command("git push --force origin main")
    assert is_destructive_command("git push -f origin main")
    assert is_destructive_command("git reset --hard HEAD~1")
    assert is_destructive_command("rm -rf /")
    assert not is_destructive_command("git push origin main")
    assert not is_destructive_command("git commit -m fix")
    assert not is_destructive_command("ls -la")


# ----------------------------------------------------------- capability registry

def test_mutating_tools_need_approval():
    assert capability_of("file_edit").value == "mutating"
    assert capability_of("file_write").value == "mutating"
    assert capability_of("terminal_exec").value == "mutating"
    assert needs_approval("file_edit", "supervised") is True
    assert needs_approval("file_edit", "autonomous") is False


def test_all_tools_registry_has_seven():
    names = {t.name for t in ALL_TOOLS}
    assert names == {"file_read", "file_search", "file_glob", "terminal_exec",
                     "file_edit", "file_write"}


# ----------------------------------------------------------- approval gate

@pytest.mark.asyncio
async def test_approval_gate_always_allow_persists_class_only(monkeypatch: pytest.MonkeyPatch):
    """always_allow persists the tool CLASS, not the specific target."""
    gate = ApprovalGate("redis://fake", "run-1", "thread-1")
    # Fake Redis: smembers returns empty, then we simulate a decision.
    fake_redis = AsyncMock()
    fake_redis.smembers = AsyncMock(return_value=set())
    fake_redis.sadd = AsyncMock(return_value=1)
    fake_redis.blpop = AsyncMock(return_value=("approval:x:decision",
                                              json.dumps({"decision": "always_allow"})))
    gate.redis = fake_redis
    gate._event_sink = None

    decision = await gate.request("file_edit", {"file_path": "a.py", "old_string": "x", "new_string": "y"})
    assert decision["approved"] is True
    assert decision["via"] == "always_allow"
    # The CLASS was persisted, not the file
    fake_redis.sadd.assert_called_once_with("always_allow:run-1", "file_edit")
    assert "file_edit" in gate._always_allowed


@pytest.mark.asyncio
async def test_approval_gate_destructive_never_always_allow(monkeypatch: pytest.MonkeyPatch):
    """Destructive commands never get always_allow — verbatim every time."""
    gate = ApprovalGate("redis://fake", "run-1", "thread-1")
    fake_redis = AsyncMock()
    fake_redis.smembers = AsyncMock(return_value={"terminal_exec"})  # already always-allowed
    fake_redis.sadd = AsyncMock(return_value=1)
    # Even though terminal_exec is in always_allow, a destructive command must re-approve
    fake_redis.blpop = AsyncMock(return_value=("approval:x:decision",
                                              json.dumps({"decision": "deny", "reason": "no"})))
    gate.redis = fake_redis
    gate._event_sink = None

    decision = await gate.request("terminal_exec", {"command": "git push --force origin main"})
    assert decision["approved"] is False
    # The destructive command did NOT use the always_allow shortcut
    fake_redis.blpop.assert_called_once()


@pytest.mark.asyncio
async def test_approval_gate_timeout_denies(monkeypatch: pytest.MonkeyPatch):
    """Timeout = DENY deterministically."""
    gate = ApprovalGate("redis://fake", "run-1", "thread-1", timeout_s=1)
    fake_redis = AsyncMock()
    fake_redis.smembers = AsyncMock(return_value=set())
    fake_redis.blpop = AsyncMock(return_value=None)  # timeout
    gate.redis = fake_redis
    gate._event_sink = None

    decision = await gate.request("file_edit", {"file_path": "a.py", "old_string": "x", "new_string": "y"})
    assert decision["approved"] is False
    assert "timed out" in decision["reason"]


@pytest.mark.asyncio
async def test_call_any_tool_routes_readonly_directly(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Read-only tools skip the approval gate entirely."""
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path))
    f = tmp_path / "x.txt"
    f.write_text("hi")
    gate = MagicMock()
    gate.request = AsyncMock()
    r = await call_any_tool("file_read", {"file_path": str(f)}, approval_gate=gate, autonomy="supervised")
    assert r["ok"] is True
    gate.request.assert_not_called()  # readonly never hits the gate


@pytest.mark.asyncio
async def test_call_any_tool_routes_mutating_through_gate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Mutating tools in supervised mode go through the gate."""
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path))
    f = tmp_path / "x.txt"
    f.write_text("foo")
    gate = MagicMock()
    gate.request = AsyncMock(return_value={"approved": True, "args": {
        "file_path": str(f), "old_string": "foo", "new_string": "bar"}, "verbatim": True})
    r = await call_any_tool("file_edit", {
        "file_path": str(f), "old_string": "foo", "new_string": "bar",
    }, approval_gate=gate, autonomy="supervised")
    assert r["ok"] is True
    gate.request.assert_called_once()


@pytest.mark.asyncio
async def test_call_any_tool_autonomous_skips_gate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Autonomous mode: nothing is bridged — gate not called."""
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path))
    f = tmp_path / "x.txt"
    f.write_text("foo")
    gate = MagicMock()
    gate.request = AsyncMock()
    r = await call_any_tool("file_edit", {
        "file_path": str(f), "old_string": "foo", "new_string": "bar",
    }, approval_gate=gate, autonomy="autonomous")
    assert r["ok"] is True
    gate.request.assert_not_called()


@pytest.mark.asyncio
async def test_call_any_tool_denial_returns_error(monkeypatch: pytest.MonkeyPatch):
    gate = MagicMock()
    gate.request = AsyncMock(return_value={"approved": False, "reason": "user said no"})
    r = await call_any_tool("file_edit", {"file_path": "a.py", "old_string": "x", "new_string": "y"},
                            approval_gate=gate, autonomy="supervised")
    assert r["ok"] is False
    assert "user said no" in r["output"]
