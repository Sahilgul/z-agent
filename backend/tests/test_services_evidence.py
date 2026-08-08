import asyncio

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


# ---------------------------------------------------------------------------
# verify_suite — the goal-mode green gate
# ---------------------------------------------------------------------------

class _FakeCheckProc:
    def __init__(self, returncode=0, out=b"", err=b""):
        self.returncode = returncode
        self._out = out
        self._err = err

    async def communicate(self):
        return self._out, self._err


class _AliveProc:
    """Dev server that stays up: wait() blocks until terminate/kill."""

    # Beyond the default Linux pid_max (2^22): os.getpgid on it raises
    # ProcessLookupError deterministically, so the group-kill path falls back
    # to send_signal in tests that don't stub os.killpg.
    pid = 2**22 + 1

    def __init__(self):
        self.returncode = None
        self.stdout = None
        self._stopped = asyncio.Event()

    async def wait(self):
        await self._stopped.wait()
        self.returncode = 0
        return 0

    def terminate(self):
        self._stopped.set()

    def kill(self):
        self._stopped.set()

    def send_signal(self, sig):
        self._stopped.set()


class _AsyncOut:
    def __init__(self, data):
        self._data = data

    async def read(self, n=-1):
        return self._data


class _DeadProc:
    """Dev server that dies inside the smoke window."""

    pid = 2**22 + 1

    def __init__(self, returncode=2, out=b""):
        self.returncode = returncode
        self.stdout = _AsyncOut(out)
        self._stopped = asyncio.Event()

    async def wait(self):
        return self.returncode

    def terminate(self):
        self._stopped.set()

    def kill(self):
        self._stopped.set()

    def send_signal(self, sig):
        self._stopped.set()


def _patch_tests(monkeypatch, passed=True):
    async def fake_tests(workspace, repo, commands=None):
        return {"repo": repo, "command": ["pytest"], "passed": passed,
                "returncode": 0 if passed else 1,
                "stdout": "2 passed" if passed else "1 failed", "stderr": ""}
    monkeypatch.setattr(evidence, "run_test_commands", fake_tests)


def _no_tools(which_tool):
    return None


async def test_verify_suite_skipped_checks_never_block(tmp_path, monkeypatch):
    """No python markers, no package.json, no tools: only the tests check
    runs; everything else skips and the gate is green."""
    _patch_tests(monkeypatch, passed=True)
    monkeypatch.setattr(evidence.shutil, "which", _no_tools)
    result = await evidence.verify_suite(str(tmp_path), "ServerApp")
    assert result["passed"] is True
    assert [c["name"] for c in result["checks"]] == [
        "tests", "ruff", "lint", "build", "dev-boot"]
    assert all(c["skipped"] for c in result["checks"][1:])
    assert result["checks"][0]["passed"] is True


async def test_verify_suite_failing_tests_block(tmp_path, monkeypatch):
    _patch_tests(monkeypatch, passed=False)
    monkeypatch.setattr(evidence.shutil, "which", _no_tools)
    result = await evidence.verify_suite(str(tmp_path), "ServerApp")
    assert result["passed"] is False


async def test_verify_suite_failing_ruff_blocks(tmp_path, monkeypatch):
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    _patch_tests(monkeypatch, passed=True)
    monkeypatch.setattr(evidence.shutil, "which",
                        lambda tool: "/usr/bin/ruff" if tool == "ruff" else None)

    async def fake_exec(*args, cwd=None, **kwargs):
        return _FakeCheckProc(returncode=1, out=b"", err=b"E501 line too long")

    monkeypatch.setattr(evidence.asyncio, "create_subprocess_exec", fake_exec)
    result = await evidence.verify_suite(str(tmp_path), "ServerApp")
    assert result["passed"] is False
    ruff = next(c for c in result["checks"] if c["name"] == "ruff")
    assert ruff["skipped"] is False
    assert ruff["passed"] is False
    assert "E501" in ruff["stderr"]


async def test_verify_suite_dev_boot_alive_passes(tmp_path, monkeypatch):
    (tmp_path / "package.json").write_text(
        '{"scripts": {"dev": "vite"}}')
    (tmp_path / "node_modules").mkdir()
    _patch_tests(monkeypatch, passed=True)
    monkeypatch.setattr(evidence.shutil, "which",
                        lambda tool: f"/usr/bin/{tool}")

    async def fake_exec(*args, cwd=None, **kwargs):
        return _AliveProc()

    monkeypatch.setattr(evidence.asyncio, "create_subprocess_exec", fake_exec)
    result = await evidence.verify_suite(str(tmp_path), "ServerApp",
                                         smoke_seconds=0.01)
    assert result["passed"] is True
    boot = next(c for c in result["checks"] if c["name"] == "dev-boot")
    assert boot["passed"] is True
    assert "stayed up" in boot["stderr"]
    # lint/build skipped: no such scripts in package.json
    assert next(c for c in result["checks"] if c["name"] == "lint")["skipped"]


async def test_verify_suite_dev_boot_dying_fails(tmp_path, monkeypatch):
    (tmp_path / "package.json").write_text(
        '{"scripts": {"dev": "vite"}}')
    (tmp_path / "node_modules").mkdir()
    _patch_tests(monkeypatch, passed=True)
    monkeypatch.setattr(evidence.shutil, "which",
                        lambda tool: f"/usr/bin/{tool}")

    async def fake_exec(*args, cwd=None, **kwargs):
        return _DeadProc(returncode=2, out=b"vite: command not found")

    monkeypatch.setattr(evidence.asyncio, "create_subprocess_exec", fake_exec)
    result = await evidence.verify_suite(str(tmp_path), "ServerApp",
                                         smoke_seconds=0.01)
    assert result["passed"] is False
    boot = next(c for c in result["checks"] if c["name"] == "dev-boot")
    assert boot["passed"] is False
    assert "exited within" in boot["stderr"]
    assert "command not found" in boot["stdout"]


async def test_verify_suite_node_checks_need_node_modules(tmp_path, monkeypatch):
    """A package.json with a build script but no node_modules -> build SKIPS
    (deps not installed); it must not block the gate."""
    (tmp_path / "package.json").write_text(
        '{"scripts": {"build": "tsc"}}')
    _patch_tests(monkeypatch, passed=True)
    monkeypatch.setattr(evidence.shutil, "which",
                        lambda tool: f"/usr/bin/{tool}")
    result = await evidence.verify_suite(str(tmp_path), "ClientApp")
    assert result["passed"] is True
    build = next(c for c in result["checks"] if c["name"] == "build")
    assert build["skipped"] is True
    assert "node_modules" in build["reason"]


async def test_stamp_screenshots_delegates_to_client(monkeypatch):
    class FakeClient:
        async def capture(self, run_id, workspace, routes):
            return [{"route": r, "path": "x", "captured": True} for r in routes]
    monkeypatch.setattr(evidence, "_playwright_client", lambda: FakeClient())
    out = await evidence.stamp_screenshots("r1", "/ws", ["/", "/about"])
    assert [r["route"] for r in out] == ["/", "/about"]
    assert out[0]["captured"] is True


async def test_boot_smoke_kills_process_group_first(tmp_path, monkeypatch):
    """`npm run dev` spawns vite/next as a grandchild — SIGTERM must go to the
    whole process group (start_new_session) or the orphan holds the port and
    the next gate round false-reds with EADDRINUSE."""
    (tmp_path / "package.json").write_text('{"scripts": {"dev": "vite"}}')
    (tmp_path / "node_modules").mkdir()
    _patch_tests(monkeypatch, passed=True)
    monkeypatch.setattr(evidence.shutil, "which", lambda tool: f"/usr/bin/{tool}")
    group_kills, exec_kwargs, procs = [], {}, []

    async def fake_exec(*args, cwd=None, **kwargs):
        exec_kwargs.update(kwargs)
        procs.append(_AliveProc())
        return procs[-1]

    def fake_killpg(pgid, sig):
        group_kills.append((pgid, sig))
        procs[-1]._stopped.set()  # the signal really kills the group

    monkeypatch.setattr(evidence.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(evidence.os, "getpgid", lambda pid: 777)
    monkeypatch.setattr(evidence.os, "killpg", fake_killpg)
    result = await evidence.verify_suite(str(tmp_path), "ServerApp",
                                         smoke_seconds=0.01)
    boot = next(c for c in result["checks"] if c["name"] == "dev-boot")
    assert boot["passed"] is True
    assert exec_kwargs.get("start_new_session") is True
    assert (777, evidence.signal.SIGTERM) in group_kills


async def test_run_tests_timeout_fails_closed(monkeypatch):
    """A hung suite must not hang an unattended pipeline forever: the check
    records a failure with the timeout as the reason."""
    class HungProc:
        returncode = None
        killed = False

        async def communicate(self):
            await asyncio.sleep(60)
            return (b"", b"")

        def kill(self):
            self.killed = True
            self.returncode = -9

        async def wait(self):
            return self.returncode

    proc = HungProc()

    async def fake_exec(*args, cwd=None, **kwargs):
        return proc

    monkeypatch.setattr(evidence.asyncio, "create_subprocess_exec", fake_exec)
    result = await evidence.run_tests("/ws", "ServerApp",
                                      command=["sleep", "60"], timeout=0.05)
    assert result["passed"] is False
    assert result["returncode"] == -1
    assert "timed out" in result["stderr"]
    assert proc.killed is True
