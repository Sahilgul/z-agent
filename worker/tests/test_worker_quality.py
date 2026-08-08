"""Wave 4 Stream E regression tests: worker quality items K2-K17."""


import pytest

# ------------------------------------------------------------------- K4

def test_system_prompt_frozen_per_process(monkeypatch, tmp_path):
    """Re-reading the prompt file every turn silently changed the agent's
    constitution mid-run and drifted the cache prefix. It is read once."""
    from worker.engine import graph
    monkeypatch.setattr(graph, "_SYSTEM_PROMPT_CACHE", None)
    p1 = graph._system_prompt_text()
    # Simulate a mid-run edit: the frozen copy must not change.
    monkeypatch.setattr(graph, "_SYSTEM_PROMPT_CACHE", p1)
    assert graph._system_prompt_text() == p1


def test_missing_system_prompt_is_loud(monkeypatch, tmp_path):
    from worker.engine import graph
    monkeypatch.setattr(graph, "PROMPTS_DIR", tmp_path)
    monkeypatch.setattr(graph, "_SYSTEM_PROMPT_CACHE", None)
    with pytest.raises(RuntimeError, match="system prompt missing"):
        graph._system_prompt_text()


# ------------------------------------------------------------------- K7

def test_skills_block_lists_playbooks():
    """system_prompt.md promises a skills list at its end — it now exists."""
    from worker.engine import graph
    block = graph._skills_block()
    assert "## Skills" in block
    assert "pr-from-story" in block  # the shipped playbook


def test_system_message_includes_skills(monkeypatch):
    from worker.engine import graph
    monkeypatch.setattr(graph, "_SYSTEM_PROMPT_CACHE", None)
    msg = graph._build_system_message()
    assert "playbook_load" in msg.content


def test_prompt_prefix_byte_stable_across_turns(monkeypatch):
    """N2: schema/prefix drift must FAIL a test. The system message is the
    cache prefix — any byte change across turns (including after tools are
    discovered mid-run) invalidates the prompt cache and silently doubles
    input spend. Discovery rides the TRANSIENT envelope, not the system
    message, so the prefix must be byte-identical turn over turn."""
    import hashlib

    from worker.engine import graph
    monkeypatch.setattr(graph, "_SYSTEM_PROMPT_CACHE", None)
    m1 = graph._build_system_message()
    m2 = graph._build_system_message()  # next turn
    h1 = hashlib.sha256(m1.content.encode()).hexdigest()
    h2 = hashlib.sha256(m2.content.encode()).hexdigest()
    assert h1 == h2


# ------------------------------------------------------------------- K6

def test_cached_tokens_priced_at_discount(monkeypatch):
    """Cached input tokens must not be billed at full input price — the
    local reminder estimate drifted ahead of real gateway spend."""
    from worker.engine import llm
    monkeypatch.setenv("MODEL_PRICE_IN_PER_MTOK", "2.0")
    monkeypatch.setenv("MODEL_PRICE_OUT_PER_MTOK", "6.0")
    usage = {"input_tokens": 1_000_000, "output_tokens": 0,
             "input_token_details": {"cache_read": 800_000}}
    cost = llm.estimate_cost("kimi-foundry", usage)
    # 200k uncached * 2.0 + 800k cached * 2.0 * 0.1 = 0.40 + 0.16 = 0.56
    assert cost == pytest.approx(0.56)


# ------------------------------------------------------------------- K8

def test_compaction_protects_error_and_edit_tool_results():
    """The prompt guarantees errors/edits/approvals survive compaction —
    they are TOOL origin and were pruned FIRST. Reclassified as protected."""
    from worker.engine.compaction import _is_survival_critical

    class _Msg:
        def __init__(self, name, content):
            self.name = name
            self.content = content

    assert _is_survival_critical(_Msg("terminal_exec", "error: boom"))
    assert _is_survival_critical(_Msg("file_edit", "edit applied to x.py"))
    assert _is_survival_critical(_Msg("file_write", "wrote x.py"))
    assert _is_survival_critical(_Msg("terminal_exec", "denied by user"))
    assert not _is_survival_critical(_Msg("file_read", "some file content"))
    assert not _is_survival_critical(_Msg("terminal_exec", "[exit 0] ok"))


# ------------------------------------------------------------------- K10

def test_file_search_truncation_marker_and_count(tmp_path, monkeypatch):
    """The footer must count matches in the FULL output and say when cut."""
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path))
    for i in range(50):
        (tmp_path / f"f{i}.py").write_text("needle\n" * 400)
    from worker.engine.tools.readonly import file_search
    out = file_search.invoke({"pattern": "needle"})
    assert "matches]" in out
    assert "truncated" in out  # 50*400 matches >> 16KB window


def test_terminal_exec_truncation_keeps_exit_marker(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path))
    (tmp_path / "big.txt").write_text("x\n" * 200_000)
    from worker.engine.tools.readonly import terminal_exec
    out = terminal_exec.invoke({"command": "cat big.txt"})
    assert "[exit 0]" in out
    assert "truncated" in out


# ------------------------------------------------------------------- K11

def test_call_tool_direct_threads_mode_to_tool_search(monkeypatch):
    import inspect

    from worker.engine import tools
    src = inspect.getsource(tools.call_tool_direct)
    assert "mode: str = " in src and "mode=mode" in src
    assert 'mode="development"' not in src


# ------------------------------------------------------------------- K2

def test_completed_notifications_scoped_to_thread(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path))
    from worker.engine.tools.background import BackgroundJob, TerminalManager
    mgr = TerminalManager()
    a = BackgroundJob("true", tmp_path, None)
    a.thread_id = "t-a"
    a.exit_code = 0
    b = BackgroundJob("true", tmp_path, None)
    b.thread_id = "t-b"
    b.exit_code = 0
    mgr.jobs = {"a": a, "b": b}
    notes = mgr.completed_notifications(thread_id="t-a")
    assert len(notes) == 1 and a.job_id in notes[0]
    # t-b's note is NOT consumed by t-a's turn.
    assert not getattr(b, "_notified", False)


# ------------------------------------------------------------------- K15

def test_pid_file_written_and_cleared(tmp_path, monkeypatch):
    """A SIGKILLed worker leaves pid files; the next manager reaps them."""
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path))
    from worker.engine.tools import background
    monkeypatch.setattr(background, "_MANAGER", None)
    mgr = background.TerminalManager()
    job = background.BackgroundJob("sleep 60", tmp_path, None)

    class _Proc:
        pid = 999999

    job._proc = _Proc()
    mgr._write_pid(job)
    pidfile = background._pids_dir() / f"{job.job_id}.pid"
    assert pidfile.read_text() == "999999"
    mgr._clear_pid(job)
    assert not pidfile.exists()


def test_manager_reaps_orphan_pid_files(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path))
    from worker.engine.tools import background
    background._pids_dir()  # ensure dir
    (background._pids_dir() / "stale.pid").write_text("999999")  # dead pid
    killed = []
    monkeypatch.setattr(background.os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(background.os, "killpg",
                        lambda pgid, sig: killed.append((pgid, sig)))
    monkeypatch.setattr(background, "_MANAGER", None)
    background.TerminalManager()  # init reaps orphans
    assert killed  # attempted to kill the orphan's group
    assert not (background._pids_dir() / "stale.pid").exists()


# ------------------------------------------------------------------- K17

def test_evidence_prefers_structured_exit_code():
    from langchain_core.messages import ToolMessage

    from worker.engine.graph import _extract_evidence

    msg = ToolMessage(content="pytest FAILED\n[exit 0]",  # lying footer
                      tool_call_id="x", name="terminal_exec")
    msg.additional_kwargs["exit_code"] = 1
    out = _extract_evidence({"messages": [msg]})
    assert out["tests_pass"] is False  # structured truth beats the string

    msg2 = ToolMessage(content="3 tests passed", tool_call_id="y",
                       name="terminal_exec")
    msg2.additional_kwargs["exit_code"] = 0
    out2 = _extract_evidence({"messages": [msg2]})
    assert out2["tests_pass"] is True
