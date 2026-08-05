"""terminal_exec background contract.

30s foreground -> auto-background. Per-job: output file + in-memory ring
buffer + 16MiB ceiling force-kill; header/body/footer format on reads;
head+tail truncation ("... N bytes omitted ..."); regex watch with >=5s
debounce; completion notify (the graph surfaces it at turn end); exit 124 on
timeout; killProcessTree on kill; one command per job; stdin closed.
"""

from __future__ import annotations

import asyncio
import os
import re
import signal
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Any

FOREGROUND_TIMEOUT_S = 30.0
OUTPUT_CEILING_BYTES = 16 * 1024 * 1024  # 16MiB force-kill
RING_BUFFER_LINES = 2000
MAX_OUTPUT_CHARS = 32 * 1024
WATCH_DEBOUNCE_S = 5.0
JOB_TIMEOUT_S = 2 * 60 * 60  # 2h cap -> exit 124
# Grace window after SIGTERM before SIGKILL escalation (H-04): a stubborn
# process that traps/ignores SIGTERM must not hang the pump forever.
_KILL_GRACE_S = 5.0


def _jobs_dir() -> Path:
    ws = Path(os.environ.get("WORKSPACE_DIR", "/workspace"))
    d = ws / ".zagent" / "terminal"
    d.mkdir(parents=True, exist_ok=True)
    return d


class BackgroundJob:
    def __init__(self, command: str, workspace: Path, watch_regex: str | None) -> None:
        self.job_id = f"job-{uuid.uuid4().hex[:8]}"
        self.command = command
        self.workspace = workspace
        self.watch_regex = watch_regex
        self.output_file = _jobs_dir() / f"{self.job_id}.out"
        self.ring: deque[str] = deque(maxlen=RING_BUFFER_LINES)
        self.total_bytes = 0
        self.exit_code: int | None = None
        self.killed_by: str | None = None  # "ceiling" | "timeout" | "kill"
        self.started = time.monotonic()
        self.ended: float | None = None
        self._proc: asyncio.subprocess.Process | None = None
        self._last_watch_hit: float = 0.0
        self.watch_hits: list[str] = []

    @property
    def running(self) -> bool:
        return self.exit_code is None

    def duration_s(self) -> float:
        return round((self.ended or time.monotonic()) - self.started, 1)


class TerminalManager:
    """Owns all background jobs for this engine process."""

    def __init__(self) -> None:
        self.jobs: dict[str, BackgroundJob] = {}

    async def run(self, command: str, *, background: bool = False,
                  watch_regex: str | None = None,
                  foreground_timeout: float = FOREGROUND_TIMEOUT_S) -> dict[str, Any]:
        """Foreground-first: stream the command; if it outlives the foreground
        window (or background=True), hand it to a job and return the
        background_started typed result."""
        workspace = Path(os.environ.get("WORKSPACE_DIR", "/workspace"))
        # Compile the watch regex BEFORE spawning: an invalid pattern must
        # fail the call cleanly, not kill the pump afterwards and leak a
        # running process stuck "running" forever.
        try:
            watch = re.compile(watch_regex) if watch_regex else None
        except re.error as exc:
            return {"kind": "error", "ok": False,
                    "output": f"error: invalid watch_regex: {exc}",
                    "tool": "terminal_exec",
                    "details": {"background": False}}
        job = BackgroundJob(command, workspace, watch_regex)
        job._watch = watch  # type: ignore[attr-defined]
        self.jobs[job.job_id] = job
        job._proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            stdin=asyncio.subprocess.DEVNULL,
            cwd=str(workspace),
            start_new_session=True,  # own process group -> killProcessTree
        )
        reader = asyncio.create_task(self._pump(job))

        if not background:
            try:
                # No shield: the pump owns completion. wait_for cancels the
                # _wait helper cleanly on timeout — shielding it orphaned one
                # sleeper task per auto-backgrounded command.
                await asyncio.wait_for(self._wait(job), timeout=foreground_timeout)
            except TimeoutError:
                pass  # auto-background below

        if job.running:
            return {
                "kind": "background_started",
                "ok": True,
                "output": self._header(job) + (
                    f"command exceeded the {foreground_timeout:.0f}s foreground window"
                    if not background else "started in background"
                ) + f" — job id: {job.job_id}\n"
                    f"poll with terminal_await/job_id or read {job.output_file}",
                "job_id": job.job_id,
                "tool": "terminal_exec",
                "details": {"background": True, "watch_regex": watch_regex},
            }
        reader.cancel()
        try:
            await reader
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            # The pump owns job completion (exit_code/ended are always set in
            # its finally); a pump error must surface as a typed result, not
            # propagate out of run() leaving the job looking alive.
            pass
        return {
            "kind": "success" if job.exit_code == 0 else "error",
            "ok": job.exit_code == 0,
            "output": self.render(job),
            "job_id": job.job_id,
            "tool": "terminal_exec",
            "details": {"background": False, "exit_code": job.exit_code},
        }

    async def _pump(self, job: BackgroundJob) -> None:
        assert job._proc and job._proc.stdout
        loop = asyncio.get_running_loop()
        deadline = loop.time() + JOB_TIMEOUT_S
        # Precompiled in run() (invalid patterns are rejected before spawn).
        watch: re.Pattern[str] | None = getattr(job, "_watch", None)
        try:
            await self._pump_loop(job, watch, loop, deadline)
            rc = await self._reap(job)
            job.exit_code = 124 if job.killed_by == "timeout" else rc
        except asyncio.CancelledError:
            # Cancelled mid-flight (shutdown): reap the process so it is
            # never leaked, then let the cancellation propagate.
            if job._proc.returncode is None:
                try:
                    os.killpg(os.getpgid(job._proc.pid), signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
                try:
                    await asyncio.wait_for(job._proc.wait(), timeout=_KILL_GRACE_S)
                except TimeoutError:
                    pass
            job.exit_code = job.exit_code if job.exit_code is not None else -1
            raise
        except Exception as exc:  # noqa: BLE001
            # The pump owns the job's terminal state. ANY failure here (file
            # I/O, reap error) must end the job with a verdict — never leave
            # it "running" forever with the exception lost to the void.
            job.killed_by = job.killed_by or "pump_error"
            job.exit_code = 1
            job.watch_hits.append(f"pump error: {exc}")
        finally:
            job.ended = time.monotonic()

    async def _pump_loop(self, job: BackgroundJob, watch: "re.Pattern[str] | None",
                         loop: asyncio.AbstractEventLoop, deadline: float) -> None:
        with job.output_file.open("ab") as fh:
            while True:
                # M-08: check the ceiling at the TOP of the loop too. The old
                # code only checked after a successful read, so a stalled
                # oversized job (reads timing out forever) escaped the kill
                # and ran to the JOB_TIMEOUT_S deadline, ballooning the
                # output file and the ring buffer.
                if job.total_bytes > OUTPUT_CEILING_BYTES:
                    job.killed_by = "ceiling"
                    self.kill(job, reason="ceiling")
                    break
                if loop.time() > deadline:
                    job.killed_by = "timeout"
                    self.kill(job, reason="timeout")
                    break
                try:
                    chunk = await asyncio.wait_for(job._proc.stdout.read(4096), timeout=1.0)
                except TimeoutError:
                    continue
                if not chunk:
                    break
                fh.write(chunk)
                fh.flush()
                text = chunk.decode("utf-8", errors="replace")
                job.total_bytes += len(chunk)
                job.ring.extend(text.splitlines())
                if watch and loop.time() - job._last_watch_hit >= WATCH_DEBOUNCE_S:
                    hit = watch.search(text)
                    if hit:
                        job._last_watch_hit = loop.time()
                        job.watch_hits.append(hit.group(0))
                if job.total_bytes > OUTPUT_CEILING_BYTES:
                    job.killed_by = "ceiling"
                    self.kill(job, reason="ceiling")
                    break

    async def _reap(self, job: BackgroundJob) -> int:
        """Wait for the process to exit, escalating SIGTERM -> SIGKILL after
        a grace period (H-04). The old code did `await job._proc.wait()` with
        no escalation, so a process that ignored the SIGTERM from kill() kept
        the pump alive forever. SIGKILL is uncatchable, so this is the hard floor."""
        try:
            return await asyncio.wait_for(job._proc.wait(), timeout=_KILL_GRACE_S)
        except TimeoutError:
            try:
                os.killpg(os.getpgid(job._proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                return await asyncio.wait_for(job._proc.wait(), timeout=_KILL_GRACE_S)
            except TimeoutError:
                # SIGKILL denied (permissions) and the process won't die —
                # record a verdict instead of hanging the pump forever.
                job.killed_by = job.killed_by or "kill_failed"
                return -1

    async def _wait(self, job: BackgroundJob) -> None:
        while job.running:
            await asyncio.sleep(0.2)

    def kill(self, job_or_id: BackgroundJob | str, *, reason: str = "kill") -> bool:
        job = self._job(job_or_id)
        if job is None or not job.running or job._proc is None:
            return False
        try:
            os.killpg(os.getpgid(job._proc.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
        job.killed_by = job.killed_by or reason
        return True

    def completed_notifications(self) -> list[str]:
        """Turn-end completion notifies (consumed once per turn)."""
        notes = []
        for job in self.jobs.values():
            if not job.running and not getattr(job, "_notified", False):
                job._notified = True  # type: ignore[attr-defined]
                status = f"exit {job.exit_code}"
                if job.killed_by:
                    status += f" (killed: {job.killed_by})"
                notes.append(f"background job {job.job_id} finished: {status} — {job.command[:80]}")
        for job in self.jobs.values():
            while job.watch_hits:
                notes.append(f"background job {job.job_id} watch matched: {job.watch_hits.pop(0)}")
        return notes

    # --- rendering: header/body/footer + head+tail truncation ---

    def _header(self, job: BackgroundJob) -> str:
        return (f"=== background job {job.job_id} ===\n"
                f"command: {job.command}\nstarted: {job.started:.0f} "
                f"({job.duration_s()}s elapsed)\n---\n")

    def _footer(self, job: BackgroundJob) -> str:
        if job.running:
            return f"\n---\n[still running — {job.total_bytes} bytes captured]"
        suffix = f" (killed: {job.killed_by})" if job.killed_by else ""
        return f"\n---\n[exit {job.exit_code}{suffix} — {job.duration_s()}s, {job.total_bytes} bytes]"

    def render(self, job_or_id: BackgroundJob | str, *, tail: int = 200) -> str:
        job = self._job(job_or_id)
        if job is None:
            return "error: unknown job id"
        lines = list(job.ring)
        if len(lines) > tail:
            # H-05: the old code hardcoded `lines[:50]` + `lines[-50:]` (ignoring
            # `tail`), so for tail < 100 the head/tail slices overlapped and
            # `len(lines) - 100` went NEGATIVE ("... -10 lines omitted ...").
            # Split tail into head/tail halves and omit exactly the middle.
            head = tail // 2
            keep_tail = tail - head
            omitted = len(lines) - tail  # always >= 0: we only truncate when len > tail
            body = "\n".join(
                lines[:head] + [f"... {omitted} lines omitted ..."] + lines[-keep_tail:]
            )
        else:
            body = "\n".join(lines)
        out = self._header(job) + body + self._footer(job)
        return out[:MAX_OUTPUT_CHARS]

    def _job(self, job_or_id: BackgroundJob | str) -> BackgroundJob | None:
        return job_or_id if isinstance(job_or_id, BackgroundJob) else self.jobs.get(job_or_id)


_MANAGER: TerminalManager | None = None


def terminal_manager() -> TerminalManager:
    global _MANAGER
    if _MANAGER is None:
        _MANAGER = TerminalManager()
    return _MANAGER


__all__ = [
    "FOREGROUND_TIMEOUT_S",
    "OUTPUT_CEILING_BYTES",
    "WATCH_DEBOUNCE_S",
    "BackgroundJob",
    "TerminalManager",
    "terminal_manager",
]
