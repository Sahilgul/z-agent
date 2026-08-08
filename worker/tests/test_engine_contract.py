"""Contract tests for the engine (highest leverage).

These do NOT hit the gateway. They validate:
- The EventEmitter produces correctly-shaped StepEvents.
- Tool-use -> tool-result pairing yields one complete event per tool.
- Secrets redaction is applied at the event boundary.
- The capability registry gates approval correctly.
- The graph compiles and routes agent->tools->agent->end.
- Idempotency keys are stable for identical calls.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from collegium_contracts import StepKind
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from worker.engine.events import EventEmitter
from worker.engine.graph import (
    _normalize_tool_call_args,
    _should_continue,
    _tool_kind,
    _tool_title,
    build_graph,
)
from worker.engine.llm import get_capabilities
from worker.engine.security import is_sensitive_path, redact, redact_dict
from worker.engine.state import Budget, EngineState, PromptOrigin, tag_message
from worker.engine.tools import (
    call_tool,
    capability_of,
    idempotency_key,
    needs_approval,
)

# ----------------------------------------------------------- EventEmitter

def test_emitter_assigns_monotonic_seq():
    em = EventEmitter("run-1", "thread-1")
    e1 = em._next(StepKind.MESSAGE, "first", {"text": "a"}, "task-1", None)
    e2 = em._next(StepKind.MESSAGE, "second", {"text": "b"}, "task-1", None)
    assert e1.seq == 0 and e2.seq == 1
    assert e1.run_id == "run-1" and e1.thread_id == "thread-1"
    assert e1.context_id == "thread-1"  # defaults to thread_id
    assert e1.task_id == "task-1"
    assert e1.schema_version == 1


def test_emitter_pairs_tool_use_with_result():
    em = EventEmitter("run-1", "thread-1", context_id="thread-1::worker-1")
    ai = AIMessage(
        content="",
        tool_calls=[{"id": "tc-1", "name": "file_read", "args": {"file_path": "x.py"}}],
    )
    # W-H10: no call-time partial event — staging only. The ONE complete
    # event per tool call is emitted at step end (from_tool_result / the
    # tools node), so the feed never renders a ghost "{tool, input}" card
    # followed by the real result card.
    events = em.from_assistant(ai, "task-1")
    assert events == []
    assert em._pending_tools["tc-1"]["name"] == "file_read"

    tm = ToolMessage(content="file contents", tool_call_id="tc-1", name="file_read")
    result_event = em.from_tool_result(tm, "task-1")
    assert result_event.kind == StepKind.FILE_READ
    assert result_event.detail["output"] == "file contents"
    assert result_event.detail["ok"] is True


def test_emitter_redacts_tool_output():
    em = EventEmitter("run-1", "thread-1")
    tm = ToolMessage(
        content="Bearer sk-1234567890abcdef1234567890 leaked",
        tool_call_id="tc-1", name="terminal_exec",
    )
    # Pre-register the pending tool so pairing works
    ai = AIMessage(content="", tool_calls=[{"id": "tc-1", "name": "terminal_exec", "args": {"command": "env"}}])
    em.from_assistant(ai, "task-1")
    event = em.from_tool_result(tm, "task-1")
    assert "REDACTED" in event.detail["output"]
    assert "sk-1234567890" not in event.detail["output"]


def test_tool_kind_map_is_unified_across_emission_paths():
    """W-H10: graph.py and events.py used to carry separate _tool_kind maps;
    file_edit/file_write were FILE_EDIT on one path and COMMAND on the
    other. There is ONE canonical map now."""
    from worker.engine import graph
    from worker.engine.events import _tool_kind
    assert graph._tool_kind is _tool_kind
    assert _tool_kind("file_edit") == StepKind.FILE_EDIT
    assert _tool_kind("file_write") == StepKind.FILE_EDIT


def test_emitter_stamps_event_uid():
    """W-B6: every emission carries a uuid4 the backend dedupes on."""
    em = EventEmitter("run-1", "thread-1")
    e1 = em._next(StepKind.MESSAGE, "a", {"text": "a"}, "task-1", None)
    e2 = em._next(StepKind.MESSAGE, "b", {"text": "b"}, "task-1", None)
    assert e1.event_uid and e2.event_uid
    assert e1.event_uid != e2.event_uid


def test_emitter_turn_boundary_event():
    em = EventEmitter("run-1", "thread-1")
    event = em.turn_boundary("task-1", num_turns=3, duration_ms=1500, is_error=False, usage={"input_tokens": 100})
    assert event.kind == StepKind.STATUS
    assert event.title == "turn complete"
    assert event.detail["num_turns"] == 3
    assert event.detail["is_error"] is False


# ----------------------------------------------------------- Security

def test_redact_bearer_token():
    assert "REDACTED" in redact("Authorization: Bearer sk-ant-1234567890abcdefghij")


def test_redact_aws_key():
    assert "REDACTED" in redact("key=AKIAIOSFODNN7EXAMPLE")


def test_redact_private_key_block():
    text = "-----BEGIN RSA PRIVATE KEY-----\nMIIabc\n-----END RSA PRIVATE KEY-----"
    assert "REDACTED" in redact(text)
    assert "MIIabc" not in redact(text)


def test_redact_generic_assignment():
    assert "REDACTED" in redact('api_key="abcdefghijklmnop123456"')
    assert "REDACTED" in redact("password: supersecretvalue12345")


def test_redact_dict_recursive():
    d = {"a": "Bearer sk-1234567890abcdef1234567890", "b": {"c": "AKIAIOSFODNN7EXAMPLE"}}
    out = redact_dict(d)
    assert "REDACTED" in out["a"]
    assert "REDACTED" in out["b"]["c"]


def test_sensitive_path_detection():
    assert is_sensitive_path(".env")
    assert is_sensitive_path("id_rsa")
    assert is_sensitive_path("/home/user/.ssh/id_ed25519")
    assert not is_sensitive_path("README.md")
    assert not is_sensitive_path("src/main.py")


# ----------------------------------------------------------- Capability registry

def test_readonly_tools_need_no_approval():
    # file_read/search/glob stay readonly; terminal_exec is now MUTATING.
    for name in ("file_read", "file_search", "file_glob"):
        assert capability_of(name).value == "readonly"
        assert needs_approval(name, "supervised") is False
    assert capability_of("terminal_exec").value == "mutating"
    assert needs_approval("terminal_exec", "supervised") is True


def test_autonomous_bypasses_all_approval():
    # Pin a MUTATING tool — a readonly tool never needs approval in ANY mode,
    # so asserting on file_read would pass even if autonomous didn't bypass.
    assert needs_approval("terminal_exec", "autonomous") is False
    assert needs_approval("file_edit", "autonomous") is False


def test_idempotency_key_stable_for_identical_calls():
    k1 = idempotency_key("r1", "t1", "task1", "file_read", {"file_path": "a.py"})
    k2 = idempotency_key("r1", "t1", "task1", "file_read", {"file_path": "a.py"})
    k3 = idempotency_key("r1", "t1", "task1", "file_read", {"file_path": "b.py"})
    assert k1 == k2
    assert k1 != k3
    assert k1.startswith("idem-")


# ----------------------------------------------------------- Graph routing

def test_graph_compiles():
    g = build_graph()
    assert g is not None


def test_should_continue_routes_to_tools_when_tool_calls():
    ai = AIMessage(content="", tool_calls=[{"id": "1", "name": "file_read", "args": {}}])
    state: EngineState = {"messages": [ai], "done": False}  # type: ignore[typeddict-item]
    assert _should_continue(state) == "tools"


def test_should_continue_routes_to_end_when_no_tool_calls():
    ai = AIMessage(content="final answer")
    state: EngineState = {"messages": [ai], "done": False}  # type: ignore[typeddict-item]
    assert _should_continue(state) == "end"


def test_should_continue_routes_to_end_when_done():
    state: EngineState = {"messages": [], "done": True}  # type: ignore[typeddict-item]
    assert _should_continue(state) == "end"


def test_tool_kind_mapping():
    assert _tool_kind("file_read") == StepKind.FILE_READ
    assert _tool_kind("terminal_exec") == StepKind.COMMAND
    assert _tool_kind("mcp__linear__create_issue") == StepKind.MCP_CALL


def test_tool_title_format():
    assert _tool_title("terminal_exec", {"command": "ls -la"}).startswith("$ ls -la")
    assert "x.py" in _tool_title("file_read", {"file_path": "x.py"})


# ----------------------------------------------------------- Tools (no gateway)

@pytest.mark.asyncio
async def test_file_read_missing_file(tmp_path: Path):
    r = await call_tool("file_read", {"file_path": str(tmp_path / "nope")})
    assert r["kind"] == "error"
    assert r["ok"] is False


@pytest.mark.asyncio
async def test_file_read_existing_file(tmp_path: Path):
    f = tmp_path / "x.txt"
    f.write_text("line1\nline2\n")
    r = await call_tool("file_read", {"file_path": str(f)})
    assert r["kind"] == "success"
    assert "line1" in r["output"]


@pytest.mark.asyncio
async def test_terminal_exec_readonly_variant_blocks_mutating(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """The readonly terminal_exec (readonly.py) blocks mutating commands.

    NOTE: this pins the READONLY registry variant only (call_tool). Production
    dispatch (call_tool_direct) routes terminal_exec to the MUTATING manager
    (C-02) — the real production boundary is the approval gate +
    is_destructive_command, pinned by test_terminal_exec_direct_dispatch_*."""
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path))
    r = await call_tool("terminal_exec", {"command": "rm -rf /"})
    assert r["kind"] == "error"
    assert "blocked" in r["output"].lower() or "error" in r["output"].lower()


@pytest.mark.asyncio
async def test_terminal_exec_direct_dispatch_executes_mutating_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """The REAL production dispatch (call_tool_direct) routes terminal_exec
    to the mutating TerminalManager — a command the readonly variant would
    block must EXECUTE here (the gate upstream already decided)."""
    from worker.engine.tools import call_tool_direct
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path))
    r = await call_tool_direct("terminal_exec", {"command": "mkdir direct-dispatch-proof"})
    assert r["kind"] == "success", f"mutating dispatch did not execute: {r['output']}"
    assert (tmp_path / "direct-dispatch-proof").is_dir()


def test_terminal_exec_destructive_never_always_allowable():
    """The production boundary for destructive commands post-C-02: they run
    after approval, but is_destructive_command keeps them OUT of the
    always_allow class — verbatim approval every single time."""
    from worker.engine.tools.mutating import is_destructive_command
    assert is_destructive_command("rm -rf /")
    assert is_destructive_command("git push --force origin main")
    assert is_destructive_command("git reset --hard")
    assert not is_destructive_command("ls -la")


@pytest.mark.asyncio
async def test_terminal_exec_allows_readonly(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path))
    r = await call_tool("terminal_exec", {"command": "echo hello-engine"})
    assert r["kind"] == "success"
    assert "hello-engine" in r["output"]


@pytest.mark.asyncio
async def test_terminal_exec_readonly_blocks_command_chaining(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """C-01: a read-only prefix must not smuggle a mutating tail past the gate.
    `ls; rm -rf ~` used to pass because the readonly prefix matched and the
    blocked tail ran under shell=True — chaining is now forbidden outright."""
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path))
    for evil in ("ls; rm -rf ~", "ls && rm -rf ~", "cat x > /tmp/evil", "echo $(rm x)", "ls | rm x"):
        r = await call_tool("terminal_exec", {"command": evil})
        assert r["kind"] == "error", f"chained command escaped the gate: {evil}"
        assert "blocked" in r["output"].lower() or "chaining" in r["output"].lower()


@pytest.mark.asyncio
async def test_unknown_tool_returns_error():
    r = await call_tool("not_a_tool", {})
    assert r["kind"] == "error"


# ----------------------------------------------------------- State + prompt assembly

def test_budget_would_exceed():
    b = Budget(used=4.5, cap=5.0)
    assert b.would_exceed(0.6) is True
    assert b.would_exceed(0.4) is False
    assert b.remaining() == 0.5


def test_tag_message_sets_origin():
    msg = HumanMessage(content="hi")
    tagged = tag_message(msg, PromptOrigin.USER)  # type: ignore[arg-type]
    assert tagged.additional_kwargs["prompt_origin"] == "user"


def test_model_capabilities_registry():
    assert get_capabilities("kimi-foundry").reasoning is True
    assert get_capabilities("qwen-foundry").reasoning is False
    # K2.6 has fixed parameters — temperature must not be sent.
    assert get_capabilities("kimi-foundry").supports_temperature is False
    assert get_capabilities("qwen-foundry").supports_temperature is True
    # Unknown model gets the conservative default
    assert get_capabilities("unknown").supports_tools is True
    assert get_capabilities("unknown").supports_temperature is True


# ----------------------------------------------- reasoning_content surfacing

def test_from_assistant_surfaces_reasoning_content_as_thinking():
    """OpenAI-compatible reasoning models (Kimi-K2, DeepSeek-R1, …) return their
    chain-of-thought in the non-standard ``reasoning_content`` field, which
    ChatOpenAIReasoning preserves into additional_kwargs. The emitter must
    surface it as a THINKING event BEFORE the message text."""
    em = EventEmitter("run-1", "thread-1")
    ai = AIMessage(
        content="The answer is 42.",
        additional_kwargs={"reasoning_content": "Let me work through this step by step."},
    )
    events = em.from_assistant(ai, "task-1")
    # thinking first, then the message
    assert len(events) == 2
    assert events[0].kind == StepKind.THINKING
    assert events[0].detail["text"] == "Let me work through this step by step."
    assert events[1].kind == StepKind.MESSAGE
    assert events[1].detail["text"] == "The answer is 42."


def test_from_assistant_reasoning_content_redacted():
    """Reasoning content is redacted at the event boundary like all other
    text — a secret in the chain-of-thought must not leak verbatim."""
    em = EventEmitter("run-1", "thread-1")
    ai = AIMessage(
        content="done",
        additional_kwargs={"reasoning_content": "The token is sk-1234567890abcdefghij"},
    )
    events = em.from_assistant(ai, "task-1")
    thinking = events[0]
    assert thinking.kind == StepKind.THINKING
    assert "sk-1234567890abcdefghij" not in thinking.detail["text"]
    assert "REDACTED" in thinking.detail["text"]


def test_from_assistant_no_reasoning_content_no_thinking_event():
    """A plain AIMessage without reasoning_content must not produce a spurious
    empty THINKING event — the thinking path is opt-in on the field's presence."""
    em = EventEmitter("run-1", "thread-1")
    ai = AIMessage(content="just a normal reply")
    events = em.from_assistant(ai, "task-1")
    assert len(events) == 1
    assert events[0].kind == StepKind.MESSAGE
    assert all(e.kind != StepKind.THINKING for e in events)


def test_make_llm_uses_reasoning_subclass_for_reasoning_models():
    """make_llm must return ChatOpenAIReasoning for reasoning-capable models
    (so reasoning_content is preserved) and stock ChatOpenAI otherwise."""
    import os
    from unittest.mock import patch

    from langchain_openai import ChatOpenAI

    from worker.engine.llm import ChatOpenAIReasoning, make_llm

    with patch.dict(os.environ, {"LITELLM_BASE_URL": "http://gw", "LITELLM_API_KEY": "k"}):
        reasoning_llm = make_llm("kimi-foundry", streaming=False)
        plain_llm = make_llm("qwen-foundry", streaming=False)
    assert isinstance(reasoning_llm, ChatOpenAIReasoning)
    assert isinstance(plain_llm, ChatOpenAI)
    assert not isinstance(plain_llm, ChatOpenAIReasoning)


def test_make_llm_omits_temperature_for_fixed_param_models():
    """K2.6 has fixed parameters — passing a non-default temperature makes
    Azure Foundry 400. make_llm must omit the param entirely for models flagged
    supports_temperature=False, and include it for models that accept it."""
    import os
    from unittest.mock import patch

    from worker.engine.llm import make_llm

    with patch.dict(os.environ, {"LITELLM_BASE_URL": "http://gw", "LITELLM_API_KEY": "k"}):
        kimi_llm = make_llm("kimi-foundry", streaming=False, temperature=0.0)
        qwen_llm = make_llm("qwen-foundry", streaming=False, temperature=0.0)
    # K2.6: temperature must NOT be set (ChatOpenAI defaults to 0.7 when unset)
    assert kimi_llm.temperature != 0.0
    # Qwen: temperature IS set to the requested value
    assert qwen_llm.temperature == 0.0


def test_make_llm_reasoning_maps_to_extra_body():
    """The composer's reasoning choice becomes extra_body: "off" disables
    thinking, an effort enables it with reasoning_effort, and None sends NO
    override (byte-identical to pre-feature traffic — provider default)."""
    import os
    from unittest.mock import patch

    from worker.engine.llm import make_llm

    with patch.dict(os.environ, {"LITELLM_BASE_URL": "http://gw", "LITELLM_API_KEY": "k"}):
        off = make_llm("kimi-foundry", streaming=False, reasoning="off")
        effort = make_llm("glm-foundry", streaming=False, reasoning="max")
        default = make_llm("glm-foundry", streaming=False)
    assert off.extra_body == {"thinking": {"type": "disabled"}}
    assert effort.extra_body == {
        "thinking": {"type": "enabled"}, "reasoning_effort": "max"}
    assert default.extra_body is None


def test_make_llm_reasoning_rejects_effort_the_model_lacks():
    """Fail-closed at the model edge too: an effort the deployment doesn't
    take raises instead of silently clamping (mirrors the backend registry)."""
    import os
    from unittest.mock import patch

    import pytest

    from worker.engine.llm import make_llm

    with patch.dict(os.environ, {"LITELLM_BASE_URL": "http://gw", "LITELLM_API_KEY": "k"}):
        with pytest.raises(RuntimeError, match="not 'xhigh'"):
            make_llm("kimi-foundry", streaming=False, reasoning="xhigh")
        with pytest.raises(RuntimeError, match="not 'low'"):
            make_llm("glm-foundry", streaming=False, reasoning="low")


def test_tool_arg_aliases_normalized_at_model_edge():
    """Models habitually call terminal_exec with cmd= (other harnesses' schema)
    instead of the bound schema's command=. Without normalization the approval
    card shows a raw JSON dump, `_tool_title` renders "$ ", permission glob
    rules miss, and execution fails with "empty command". Values stay verbatim;
    unknown extra keys pass through."""
    msg = AIMessage(content="", tool_calls=[{
        "id": "tc1", "name": "terminal_exec",
        "args": {"cmd": "pwd && ls -la", "timeout_ms": 30000},
    }])
    _normalize_tool_call_args(msg)
    assert msg.tool_calls[0]["args"] == {
        "command": "pwd && ls -la", "timeout_ms": 30000,
    }
    assert _tool_title("terminal_exec", msg.tool_calls[0]["args"]) == "$ pwd && ls -la"

    # The canonical key already present wins; the alias is left alone.
    msg2 = AIMessage(content="", tool_calls=[{
        "id": "tc2", "name": "terminal_exec",
        "args": {"command": "ls", "cmd": "should-not-win"},
    }])
    _normalize_tool_call_args(msg2)
    assert msg2.tool_calls[0]["args"]["command"] == "ls"

    # Other tools are untouched.
    msg3 = AIMessage(content="", tool_calls=[{
        "id": "tc3", "name": "file_read", "args": {"cmd": "x"},
    }])
    _normalize_tool_call_args(msg3)
    assert msg3.tool_calls[0]["args"] == {"cmd": "x"}
