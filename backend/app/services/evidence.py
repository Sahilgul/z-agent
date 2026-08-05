"""Deterministic evidence collection: the control plane — not the
agent — runs tests and stamps screenshots, so evidence is tamper-proof and
agents can't misreport test results.

``run_tests`` shells out to the repo's test command in the stamped clone and
parses the exit code. ``stamp_screenshots`` drives the Playwright MCP client
for UI-change evidence. Both are pure code: zero LLM. The development
blueprint's stamp node calls these, persists the results as ``test_run``
Event rows, and folds them into the delivery evidence package.
"""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import shutil
import signal
from pathlib import Path

from app.core.logging import get_logger

log = get_logger(service="evidence")


def _default_test_command(repo: str) -> list[str]:
    """Repo-specific default test invocation. ServerApp -> pytest, ClientApp ->
    vitest; unknown repos get a no-op ``true`` so the stamp node never crashes
    on a repo without a known test runner (evidence contract still records the
    attempt)."""
    if repo == "ServerApp":
        return ["python", "-m", "pytest", "-q"]
    if repo == "ClientApp":
        return ["npm", "run", "test", "--", "--run"]
    return ["true"]


def _normalize_command(command: list[str] | str | None, repo: str) -> list[str]:
    """Profile test_cmds arrive as shell strings ("pytest -q"); subprocess wants
    argv. None/empty falls back to the repo's default runner."""
    if not command:
        return _default_test_command(repo)
    if isinstance(command, str):
        return shlex.split(command) or _default_test_command(repo)
    return list(command)


async def run_tests(workspace: str, repo: str, command: list[str] | str | None = None,
                    *, timeout: float = 900.0) -> dict:
    """Run the repo's test suite in the stamped clone. Returns a tamper-proof
    signal: ``passed`` mirrors the process exit code, plus truncated stdout/stderr
    for the evidence package. Never raises — a crash (or a hung suite past
    ``timeout`` — unattended pipelines can't wait forever) is recorded as a
    failed run so the run fails closed instead of leaking a partial PR."""
    cmd = _normalize_command(command, repo)
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=workspace,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            return {"repo": repo, "command": list(cmd), "passed": False,
                    "returncode": -1, "stdout": "",
                    "stderr": f"timed out after {timeout:.0f}s"}
        return {
            "repo": repo, "command": list(cmd), "passed": proc.returncode == 0,
            "returncode": proc.returncode,
            "stdout": out.decode(errors="replace")[-2000:],
            "stderr": err.decode(errors="replace")[-2000:],
        }
    except Exception as exc:  # missing runner / bad workspace -> fail closed
        log.warning("test run crashed", repo=repo, error=str(exc)[:200])
        return {"repo": repo, "command": list(cmd), "passed": False,
                "returncode": -1, "stdout": "", "stderr": str(exc)[:200]}


async def run_test_commands(workspace: str, repo: str,
                            commands: list[str] | None = None) -> dict:
    """Run the repo profile's test_cmds (plural) in order and aggregate: passed
    only when EVERY command exits 0; outputs concatenate so the evidence package
    shows the full signal. Empty/None -> the repo's single default command."""
    if not commands:
        return await run_tests(workspace, repo)
    results = [await run_tests(workspace, repo, command=c) for c in commands]
    # L-20: when every command passed, the aggregate returncode defaulted to
    # 0 — the same value an empty `results` would yield (all([])=True, next
    # (... , 0)=0), conflating "all passed" with "no data". The early return
    # above normally prevents empty results, but guard defensively so a
    # caller can never read "no commands ran" as "all passed".
    if not results:
        return {"repo": repo, "command": [], "passed": False,
                "returncode": -1, "stdout": "", "stderr": "no test commands ran"}
    return {
        "repo": repo,
        "command": [r["command"] for r in results],
        "passed": all(r["passed"] for r in results),
        "returncode": next((r["returncode"] for r in results if not r["passed"]), 0),
        "stdout": "\n".join(r["stdout"] for r in results if r["stdout"])[-2000:],
        "stderr": "\n".join(r["stderr"] for r in results if r["stderr"])[-2000:],
    }


async def stamp_screenshots(run_id: str, workspace: str, routes: list[str]) -> list[dict]:
    """Playwright MCP screenshot stamping. Returns one entry per route with the
    stamped artifact path. If Playwright isn't configured the function returns
    ``[]`` (the evidence contract is still satisfied by tests + diff); the
    development stamp node never blocks a PR on screenshot availability."""
    if not routes:
        return []
    client = _playwright_client()
    if client is None:
        log.info("playwright unavailable; skipping screenshots", run_id=run_id)
        return []
    return await client.capture(run_id, workspace, routes)


def _playwright_client():
    from app.sandbox.playwright import PlaywrightMcpClient
    return PlaywrightMcpClient.build()


# ---------------------------------------------------------------------------
# Goal-mode verification gate: the control plane — never the agent — decides
# "everything is green". Tests + ruff + npm lint/build + a bounded dev-server
# boot smoke. A check whose tool/deps aren't on the control plane is SKIPPED
# (reported, never blocking); every check that RUNS must pass.
# ---------------------------------------------------------------------------

def _skipped(name: str, reason: str) -> dict:
    # skipped checks carry passed=True so a naive all(passed) read is also safe;
    # the aggregate filters on `skipped` regardless.
    return {"name": name, "skipped": True, "reason": reason, "passed": True,
            "command": [], "returncode": 0, "stdout": "", "stderr": ""}


def _python_project(ws: Path) -> bool:
    markers = ("pyproject.toml", "setup.py", "setup.cfg", "requirements.txt")
    if any((ws / m).exists() for m in markers):
        return True
    # Bounded probe (never rglob — node_modules would make it a 10k-file walk).
    return any(ws.glob("*.py")) or any(ws.glob("*/*.py")) or any(ws.glob("*/*/*.py"))


def _node_scripts(ws: Path) -> dict | None:
    """None = not a node project; {} = package.json without usable scripts."""
    pkg = ws / "package.json"
    if not pkg.exists():
        return None
    try:
        data = json.loads(pkg.read_text(encoding="utf-8"))
    except Exception:
        return {}
    scripts = data.get("scripts")
    return scripts if isinstance(scripts, dict) else {}


async def _run_check(name: str, cmd: list[str], workspace: str, timeout: float) -> dict:
    """One gated check with a hard timeout (autonomous pipelines can't afford a
    hung npm build). Fail-closed like run_tests: a crash is a failed check."""
    base = {"name": name, "skipped": False, "command": list(cmd)}
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=workspace,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            return {**base, "passed": False, "returncode": -1, "stdout": "",
                    "stderr": f"timed out after {timeout:.0f}s"}
        return {**base, "passed": proc.returncode == 0, "returncode": proc.returncode,
                "stdout": out.decode(errors="replace")[-2000:],
                "stderr": err.decode(errors="replace")[-2000:]}
    except Exception as exc:
        log.warning("verify check crashed", check=name, error=str(exc)[:200])
        return {**base, "passed": False, "returncode": -1, "stdout": "",
                "stderr": str(exc)[:200]}


async def _ruff_check(workspace: str, timeout: float) -> dict:
    if not _python_project(Path(workspace)):
        return _skipped("ruff", "no python sources or packaging markers")
    if shutil.which("ruff") is None:
        return _skipped("ruff", "ruff not on the control-plane PATH")
    return await _run_check("ruff", ["ruff", "check", "."], workspace, timeout)


async def _node_check(name: str, scripts: dict | None, workspace: str, timeout: float) -> dict:
    if scripts is None:
        return _skipped(name, "no package.json")
    if name not in scripts:
        return _skipped(name, f"no '{name}' script in package.json")
    if shutil.which("npm") is None:
        return _skipped(name, "npm not on the control-plane PATH")
    if not (Path(workspace) / "node_modules").exists():
        return _skipped(name, "deps not installed (no node_modules)")
    return await _run_check(name, ["npm", "run", name], workspace, timeout)


async def _boot_smoke(scripts: dict | None, workspace: str, seconds: float) -> dict:
    """Dev-server boot smoke — the goal-mode 'run the application' check. A dev
    server that dies inside the window is broken; one still up at the deadline
    booted (then it's terminated). Bounded by construction."""
    name = "dev-boot"
    if scripts is None:
        return _skipped(name, "no package.json")
    if "dev" not in scripts:
        return _skipped(name, "no 'dev' script in package.json")
    if shutil.which("npm") is None:
        return _skipped(name, "npm not on the control-plane PATH")
    if not (Path(workspace) / "node_modules").exists():
        return _skipped(name, "deps not installed (no node_modules)")
    cmd = ["npm", "run", "dev"]
    base = {"name": name, "skipped": False, "command": cmd}
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=workspace,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            # Own process group: `npm` spawns vite/next as a CHILD — SIGTERM to
            # npm alone orphans the grandchild holding the port, so the next
            # gate round's dev server dies with EADDRINUSE (a false red).
            start_new_session=True,
        )
        try:
            await asyncio.wait_for(proc.wait(), timeout=seconds)
            out = (await proc.stdout.read()) if proc.stdout else b""
            return {**base, "passed": False, "returncode": proc.returncode,
                    "stdout": out.decode(errors="replace")[-2000:],
                    "stderr": f"dev server exited within {seconds:.0f}s of boot"}
        except TimeoutError:
            _kill_process_group(proc, signal.SIGTERM)
            try:
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except TimeoutError:
                _kill_process_group(proc, signal.SIGKILL)
                await proc.wait()
            return {**base, "passed": True, "returncode": 0,
                    "stdout": "", "stderr": f"stayed up {seconds:.0f}s (terminated)"}
    except Exception as exc:
        log.warning("dev-boot smoke crashed", error=str(exc)[:200])
        return {**base, "passed": False, "returncode": -1, "stdout": "",
                "stderr": str(exc)[:200]}


def _kill_process_group(proc: asyncio.subprocess.Process, sig: int) -> None:
    """Signal the dev server's whole process group; fall back to the direct
    child when the group is already gone (race with a dying process)."""
    try:
        os.killpg(os.getpgid(proc.pid), sig)
    except (ProcessLookupError, PermissionError):
        try:
            proc.send_signal(sig)
        except ProcessLookupError:
            pass


async def verify_suite(workspace: str, repo: str,
                       test_cmds: list[str] | None = None, *,
                       smoke_seconds: float = 8.0,
                       check_timeout: float = 600.0) -> dict:
    """The goal-mode green gate: tests + ruff + npm lint + npm build + dev-boot
    smoke, all run by the control plane (tamper-proof). ``passed`` requires
    every check that RAN to pass; skipped checks (tool or deps unavailable on
    the control plane) are reported but never block. The tests check always
    runs, so the suite is never vacuously green."""
    checks: list[dict] = []
    tests = await run_test_commands(workspace, repo, test_cmds)
    checks.append({"name": "tests", "skipped": False, **tests})
    checks.append(await _ruff_check(workspace, check_timeout))
    scripts = _node_scripts(Path(workspace))
    checks.append(await _node_check("lint", scripts, workspace, check_timeout))
    checks.append(await _node_check("build", scripts, workspace, check_timeout))
    checks.append(await _boot_smoke(scripts, workspace, smoke_seconds))
    ran = [c for c in checks if not c["skipped"]]
    return {"repo": repo, "passed": bool(ran) and all(c["passed"] for c in ran),
            "checks": checks}
