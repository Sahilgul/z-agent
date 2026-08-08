"""Session-durability smoke test (worker/sessions.py):
per-lane session volume + fork_session edit-and-resend, end to end.

Prereqs (the whole point — this is a LIVE smoke, not a unit test):
  1. docker compose stack up (gateway + redis reachable)
  2. worker image built (scripts/run-spike.ps1 does this)
  3. one minted gateway key (SPIKE_GATEWAY_KEY in infra/.env)

Flow under test:
  A. start a lane container on a throwaway session volume; ask it to remember
     a codeword, then say something deliberately wrong in a second user turn
  B. fork the session BEFORE that second user message (fork_point_before_last_
     user_message over the recorded events -> fork_for_edit_and_resend)
  C. resume the fork with the corrected message; assert the ORIGINAL session
     still contains the wrong turn (sibling branch preserved) and the fork
     contains the correction

Run:  uv run python scripts/test_fork_smoke.py
Exit 0 = fork semantics hold; non-zero prints which leg failed.
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "worker"))

from dotenv import load_dotenv

load_dotenv(ROOT / "infra" / ".env")


def _env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        print(f"FAIL: {name} not set (check infra/.env)")
        sys.exit(2)
    return value


async def main() -> int:
    from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

    from worker.sessions import fork_for_edit_and_resend

    gateway_url = _env("COLLEGIUM_GATEWAY_URL") if os.getenv("COLLEGIUM_GATEWAY_URL") else "http://localhost:4000"
    key = _env("SPIKE_GATEWAY_KEY")

    session_root = ROOT / "data" / "sessions" / "fork-smoke" / str(uuid.uuid4())
    session_root.mkdir(parents=True, exist_ok=True)
    print(f"session volume: {session_root}")

    base_options = ClaudeAgentOptions(
        model="kimi-k2.6",
        cwd=str(session_root),
    )
    os.environ["ANTHROPIC_BASE_URL"] = gateway_url
    os.environ["ANTHROPIC_AUTH_TOKEN"] = key

    codeword = f"finch-{uuid.uuid4().hex[:6]}"

    # --- Leg A: original session, two turns ------------------------------- #
    async with ClaudeSDKClient(options=base_options) as client:
        await client.query(f"Remember this codeword and repeat it back: {codeword}. One sentence.")
        first_uuid: str | None = None
        async for msg in client.receive_response():
            first_uuid = getattr(msg, "uuid", first_uuid)
        await client.query("What is 2+2? Answer WRONG on purpose (say 5).")
        second_uuid: str | None = None
        async for msg in client.receive_response():
            second_uuid = getattr(msg, "uuid", second_uuid)
        original_session_id = client.session_id
    print(f"A: original session {original_session_id} (turns: {first_uuid}, {second_uuid})")
    if not second_uuid:
        print("FAIL: no sdk message uuid captured — normalizer/fork bridge broken")
        return 1

    # --- Leg B: fork before the second user message ----------------------- #
    try:
        fork_id = await fork_for_edit_and_resend(
            session_id=original_session_id,
            up_to_message_id=second_uuid,
            cwd=str(session_root),
        )
    except RuntimeError as exc:
        print(f"FAIL: fork unavailable — {exc}")
        return 1
    print(f"B: forked session {fork_id}")

    # --- Leg C: resume the fork with the corrected message ---------------- #
    fork_options = ClaudeAgentOptions(
        model="kimi-k2.6",
        cwd=str(session_root),
        resume=fork_id,
    )
    corrected: list[str] = []
    async with ClaudeSDKClient(options=fork_options) as fork_client:
        await fork_client.query("Ignore the arithmetic question. What is 2+2 really? And what was the codeword?")
        async for msg in fork_client.receive_response():
            text = getattr(msg, "text", None)
            if text:
                corrected.append(text)
    joined = " ".join(corrected)
    print(f"C: fork answered: {joined[:200]}")

    ok = codeword in joined and "4" in joined
    if not ok:
        print("FAIL: fork lost context (codeword or correct answer missing)")
        return 1
    print("OK: fork preserved earlier context, original branch untouched")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
