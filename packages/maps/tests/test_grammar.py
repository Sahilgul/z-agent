"""Tests for the canonical grammar module (collegium_maps.grammar)."""

from __future__ import annotations

import json

from collegium_maps import grammar as g
from collegium_maps.grammar import (
    SCHEMA_VERSION,
    Edge,
    HotFile,
    Module,
    RepoMap,
    Symbol,
    canonicalize,
    from_dict,
    render_markdown,
    to_dict,
    to_posix,
)


def test_schema_version_is_one():
    assert SCHEMA_VERSION == 1


def test_to_posix_normalizes_backslashes():
    assert to_posix(r"src\pkg\mod.py") == "src/pkg/mod.py"
    assert to_posix("src/pkg/mod.py") == "src/pkg/mod.py"


def test_canonicalize_sorts_all_lists_idempotently():
    m = RepoMap(
        modules=[
            Module(path="b", language="python", file_count=1, purpose=""),
            Module(path="a", language="python", file_count=2, purpose=""),
        ],
        symbols=[
            Symbol(module="a", file="a/z.py", name="z", kind="function", signature="def z()", line=5),
            Symbol(module="a", file="a/a.py", name="a", kind="class", signature="class a", line=1),
            Symbol(module="a", file="a/a.py", name="a", kind="function", signature="def a()", line=1),
        ],
        edges=[
            Edge(from_="b", to="a", kind="import"),
            Edge(from_="a", to="b", kind="import"),
        ],
        hot_files=[
            HotFile(file="x.py", importers=1),
            HotFile(file="y.py", importers=5),
            HotFile(file="z.py", importers=5),
        ],
        entrypoints=["b.py", "a.py"],
        tests=["t2.py", "t1.py"],
    )
    c = canonicalize(m)
    assert [mod.path for mod in c.modules] == ["a", "b"]
    assert [(s.file, s.line, s.name, s.kind) for s in c.symbols] == [
        ("a/a.py", 1, "a", "class"),
        ("a/a.py", 1, "a", "function"),
        ("a/z.py", 5, "z", "function"),
    ]
    assert [(e.from_, e.to) for e in c.edges] == [("a", "b"), ("b", "a")]
    # hot files ranked by importers desc, then file asc
    assert [(h.file, h.importers) for h in c.hot_files] == [
        ("y.py", 5), ("z.py", 5), ("x.py", 1),
    ]
    assert c.entrypoints == ["a.py", "b.py"]
    assert c.tests == ["t1.py", "t2.py"]
    # Idempotent: re-canonicalizing yields an equal dict
    assert to_dict(canonicalize(c)) == to_dict(c)


def test_to_dict_preserves_canonical_key_order():
    m = RepoMap(generated_at="T", repo="r")
    d = to_dict(m)
    assert list(d.keys()) == [
        "version", "generated_at", "repo", "modules", "symbols",
        "edges", "hot_files", "entrypoints", "tests",
    ]
    assert d["version"] == SCHEMA_VERSION


def test_to_dict_renames_from_field_on_edge():
    m = RepoMap(edges=[Edge(from_="a", to="b", kind="import")])
    d = to_dict(m)
    assert d["edges"] == [{"from": "a", "to": "b", "kind": "import"}]
    assert "from_" not in d["edges"][0]


def test_from_dict_roundtrip():
    m = RepoMap(
        generated_at="2026-01-01T00:00:00Z",
        repo="r",
        modules=[Module(path="a", language="python", file_count=1, purpose="p")],
        symbols=[Symbol(module="a", file="a/x.py", name="X", kind="class",
                        signature="class X", line=3)],
        edges=[Edge(from_="a", to="b", kind="import")],
        hot_files=[HotFile(file="a/x.py", importers=2)],
        entrypoints=["a/main.py"],
        tests=["a/test_x.py"],
    )
    d = to_dict(canonicalize(m))
    # JSON roundtrip preserves shape and key order
    s = json.dumps(d)
    d2 = json.loads(s)
    m2 = from_dict(d2)
    assert to_dict(canonicalize(m2)) == d


def test_from_dict_tolerant_of_missing_optional_fields():
    m = from_dict({"version": 1, "repo": "r"})
    assert m.modules == [] and m.symbols == [] and m.edges == []
    assert m.hot_files == [] and m.entrypoints == [] and m.tests == []
    assert m.generated_at == ""


def test_render_markdown_is_deterministic_and_has_sections():
    m = canonicalize(RepoMap(
        generated_at="2026-01-01T00:00:00Z", repo="r",
        modules=[Module(path=".", language="python", file_count=1, purpose="root")],
        entrypoints=["main.py"],
        symbols=[Symbol(module=".", file="main.py", name="main",
                        kind="function", signature="def main()", line=1)],
        edges=[Edge(from_=".", to="pkg", kind="import")],
        hot_files=[HotFile(file="pkg/x.py", importers=3)],
        tests=["tests/t.py"],
    ))
    out = render_markdown(m)
    assert out.startswith("# r — repo map\n")
    for section in ["## Modules", "## Entrypoints", "## Symbols",
                    "## Edges", "## Hot files", "## Tests"]:
        assert section in out
    assert "`pkg/x.py` (3 importers)" in out
    # Re-rendering the same map yields identical output
    assert render_markdown(m) == out


def test_render_markdown_handles_empty_sections():
    m = canonicalize(RepoMap(repo="r"))
    out = render_markdown(m)
    assert "- _(none detected)_" in out


def test_sort_key_helpers_are_stable():
    assert g.module_sort_key(Module(path="a", language="python", file_count=1, purpose="")) == ("a",)
    assert g.symbol_sort_key(Symbol(module="m", file="f", name="n", kind="k",
                                    signature="s", line=1)) == ("f", 1, "n", "k")
    assert g.edge_sort_key(Edge(from_="a", to="b", kind="import")) == ("a", "b", "import")
    assert g.hot_file_sort_key(HotFile(file="a.py", importers=3)) == (-3, "a.py")
