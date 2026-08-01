import pytest

from app.services import evidence


def test_default_test_command_known_repos():
    assert evidence._default_test_command("ServerApp") == ["python", "-m", "pytest", "-q"]
    assert "test" in evidence._default_test_command("ClientApp")
    assert evidence._default_test_command("Unknown") == ["true"]


async def test_run_tests_records_crash_as_failed(monkeypatch):
    async def boom(*args, **kwargs):
        raise FileNotFoundError("no such workspace")
    monkeypatch.setattr(evidence.asyncio, "create_subprocess_exec", boom)
    result = await evidence.run_tests("/missing", "ServerApp")
    assert result["passed"] is False
    assert result["returncode"] == -1
    assert "no such workspace" in result["stderr"]


async def test_run_tests_parses_exit_code(monkeypatch):
    class FakeProc:
        returncode = 0

        async def communicate(self):
            return (b"ok", b"")
    captured = {}

    async def fake_exec(*args, cwd=None, **kwargs):
        captured["cmd"] = args
        captured["cwd"] = cwd
        return FakeProc()
    monkeypatch.setattr(evidence.asyncio, "create_subprocess_exec", fake_exec)
    result = await evidence.run_tests("/ws", "ServerApp", command=["echo", "hi"])
    assert result["passed"] is True
    assert result["command"] == ["echo", "hi"]
    assert captured["cwd"] == "/ws"


async def test_run_tests_accepts_shell_string_command(monkeypatch):
    """Profile test_cmds are shell strings ("pytest -q") — they must be split
    into argv before subprocess (C4)."""
    class FakeProc:
        returncode = 0

        async def communicate(self):
            return (b"ok", b"")
    captured = {}

    async def fake_exec(*args, cwd=None, **kwargs):
        captured["cmd"] = args
        return FakeProc()
    monkeypatch.setattr(evidence.asyncio, "create_subprocess_exec", fake_exec)
    result = await evidence.run_tests("/ws", "ServerApp", command="python -m pytest -q")
    assert result["passed"] is True
    assert captured["cmd"] == ("python", "-m", "pytest", "-q")


async def test_run_test_commands_aggregates_fail_closed(monkeypatch):
    """C4: every profile command must pass; the first failure wins the report."""
    calls = []

    class FakeProc:
        def __init__(self, rc):
            self.returncode = rc

        async def communicate(self):
            return (b"out", b"")

    async def fake_exec(*args, cwd=None, **kwargs):
        calls.append(args)
        return FakeProc(0 if args[0] == "good" else 3)
    monkeypatch.setattr(evidence.asyncio, "create_subprocess_exec", fake_exec)
    result = await evidence.run_test_commands("/ws", "ServerApp", ["good", "bad"])
    assert result["passed"] is False
    assert result["returncode"] == 3
    assert len(calls) == 2


async def test_run_test_commands_empty_falls_back_to_default(monkeypatch):
    class FakeProc:
        returncode = 0

        async def communicate(self):
            return (b"", b"")
    captured = {}

    async def fake_exec(*args, cwd=None, **kwargs):
        captured["cmd"] = args
        return FakeProc()
    monkeypatch.setattr(evidence.asyncio, "create_subprocess_exec", fake_exec)
    result = await evidence.run_test_commands("/ws", "ServerApp", None)
    assert result["passed"] is True
    assert captured["cmd"] == ("python", "-m", "pytest", "-q")


async def test_stamp_screenshots_empty_routes_returns_empty():
    assert await evidence.stamp_screenshots("r1", "/ws", []) == []


async def test_stamp_screenshots_no_client_returns_empty(monkeypatch):
    monkeypatch.setattr(evidence, "_playwright_client", lambda: None)
    assert await evidence.stamp_screenshots("r1", "/ws", ["/"]) == []


async def test_stamp_screenshots_delegates_to_client(monkeypatch):
    class FakeClient:
        async def capture(self, run_id, workspace, routes):
            return [{"route": r, "path": "x", "captured": True} for r in routes]
    monkeypatch.setattr(evidence, "_playwright_client", lambda: FakeClient())
    out = await evidence.stamp_screenshots("r1", "/ws", ["/", "/about"])
    assert [r["route"] for r in out] == ["/", "/about"]
    assert out[0]["captured"] is True
