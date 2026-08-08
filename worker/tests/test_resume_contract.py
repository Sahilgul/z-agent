"""Wave 2 resume-contract regression tests (worker side).

C2: PERSONA_PROMPT reaches the model (in the initial user message, below
    the frozen system prefix).
C4: an invalid control `mode` payload fails loudly (durable error event).
B1: the engine publishes its stable resumable identity at boot.
C7: the custom engine loads the stamped .mcp.json (Playwright MCP).
K1: file writes are atomic and a crash mid-tools-node cannot double-execute
    a mutating tool (replay journal).
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.asyncio

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_runner_lifecycle import _runner_shell

# ------------------------------------------------------------------- C2

def _runner_for_state(env: dict[str, str]):
    """Minimal EngineRunner for _initial_state (no __init__ side effects)."""
    from worker.engine.runner import EngineRunner
    from worker.engine.state import Autonomy, Budget, Mode
    for k, v in env.items():
        os.environ[k] = v
    try:
        r = EngineRunner.__new__(EngineRunner)
        r.run_id = "r1"
        r.thread_id = "t1"
        r.context_id = "t1"
        r.task_id = "task-1"
        r.task_prompt = "do the thing"
        r.workspace = Path("/workspace")
        r.mode = Mode.ASK
        r.autonomy = Autonomy.SUPERVISED
        r.budget = Budget(cap=5.0)
        return r
    finally:
        for k in env:
            os.environ.pop(k, None)


def test_persona_prompt_reaches_initial_message():
    r = _runner_for_state({"PERSONA_PROMPT": "You are the Lead."})
    try:
        os.environ["PERSONA_PROMPT"] = "You are the Lead."
        state = r._initial_state()
    finally:
        os.environ.pop("PERSONA_PROMPT", None)
    content = state["messages"][0].content
    assert "<persona>" in content and "You are the Lead." in content
    assert "Workspace root:" in content  # task context preserved


def test_persona_prompt_absent_means_no_tag():
    r = _runner_for_state({})
    content = r._initial_state()["messages"][0].content
    assert "<persona>" not in content


# ------------------------------------------------------------------- C4

@pytest.mark.asyncio
async def test_invalid_mode_control_fails_loudly():
    from worker.control import ControlMessage
    r = _runner_shell()
    r.status = "idle"
    from worker.engine.state import Mode
    r.mode = Mode.ASK

    await r.control.queue.put(ControlMessage(type="mode", mode="nonsense"))
    pump = asyncio.create_task(r._control_pump())
    await asyncio.sleep(0.05)
    r._stop.set()
    pump.cancel()
    try:
        await pump
    except asyncio.CancelledError:
        pass
    errors = [e for e in r.forwarder.events
              if isinstance(e, dict) and (e.get("detail") or {}).get("error") == "invalid_mode"]
    assert errors, "invalid mode must emit a durable error event"
    assert r.mode.value == "ask"  # unchanged


# ------------------------------------------------------------------- B1

async def test_engine_identity_event_shape():
    from worker.engine.events import EventEmitter
    em = EventEmitter("r1", "t1", "ctx-1")
    from collegium_contracts import StepKind
    ev = em._next(StepKind.STATUS, "engine identity",
                  {"kind": "engine_identity", "session_id": "ctx-1",
                   "engine": "custom"}, "task-1", None)
    assert ev.detail["session_id"] == "ctx-1"
    assert ev.detail["kind"] == "engine_identity"


# ------------------------------------------------------------------- C7

def test_mcp_loader_reads_stamped_mcp_json(tmp_path, monkeypatch):
    (tmp_path / ".mcp.json").write_text(json.dumps({
        "mcpServers": {"playwright": {"command": "npx",
                                      "args": ["@playwright/mcp@latest"]}}
    }))
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path))
    monkeypatch.delenv("MCP_SERVERS", raising=False)
    from worker.engine.mcp import _servers_from_env
    servers = _servers_from_env()
    assert servers == [{"name": "playwright", "transport": "stdio",
                        "command": "npx", "args": ["@playwright/mcp@latest"]}]

    # Env config wins on name collision; a malformed .mcp.json is ignored.
    monkeypatch.setenv("MCP_SERVERS", json.dumps([{"name": "playwright", "transport": "streamable_http", "url": "http://x/mcp"}]))
    servers = _servers_from_env()
    assert servers[0]["transport"] == "streamable_http"
    (tmp_path / ".mcp.json").write_text("{not json")
    monkeypatch.delenv("MCP_SERVERS")
    assert _servers_from_env() == []


# ------------------------------------------------------------------- K1

def test_file_write_is_atomic(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path))
    from worker.engine.tools import mutating

    # Pre-seed a file, then simulate a crash mid-write by patching
    # os.replace to raise — the original content must be intact.
    target = tmp_path / "a.txt"
    target.write_text("original")
    import os as _os
    real_replace = _os.replace

    def boom(*a, **k):
        raise OSError("simulated crash mid-rename")

    monkeypatch.setattr(mutating.os, "replace", boom)
    out = mutating.file_write.invoke({"file_path": "a.txt", "content": "new",
                                      "expected_hash": mutating.content_hash("original")})
    assert out.startswith("error: write failed")
    assert target.read_text() == "original"
    monkeypatch.setattr(mutating.os, "replace", real_replace)

    out = mutating.file_write.invoke({"file_path": "a.txt", "content": "new",
                                      "expected_hash": mutating.content_hash("original")})
    assert out.startswith("wrote")
    assert target.read_text() == "new"
    # No stray temp files.
    assert [p.name for p in tmp_path.iterdir()] == ["a.txt"]


def test_replay_journal_hit_skips_execution(tmp_path):
    from worker.engine.checkpointer import ReplayJournal
    j = ReplayJournal(tmp_path)
    result = {"kind": "success", "ok": True, "output": "ran once"}
    j.record("tc-1", "terminal_exec", result)
    assert j.lookup("tc-1") == result
    assert j.lookup("tc-missing") is None
    assert j.lookup("") is None
    # A torn entry (crash mid-record) reads as a MISS, not corruption.
    (j.dir / "tc-bad.json").write_text("{torn")
    assert j.lookup("tc-bad") is None


async def test_tools_node_replay_guard(tmp_path):
    """End-to-end at the node level: a journaled tool_call_id returns the
    recorded result WITHOUT re-executing the tool."""
    from langchain_core.messages import AIMessage

    from worker.engine.checkpointer import ReplayJournal
    from worker.engine.events import EventEmitter
    from worker.engine.graph import tools_node

    journal = ReplayJournal(tmp_path)
    recorded = {"kind": "success", "ok": True,
                "output": "wrote f.py (10 chars). new hash: abc"}
    journal.record("call-1", "file_write", recorded)

    executed = []
    import worker.engine.graph as graph_mod
    real_call = graph_mod.call_tool_direct

    async def spy(name, args):
        executed.append(name)
        return await real_call(name, args)

    graph_mod.call_tool_direct = spy
    try:
        ai = AIMessage(content="", tool_calls=[{
            "id": "call-1", "name": "file_write",
            "args": {"file_path": "f.py", "content": "x"},
        }])
        state = {"messages": [ai], "task_id": "task-1", "thread_id": "t1",
                 "approved_calls": {}, "tool_streak": {}}
        config = {"configurable": {
            "emitter": EventEmitter("r1", "t1"),
            "replay_journal": journal,
        }}
        out = await tools_node(state, config)
    finally:
        graph_mod.call_tool_direct = real_call

    assert executed == []  # no re-execution on a journal hit
    tool_msg = out["messages"][-1]
    assert tool_msg.content == recorded["output"]
