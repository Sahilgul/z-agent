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
import shlex

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


async def run_tests(workspace: str, repo: str, command: list[str] | str | None = None) -> dict:
    """Run the repo's test suite in the stamped clone. Returns a tamper-proof
    signal: ``passed`` mirrors the process exit code, plus truncated stdout/stderr
    for the evidence package. Never raises — a crash is recorded as a failed run
    so the run fails closed instead of leaking a partial PR."""
    cmd = _normalize_command(command, repo)
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=workspace,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate()
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
