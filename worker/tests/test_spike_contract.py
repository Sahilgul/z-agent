"""Contract tests for the spike (highest leverage).

These do NOT hit the gateway (no live LLM). They validate:
- The spike tool-result taxonomy (kind: success|error) is consistent.
- The gate-evaluation logic maps results to a–g verdicts correctly.
- The DECISION_MATRIX template renders without leaving placeholder stubs.

Gateway-dependent behavior (actual tool-call fidelity, streaming, caching,
interrupt/resume) is validated by RUNNING the matrix, not by these tests.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from spike.matrix import evaluate_gate, render_matrix
from spike.spike_tools import TOOL_BY_NAME, call_tool, looks_like_test

# ----------------------------------------------------------- tool taxonomy

@pytest.mark.asyncio
async def test_tool_result_taxonomy_success(tmp_path: Path) -> None:
    f = tmp_path / "x.txt"
    f.write_text("hello\nworld\n")
    r = await call_tool("file_read", {"file_path": str(f)})
    assert r["kind"] == "success"
    assert r["ok"] is True
    assert "hello" in r["output"]
    assert "lines" in r["output"]  # footer


@pytest.mark.asyncio
async def test_tool_result_taxonomy_error_missing_file() -> None:
    r = await call_tool("file_read", {"file_path": "/no/such/file"})
    assert r["kind"] == "error"
    assert r["ok"] is False
    assert r["output"].startswith("error:")


@pytest.mark.asyncio
async def test_tool_result_taxonomy_error_unknown_tool() -> None:
    r = await call_tool("not_a_tool", {})
    assert r["kind"] == "error"
    assert r["ok"] is False
    assert "unknown tool" in r["output"]


@pytest.mark.asyncio
async def test_file_edit_ambiguous_rejects(tmp_path: Path) -> None:
    f = tmp_path / "x.txt"
    f.write_text("dup\ndup\n")
    r = await call_tool("file_edit", {"file_path": str(f), "old_string": "dup", "new_string": "x"})
    assert r["kind"] == "error"
    assert "appears 2 times" in r["output"]


@pytest.mark.asyncio
async def test_bash_success_and_exit(tmp_path: Path) -> None:
    r = await call_tool("bash", {"command": "echo hi"})
    assert r["kind"] == "success"
    assert "hi" in r["output"]
    assert "exit 0" in r["output"]


@pytest.mark.asyncio
async def test_bash_empty_command_rejected() -> None:
    r = await call_tool("bash", {"command": "   "})
    assert r["kind"] == "error"


def test_tool_registry_has_three_tools() -> None:
    assert set(TOOL_BY_NAME) == {"file_read", "file_edit", "bash"}


def test_tracer_normalizer_kwargs_fixed() -> None:
    """C-06: the lane->thread rename left a stale `lane_id=` kwarg in the
    tracer's Normalizer construction, raising TypeError and killing the
    whole tracer CLI. Verify the source now passes `thread_id=` (which
    Normalizer.__init__ accepts), not the stale `lane_id=`. Done as a
    source check because tracer.py imports claude_agent_sdk (not installed
    in the test env), so it can't be imported here."""
    from pathlib import Path
    src = Path(__file__).resolve().parents[1] / "spike" / "tracer.py"
    text = src.read_text(encoding="utf-8")
    assert "lane_id=" not in text, "tracer still passes stale lane_id= to Normalizer"
    assert "thread_id=" in text, "tracer must pass thread_id= to Normalizer"


def test_looks_like_test_detection() -> None:
    assert looks_like_test("pytest tests/")
    assert looks_like_test("npm test")
    assert looks_like_test("vitest run")
    assert not looks_like_test("ls -la")
    assert not looks_like_test("git status")


# ----------------------------------------------------------- gate evaluation

def _passing_model_results() -> dict:
    return {
        "ask": {"tool_call_success_rate": 0.97, "first_delta_latency_s": 2.0, "usage": {"input_tokens": 10, "output_tokens": 5}},
        "structured": {"schema_validity_rate": 1.0},
        "soak": {"soak_turns_met": True},
        "cache": {"caching_survives": True},
        "interrupt": {"interrupt_resume_works": True},
    }


def test_gate_passes_when_two_models_pass_all() -> None:
    results = {"kimi-foundry": _passing_model_results(), "qwen-foundry": _passing_model_results()}
    g = evaluate_gate(results)
    assert g["gate_passed"] is True
    assert set(g["passing_models"]) == {"kimi-foundry", "qwen-foundry"}


def test_gate_fails_when_only_one_model_passes() -> None:
    results = {"kimi-foundry": _passing_model_results(), "qwen-foundry": {
        **_passing_model_results(), "cache": {"caching_survives": False}
    }}
    g = evaluate_gate(results)
    assert g["gate_passed"] is False
    assert g["passing_models"] == ["kimi-foundry"]


def test_gate_fails_when_tool_fidelity_below_threshold() -> None:
    r = _passing_model_results()
    r["ask"]["tool_call_success_rate"] = 0.80  # below 0.95
    results = {"kimi-foundry": r, "qwen-foundry": _passing_model_results()}
    g = evaluate_gate(results)
    assert g["gate_passed"] is False
    assert g["model_verdicts"]["kimi-foundry"]["a"] is False


def test_gate_fails_when_soak_turns_not_met() -> None:
    r = _passing_model_results()
    r["soak"]["soak_turns_met"] = False
    results = {"kimi-foundry": r, "qwen-foundry": _passing_model_results()}
    g = evaluate_gate(results)
    assert g["model_verdicts"]["kimi-foundry"]["e"] is False
    assert g["gate_passed"] is False


def test_gate_fails_when_interrupt_resume_broken() -> None:
    r = _passing_model_results()
    r["interrupt"]["interrupt_resume_works"] = False
    results = {"kimi-foundry": r, "qwen-foundry": _passing_model_results()}
    g = evaluate_gate(results)
    assert g["model_verdicts"]["kimi-foundry"]["g"] is False
    assert g["gate_passed"] is False


# ----------------------------------------------------------- matrix rendering

def test_render_matrix_replaces_all_placeholders() -> None:
    results = {"kimi-foundry": _passing_model_results(), "qwen-foundry": _passing_model_results()}
    g = evaluate_gate(results)
    rendered = render_matrix(results, g)
    assert "{{GENERATED_AT}}" not in rendered
    assert "{{RESULTS_JSON}}" not in rendered
    assert "Verdicts" in rendered
    assert "kimi-foundry" in rendered
    assert "Gate passed:** True" in rendered


def test_render_matrix_includes_results_json() -> None:
    results = {"kimi-foundry": _passing_model_results()}
    g = evaluate_gate(results)
    rendered = render_matrix(results, g)
    # The results JSON is embedded — parse it back out of the fenced block.
    assert "kimi-foundry" in rendered
    assert "schema_validity_rate" in rendered
