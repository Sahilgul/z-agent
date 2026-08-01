"""Tests for the Python repo-map generator (zagent_maps.generate)."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import pytest

from zagent_maps import to_dict
from zagent_maps.generate import main as gen_main
from zagent_maps.generate import (
    GenerateReport,
    _collect_local_imports,
    _is_test_file,
    _resolve_dotted,
    _signature_for_class,
    _signature_for_function,
    analyze_file,
    build_module_index,
    canonicalize,
    dir_module,
    generate,
    iter_source_files,
    rel_posix,
    write_map,
)
from zagent_maps.grammar import RepoMap

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "python_repo"
GOLDEN = Path(__file__).resolve().parent / "fixtures" / "expected" / "python_repo_map.json"
PINNED_AT = "2026-01-01T00:00:00Z"


# ---------------------------------------------------------------------------
# Golden-fixture parity test (byte-for-byte)
# ---------------------------------------------------------------------------

def test_generate_matches_golden_byte_for_byte():
    repo_map, report = generate(str(FIXTURE), generated_at=PINNED_AT)
    actual = json.dumps(to_dict(repo_map), indent=2, ensure_ascii=False) + "\n"
    expected = GOLDEN.read_text(encoding="utf-8")
    assert actual == expected, (
        "Generated map.json diverged from the committed golden fixture. "
        "Re-run packages/maps/scripts/gen_golden_py.py if the change is intended."
    )


def test_generate_report_stats_for_fixture():
    _repo_map, report = generate(str(FIXTURE), generated_at=PINNED_AT)
    assert isinstance(report, GenerateReport)
    assert report.files_scanned == 9
    assert report.files_skipped == 1
    assert report.crashes == ["broken.py"]
    assert report.crash_count == 1
    assert report.modules_count == 3
    assert report.symbols_count == 7
    assert report.edges_count == 2
    top = [(h.file, h.importers) for h in report.hot_files_top]
    assert top[0] == ("pkg/core.py", 2)


def test_generate_is_deterministic_across_runs():
    m1, _ = generate(str(FIXTURE), generated_at=PINNED_AT)
    m2, _ = generate(str(FIXTURE), generated_at=PINNED_AT)
    assert to_dict(m1) == to_dict(m2)


def test_generated_at_defaults_to_iso_when_not_pinned():
    m, _ = generate(str(FIXTURE))
    assert m.generated_at.endswith("Z")
    assert "T" in m.generated_at


def test_repo_name_is_repo_basename():
    m, _ = generate(str(FIXTURE), generated_at=PINNED_AT)
    assert m.repo == "python_repo"


# ---------------------------------------------------------------------------
# Crash handling — never crash a run on one bad file
# ---------------------------------------------------------------------------

def test_syntax_error_file_recorded_as_crash_not_raised(tmp_path):
    (tmp_path / "good.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    (tmp_path / "bad.py").write_text("def (\n", encoding="utf-8")
    repo_map, report = generate(str(tmp_path), generated_at=PINNED_AT)
    assert report.crash_count == 1
    assert "bad.py" in report.crashes
    # The good file still produced a symbol
    assert any(s.name == "f" for s in repo_map.symbols)


def test_unreadable_binary_file_recorded_as_crash(tmp_path):
    (tmp_path / "good.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    (tmp_path / "bin.py").write_bytes(b"\x00\x01\x02 not text \x00")
    repo_map, report = generate(str(tmp_path), generated_at=PINNED_AT)
    assert "bin.py" in report.crashes
    assert any(s.name == "f" for s in repo_map.symbols)


def test_empty_repo_produces_empty_map(tmp_path):
    repo_map, report = generate(str(tmp_path), generated_at=PINNED_AT)
    assert repo_map.modules == []
    assert repo_map.symbols == []
    assert repo_map.edges == []
    assert repo_map.hot_files == []
    assert repo_map.entrypoints == []
    assert repo_map.tests == []
    assert report.files_scanned == 0
    assert report.crash_count == 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def test_iter_source_files_skips_ignored_dirs(tmp_path):
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "skip.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "skip.pyc").write_text("x", encoding="utf-8")
    (tmp_path / "keep.py").write_text("x = 1\n", encoding="utf-8")
    files = [rel_posix(tmp_path, f) for f in iter_source_files(tmp_path)]
    assert files == ["keep.py"]


def test_rel_posix_and_dir_module(tmp_path):
    nested = tmp_path / "pkg" / "sub" / "mod.py"
    nested.parent.mkdir(parents=True)
    nested.write_text("x = 1\n", encoding="utf-8")
    root_file = tmp_path / "root.py"
    root_file.write_text("x = 1\n", encoding="utf-8")
    assert rel_posix(tmp_path, nested) == "pkg/sub/mod.py"
    assert dir_module(tmp_path, nested) == "pkg/sub"
    assert dir_module(tmp_path, root_file) == "."


def test_signature_for_function_and_class():
    import ast
    tree = ast.parse("def f(a, b, *, c=1, **kw):\n    pass\nclass C(A, B):\n    pass\n")
    fn = tree.body[0]
    cls = tree.body[1]
    assert _signature_for_function(fn).startswith("def f(")
    assert "c" in _signature_for_function(fn)
    assert _signature_for_class(cls) == "class C(A, B)"
    # class with no bases
    cls2 = ast.parse("class D:\n    pass\n").body[0]
    assert _signature_for_class(cls2) == "class D"


def test_build_module_index_keys_dotted_names(tmp_path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "mod.py").write_text("x = 1\n", encoding="utf-8")
    files = list(iter_source_files(tmp_path))
    idx = build_module_index(tmp_path, files)
    assert idx["pkg"] == "pkg/__init__.py"
    assert idx["pkg.mod"] == "pkg/mod.py"


def test_resolve_dotted_exact_and_stripped():
    index = {"pkg.mod": "pkg/mod.py", "pkg": "pkg/__init__.py"}
    assert _resolve_dotted("pkg.mod", index) == "pkg/mod.py"
    assert _resolve_dotted("pkg.mod.attr", index) == "pkg/mod.py"
    assert _resolve_dotted("repo.pkg.mod", index) == "pkg/mod.py"  # leading-strip
    assert _resolve_dotted("nonexistent", index) is None
    assert _resolve_dotted("", index) is None


def test_collect_local_imports_relative_and_absolute(tmp_path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "core.py").write_text("x = 1\n", encoding="utf-8")
    (pkg / "api.py").write_text(
        "from pkg.core import x\nfrom . import core\nimport os\n",
        encoding="utf-8",
    )
    files = list(iter_source_files(tmp_path))
    idx = build_module_index(tmp_path, files)
    api = pkg / "api.py"
    import ast
    tree = ast.parse(api.read_text(encoding="utf-8"))
    resolved = _collect_local_imports(tree, tmp_path, api, idx)
    assert "pkg/core.py" in resolved


def test_is_test_file_detection():
    assert _is_test_file("tests/test_core.py")
    assert _is_test_file("pkg/test_thing.py")
    assert _is_test_file("pkg/thing_test.py")
    assert not _is_test_file("pkg/core.py")
    assert not _is_test_file("tests/__init__.py")


def test_analyze_file_returns_none_for_syntax_error(tmp_path):
    bad = tmp_path / "bad.py"
    bad.write_text("def (\n", encoding="utf-8")
    assert analyze_file(tmp_path, bad, {}) is None


def test_analyze_file_extracts_public_symbols_only(tmp_path):
    f = tmp_path / "mod.py"
    f.write_text(
        '"""Mod."""\n\n'
        'def _private():\n    pass\n\n'
        'def public(a, b):\n    pass\n\n'
        'class _Hidden:\n    pass\n\n'
        'class Visible(Base):\n    pass\n',
        encoding="utf-8",
    )
    a = analyze_file(tmp_path, f, {})
    assert a is not None
    names = {s.name for s in a.symbols}
    assert names == {"public", "Visible"}
    kinds = {s.name: s.kind for s in a.symbols}
    assert kinds == {"public": "function", "Visible": "class"}
    assert a.docstring == "Mod."


# ---------------------------------------------------------------------------
# write_map
# ---------------------------------------------------------------------------

def test_write_map_writes_json_and_md(tmp_path):
    (tmp_path / "main.py").write_text("def main():\n    pass\n", encoding="utf-8")
    repo_map, _ = generate(str(tmp_path), generated_at=PINNED_AT)
    jp, mp = write_map(repo_map, str(tmp_path), ".zagent")
    assert jp.exists() and mp.exists()
    data = json.loads(jp.read_text(encoding="utf-8"))
    assert data["repo"] == tmp_path.name
    md = mp.read_text(encoding="utf-8")
    assert md.startswith(f"# {tmp_path.name} — repo map\n")


def test_write_map_creates_out_dir(tmp_path):
    (tmp_path / "main.py").write_text("x = 1\n", encoding="utf-8")
    repo_map, _ = generate(str(tmp_path), generated_at=PINNED_AT)
    jp, _ = write_map(repo_map, str(tmp_path), ".zagent")
    assert jp.parent.exists()
    assert jp.parent.name == ".zagent"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def test_generate_cli_writes_files(tmp_path, monkeypatch, capsys):
    (tmp_path / "main.py").write_text("def main():\n    pass\n", encoding="utf-8")
    rc = gen_main([str(tmp_path), "--report"])
    assert rc == 0
    assert (tmp_path / ".zagent" / "map.json").exists()
    assert (tmp_path / ".zagent" / "map.md").exists()
    err = capsys.readouterr().err
    assert "files_scanned" in err


def test_generate_cli_custom_out_dir(tmp_path):
    (tmp_path / "main.py").write_text("x = 1\n", encoding="utf-8")
    rc = gen_main([str(tmp_path), "--out", "mapout"])
    assert rc == 0
    assert (tmp_path / "mapout" / "map.json").exists()
