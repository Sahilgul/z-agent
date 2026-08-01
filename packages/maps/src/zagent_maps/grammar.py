"""Canonical map grammar for Zagent's repo-map (layer 1 of the context system).

This module is the SINGLE source of truth for the shape of a ``map.json`` document.
Both generators — the Python generator in :mod:`zagent_maps.generate` (analyzes
Python repos via :mod:`ast`) and the TypeScript generator in ``packages/maps-ts``
(analyzes TS/JS repos via regex/token heuristics) — MUST emit a document that
conforms to this grammar with byte-identical key order and sorting for equivalent
inputs. That parity is what makes the map diff-stable across runs and across the
two language ecosystems.

Grammar (top-level keys, in this exact order):

    version        : int            — grammar schema version (currently 1).
    generated_at   : str            — ISO-8601 UTC timestamp the map was produced.
                                     Deterministic only when pinned by the caller
                                     (the CLI uses wall-clock time; tests pin it).
    repo           : str            — repository name (basename of the repo path).
    modules        : list[Module]   — directory tree with one-line inferred purpose.
    symbols        : list[Symbol]   — public classes/functions per module.
    edges          : list[Edge]     — intra-repo import edges between top-level modules.
    hot_files      : list[HotFile]  — files most imported by others, ranked desc.
    entrypoints    : list[str]      — known entry files (main.py / index.ts / ...).
    tests          : list[str]      — test file paths.

Sub-object shapes (field order is canonical and MUST be preserved by serializers):

    Module  : {path: str, language: str, file_count: int, purpose: str}
    Symbol  : {module: str, file: str, name: str, kind: str, signature: str,
               line: int}
    Edge    : {from: str, to: str, kind: str}
    HotFile : {file: str, importers: int}

Determinism rules enforced by :func:`canonicalize`:

  * Every list is sorted by a stable, total key (see each ``*_sort_key`` helper).
  * Every string is a forward-slash POSIX path regardless of host OS.
  * ``generated_at`` is the ONLY non-deterministic field; callers pin it for
    golden-fixture comparison.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Iterable

# Bumped only when the grammar shape changes in a way that breaks consumers.
SCHEMA_VERSION: int = 1

# Languages a generator may tag a module with.
LANGUAGE_PYTHON = "python"
LANGUAGE_TYPESCRIPT = "typescript"
LANGUAGE_JAVASCRIPT = "javascript"

# Edge kinds currently emitted by both generators.
EDGE_IMPORT = "import"


@dataclass
class Module:
    """A directory in the repo that directly contains at least one source file."""

    path: str
    language: str
    file_count: int
    purpose: str


@dataclass
class Symbol:
    """A public class or function declared in a file, with its signature line."""

    module: str
    file: str
    name: str
    kind: str  # "class" | "function" | "interface" | "const" | ...
    signature: str
    line: int


@dataclass
class Edge:
    """A directed import edge between two top-level modules in the repo."""

    from_: str = field(metadata={"json": "from"})
    to: str = ""
    kind: str = EDGE_IMPORT


@dataclass
class HotFile:
    """A file ranked by how many other files in the repo import it."""

    file: str
    importers: int


@dataclass
class RepoMap:
    """The full map document. Serialized to ``map.json`` in canonical order."""

    version: int = SCHEMA_VERSION
    generated_at: str = ""
    repo: str = ""
    modules: list[Module] = field(default_factory=list)
    symbols: list[Symbol] = field(default_factory=list)
    edges: list[Edge] = field(default_factory=list)
    hot_files: list[HotFile] = field(default_factory=list)
    entrypoints: list[str] = field(default_factory=list)
    tests: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Sort keys — stable, total orderings so output is byte-stable across runs.
# ---------------------------------------------------------------------------

def module_sort_key(m: Module) -> tuple:
    return (m.path,)


def symbol_sort_key(s: Symbol) -> tuple:
    return (s.file, s.line, s.name, s.kind)


def edge_sort_key(e: Edge) -> tuple:
    return (e.from_, e.to, e.kind)


def hot_file_sort_key(h: HotFile) -> tuple:
    # Rank by importer count desc, then file path asc for tie-break stability.
    return (-h.importers, h.file)


def to_posix(p: str) -> str:
    """Normalize a path to forward-slash POSIX form for diff-stable output."""
    return p.replace("\\", "/")


def canonicalize(m: RepoMap) -> RepoMap:
    """Return a copy of ``m`` with every list deterministically sorted.

    Idempotent and pure — safe to call on an already-canonical map.
    """
    m.modules = sorted(m.modules, key=module_sort_key)
    m.symbols = sorted(m.symbols, key=symbol_sort_key)
    m.edges = sorted(m.edges, key=edge_sort_key)
    m.hot_files = sorted(m.hot_files, key=hot_file_sort_key)
    m.entrypoints = sorted(m.entrypoints)
    m.tests = sorted(m.tests)
    return m


def to_dict(m: RepoMap) -> dict[str, Any]:
    """Serialize a RepoMap to a dict with the canonical key order.

    The dataclass field declaration order is preserved, and the ``from_`` field
    on :class:`Edge` is renamed to ``from`` for JSON fidelity.
    """
    return {
        "version": m.version,
        "generated_at": m.generated_at,
        "repo": m.repo,
        "modules": [
            {
                "path": mod.path,
                "language": mod.language,
                "file_count": mod.file_count,
                "purpose": mod.purpose,
            }
            for mod in m.modules
        ],
        "symbols": [
            {
                "module": s.module,
                "file": s.file,
                "name": s.name,
                "kind": s.kind,
                "signature": s.signature,
                "line": s.line,
            }
            for s in m.symbols
        ],
        "edges": [
            {"from": e.from_, "to": e.to, "kind": e.kind}
            for e in m.edges
        ],
        "hot_files": [
            {"file": h.file, "importers": h.importers}
            for h in m.hot_files
        ],
        "entrypoints": list(m.entrypoints),
        "tests": list(m.tests),
    }


def from_dict(d: dict[str, Any]) -> RepoMap:
    """Parse a dict (e.g. loaded from ``map.json``) back into a RepoMap.

    Tolerant of missing keys so it can read maps produced by either generator.
    """
    modules = [
        Module(
            path=mod["path"],
            language=mod["language"],
            file_count=mod["file_count"],
            purpose=mod.get("purpose", ""),
        )
        for mod in d.get("modules", [])
    ]
    symbols = [
        Symbol(
            module=s["module"],
            file=s["file"],
            name=s["name"],
            kind=s["kind"],
            signature=s.get("signature", ""),
            line=s["line"],
        )
        for s in d.get("symbols", [])
    ]
    edges = [
        Edge(from_=e["from"], to=e["to"], kind=e.get("kind", EDGE_IMPORT))
        for e in d.get("edges", [])
    ]
    hot_files = [
        HotFile(file=h["file"], importers=h["importers"])
        for h in d.get("hot_files", [])
    ]
    return RepoMap(
        version=d.get("version", SCHEMA_VERSION),
        generated_at=d.get("generated_at", ""),
        repo=d.get("repo", ""),
        modules=modules,
        symbols=symbols,
        edges=edges,
        hot_files=hot_files,
        entrypoints=list(d.get("entrypoints", [])),
        tests=list(d.get("tests", [])),
    )


def render_markdown(m: RepoMap) -> str:
    """Render a condensed MAP.md-style text view of the map.

    Deterministic: assumes ``m`` is already canonicalized. Pure function.
    """
    lines: list[str] = []
    lines.append(f"# {m.repo} — repo map")
    lines.append("")
    lines.append(f"_generated: {m.generated_at} · grammar v{m.version}_")
    lines.append("")
    lines.append("## Modules")
    for mod in m.modules:
        purpose = mod.purpose or "(no inferred purpose)"
        lines.append(f"- `{mod.path}` ({mod.language}, {mod.file_count} files) — {purpose}")
    lines.append("")
    lines.append("## Entrypoints")
    if m.entrypoints:
        for ep in m.entrypoints:
            lines.append(f"- `{ep}`")
    else:
        lines.append("- _(none detected)_")
    lines.append("")
    lines.append("## Symbols")
    if m.symbols:
        for s in m.symbols:
            lines.append(f"- `{s.file}:{s.line}` {s.kind} `{s.name}` — {s.signature}")
    else:
        lines.append("- _(none detected)_")
    lines.append("")
    lines.append("## Edges")
    if m.edges:
        for e in m.edges:
            lines.append(f"- `{e.from_}` -> `{e.to}` ({e.kind})")
    else:
        lines.append("- _(none detected)_")
    lines.append("")
    lines.append("## Hot files")
    if m.hot_files:
        for h in m.hot_files:
            lines.append(f"- `{h.file}` ({h.importers} importers)")
    else:
        lines.append("- _(none detected)_")
    lines.append("")
    lines.append("## Tests")
    if m.tests:
        for t in m.tests:
            lines.append(f"- `{t}`")
    else:
        lines.append("- _(none detected)_")
    lines.append("")
    return "\n".join(lines)


__all__ = [
    "SCHEMA_VERSION",
    "LANGUAGE_PYTHON",
    "LANGUAGE_TYPESCRIPT",
    "LANGUAGE_JAVASCRIPT",
    "EDGE_IMPORT",
    "Module",
    "Symbol",
    "Edge",
    "HotFile",
    "RepoMap",
    "module_sort_key",
    "symbol_sort_key",
    "edge_sort_key",
    "hot_file_sort_key",
    "to_posix",
    "canonicalize",
    "to_dict",
    "from_dict",
    "render_markdown",
]
