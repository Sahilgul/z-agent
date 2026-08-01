"""Tests for the citation linter (zagent_maps.lint)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from zagent_maps.lint import (
    LintReport,
    lint,
    lint_citation,
    load_citations_file,
    main as lint_main,
    parse_citation,
    render_report,
)


@pytest.fixture()
def small_repo(tmp_path):
    (tmp_path / "mod.py").write_text(
        '"""Mod."""\n\nclass C:\n    pass\n\ndef f(a, b):\n    return a + b\n',
        encoding="utf-8",
    )
    (tmp_path / "empty_lines.py").write_text(
        "first\n\nthird\n", encoding="utf-8",
    )
    (tmp_path / "bin.dat").write_bytes(b"\xff\xff\xfe invalid utf8 \xff")
    return tmp_path


# ---------------------------------------------------------------------------
# parse_citation
# ---------------------------------------------------------------------------

def test_parse_single_line():
    c = parse_citation("mod.py:3")
    assert c.path == "mod.py"
    assert c.line_start == 3 and c.line_end == 3


def test_parse_line_range():
    c = parse_citation("mod.py:3-5")
    assert c.line_start == 3 and c.line_end == 5


def test_parse_windows_drive_path():
    c = parse_citation(r"C:\foo\bar.py:12")
    assert c.path == r"C:\foo\bar.py"
    assert c.line_start == 12


def test_parse_rejects_malformed():
    for bad in ["", "mod.py", "mod.py:", ":3", "mod.py:abc", "mod.py:3-", "mod.py:5-3",
               "mod.py:0", "mod.py:-1", "   "]:
        assert parse_citation(bad) is None, f"expected None for {bad!r}"


def test_parse_strips_whitespace():
    c = parse_citation("  mod.py:3  ")
    assert c is not None and c.path == "mod.py" and c.line_start == 3


# ---------------------------------------------------------------------------
# lint_citation
# ---------------------------------------------------------------------------

def test_lint_valid_citation(small_repo):
    c = parse_citation("mod.py:3")
    r = lint_citation(small_repo, c)
    assert r.ok is True
    assert r.error == ""


def test_lint_valid_range(small_repo):
    c = parse_citation("mod.py:3-6")
    r = lint_citation(small_repo, c)
    assert r.ok is True


def test_lint_file_not_found(small_repo):
    c = parse_citation("missing.py:1")
    r = lint_citation(small_repo, c)
    assert r.ok is False and r.error == "file_not_found"


def test_lint_line_out_of_range(small_repo):
    c = parse_citation("mod.py:999")
    r = lint_citation(small_repo, c)
    assert r.ok is False and r.error == "line_out_of_range"
    assert "file has" in r.detail


def test_lint_empty_line(small_repo):
    c = parse_citation("empty_lines.py:2")
    r = lint_citation(small_repo, c)
    assert r.ok is False and r.error == "empty_line"


def test_lint_unreadable_binary_file(small_repo):
    c = parse_citation("bin.dat:1")
    r = lint_citation(small_repo, c)
    assert r.ok is False and r.error == "unreadable_file"


def test_lint_directory_not_a_file(small_repo):
    (small_repo / "dir").mkdir()
    c = parse_citation("dir:1")
    r = lint_citation(small_repo, c)
    assert r.ok is False and r.error == "not_a_file"


# ---------------------------------------------------------------------------
# lint (batch)
# ---------------------------------------------------------------------------

def test_lint_batch_counts(small_repo):
    cits = ["mod.py:3", "mod.py:999", "missing.py:1", "mod.py:abc", "empty_lines.py:2"]
    report = lint(str(small_repo), cits)
    assert isinstance(report, LintReport)
    assert report.total == 5
    assert report.valid == 1
    assert report.invalid == 4
    assert report.passed is False
    assert len(report.results) == 5
    assert report.results[0].ok is True
    assert report.results[1].error == "line_out_of_range"
    assert report.results[2].error == "file_not_found"
    assert report.results[3].error == "malformed_citation"
    assert report.results[4].error == "empty_line"


def test_lint_all_valid_passes(small_repo):
    report = lint(str(small_repo), ["mod.py:3", "mod.py:4", "mod.py:6"])
    assert report.valid == 3 and report.invalid == 0
    assert report.passed is True


def test_lint_as_dict_shape(small_repo):
    report = lint(str(small_repo), ["mod.py:3"])
    d = report.as_dict()
    assert d["total"] == 1 and d["valid"] == 1 and d["invalid"] == 0
    assert d["passed"] is True
    assert d["results"][0]["citation"] == "mod.py:3"
    assert d["results"][0]["ok"] is True


# ---------------------------------------------------------------------------
# load_citations_file
# ---------------------------------------------------------------------------

def test_load_citations_array(tmp_path):
    f = tmp_path / "c.json"
    f.write_text(json.dumps(["a.py:1", "b.py:2-3"]), encoding="utf-8")
    assert load_citations_file(str(f)) == ["a.py:1", "b.py:2-3"]


def test_load_citations_object(tmp_path):
    f = tmp_path / "c.json"
    f.write_text(json.dumps({"citations": ["a.py:1"]}), encoding="utf-8")
    assert load_citations_file(str(f)) == ["a.py:1"]


def test_load_citations_bad_shape(tmp_path):
    f = tmp_path / "c.json"
    f.write_text(json.dumps({"nope": 1}), encoding="utf-8")
    with pytest.raises(ValueError):
        load_citations_file(str(f))


# ---------------------------------------------------------------------------
# render_report
# ---------------------------------------------------------------------------

def test_render_report_lists_invalid(small_repo):
    report = lint(str(small_repo), ["mod.py:3", "missing.py:1"])
    out = render_report(report)
    assert "citation lint report" in out
    assert "passed=False" in out
    assert "OK " in out and "XX " in out


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def test_lint_cli_exit_zero_when_all_valid(small_repo, tmp_path):
    cits = tmp_path / "cits.json"
    cits.write_text(json.dumps(["mod.py:3"]), encoding="utf-8")
    rc = lint_main([str(small_repo), str(cits)])
    assert rc == 0


def test_lint_cli_exit_nonzero_on_invalid(small_repo, tmp_path, capsys):
    cits = tmp_path / "cits.json"
    cits.write_text(json.dumps(["mod.py:999", "missing.py:1"]), encoding="utf-8")
    rc = lint_main([str(small_repo), str(cits)])
    assert rc == 1
    out = capsys.readouterr().out
    assert "invalid=2" in out


def test_lint_cli_json_output(small_repo, tmp_path, capsys):
    cits = tmp_path / "cits.json"
    cits.write_text(json.dumps(["mod.py:3"]), encoding="utf-8")
    rc = lint_main([str(small_repo), str(cits), "--json"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["valid"] == 1
