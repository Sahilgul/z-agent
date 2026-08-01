"""Python repo-map generator  analyzes Python repos with the stdlib :mod:`ast`.

This is the Python half of the shared-grammar generator pair. It scans a repo,
parses every ``.py`` file with :mod:`ast`, and emits a
:class:`zagent_maps.grammar.RepoMap` conforming to the canonical grammar. The
TypeScript half lives in ``packages/maps-ts`` and emits the same shape for
TS/JS repos.

Design notes:
  * Pure stdlib  no tree-sitter, no third-party deps.
  * Never crashes on a single bad file: unreadable / binary / syntactically
    invalid files are skipped and counted as ``crashes`` in the run report.
  * Deterministic: output is canonicalized (sorted) before writing, so two runs
    over the same tree produce byte-identical ``map.json`` (modulo
    ``generated_at``, which the CLI sets to wall-clock time and tests pin).
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from zagent_maps.grammar import (
    EDGE_IMPORT,
    LANGUAGE_PYTHON,
    HotFile,
    Module,
    RepoMap,
    Symbol,
    Edge,
    canonicalize,
    render_markdown,
    to_dict,
    to_posix,
)

# Directories never descended into. Lower-cased for case-insensitive matching.
IGNORED_DIRS: frozenset[str] = frozenset({
    ".git", ".hg", ".svn",
    ".venv", "venv", "env",
    "node_modules", "__pycache__",
    ".zagent",
    "build", "dist", "out", "coverage", ".next",
    ".pytest_cache", ".ruff_cache", ".mypy_cache", ".tox",
    "site-packages",
})

# Filenames treated as entrypoints when present anywhere in the tree.
ENTRYPOINT_BASENAMES: frozenset[str] = frozenset({
    "main.py", "app.py", "server.py", "cli.py", "__main__.py",
    "manage.py", "wsgi.py", "asgi.py", "run.py",
})

# Module path used for files that live directly in the repo root.
ROOT_MODULE = "."


@dataclass
class GenerateReport:
    """Summary of a generate run, returned alongside the RepoMap."""

    repo_path: str
    files_scanned: int = 0
    files_skipped: int = 0
    crashes: list[str] = field(default_factory=list)
    modules_count: int = 0
    symbols_count: int = 0
    edges_count: int = 0
    hot_files_top: list[HotFile] = field(default_factory=list)

    @property
    def crash_count(self) -> int:
        return len(self.crashes)

    def as_dict(self) -> dict[str, Any]:
        return {
            "repo_path": self.repo_path,
            "files_scanned": self.files_scanned,
            "files_skipped": self.files_skipped,
            "crashes": list(self.crashes),
            "crash_count": len(self.crashes),
            "modules_count": self.modules_count,
            "symbols_count": self.symbols_count,
            "edges_count": self.edges_count,
            "hot_files_top": [
                {"file": h.file, "importers": h.importers} for h in self.hot_files_top
            ],
        }


def iter_source_files(repo_root: Path) -> Iterable[Path]:
    """Yield ``.py`` files under ``repo_root``, skipping ignored directories."""
    for dirpath, dirnames, filenames in os.walk(repo_root):
        dirnames[:] = [d for d in dirnames if d.lower() not in IGNORED_DIRS]
        for name in filenames:
            if name.lower().endswith(".py"):
                yield Path(dirpath) / name


def rel_posix(repo_root: Path, path: Path) -> str:
    """Return ``path`` relative to ``repo_root`` as a POSIX string."""
    return to_posix(str(path.relative_to(repo_root)))


def dir_module(repo_root: Path, path: Path) -> str:
    """Module path for a file = its containing directory, relative & POSIX.

    Root-level files map to :data:`ROOT_MODULE` (``"."``).
    """
    parent = path.parent
    if parent == repo_root:
        return ROOT_MODULE
    return rel_posix(repo_root, parent)


# ---------------------------------------------------------------------------
# AST analysis
# ---------------------------------------------------------------------------

@dataclass
class _FileAnalysis:
    """Per-file extraction result."""

    path: str  # posix relative
    module: str
    docstring: str | None
    symbols: list[Symbol]
    imported_local_files: set[str] = field(default_factory=set)


def _signature_for_function(node: ast.FunctionDef) -> str:
    """Reconstruct a compact ``def name(args)`` signature string."""
    args = node.args
    parts: list[str] = []
    posonly = getattr(args, "posonlyargs", None) or []
    for a in posonly:
        parts.append(a.arg)
    if posonly:
        parts.append("/")
    for a in args.args:
        parts.append(a.arg)
    if args.vararg:
        parts.append("*" + args.vararg.arg)
    elif args.kwonlyargs:
        parts.append("*")
    for a in args.kwonlyargs:
        parts.append(a.arg)
    if args.kwarg:
        parts.append("**" + args.kwarg.arg)
    defaults_note = ""
    if args.defaults:
        defaults_note = f"  # {len(args.defaults)} default(s)"
    return f"def {node.name}({', '.join(parts)}){defaults_note}".rstrip()


def _signature_for_class(node: ast.ClassDef) -> str:
    bases = [ast.unparse(b) if hasattr(ast, "unparse") else getattr(b, "id", "?")
            for b in node.bases]
    if bases:
        return f"class {node.name}({', '.join(bases)})"
    return f"class {node.name}"


def _is_public(name: str) -> bool:
    return not name.startswith("_")


def analyze_file(repo_root: Path, path: Path,
                 all_py_modules: dict[str, str]) -> _FileAnalysis | None:
    """Parse one Python file and extract its docstring, symbols, and local imports.

    Returns ``None`` (and the caller records a crash) if the file cannot be read
    or parsed. Never raises.
    """
    rel = rel_posix(repo_root, path)
    module = dir_module(repo_root, path)
    try:
        src = path.read_text(encoding="utf-8", errors="strict")
    except (OSError, UnicodeDecodeError):
        return None
    try:
        tree = ast.parse(src, filename=str(path))
    except SyntaxError:
        return None

    docstring = ast.get_docstring(tree)
    symbols: list[Symbol] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if _is_public(node.name):
                symbols.append(Symbol(
                    module=module, file=rel, name=node.name,
                    kind="function",
                    signature=_signature_for_function(node),
                    line=node.lineno,
                ))
        elif isinstance(node, ast.ClassDef):
            if _is_public(node.name):
                symbols.append(Symbol(
                    module=module, file=rel, name=node.name,
                    kind="class",
                    signature=_signature_for_class(node),
                    line=node.lineno,
                ))

    imported_local = _collect_local_imports(tree, repo_root, path, all_py_modules)

    return _FileAnalysis(
        path=rel, module=module, docstring=docstring,
        symbols=symbols, imported_local_files=imported_local,
    )


def _collect_local_imports(tree: ast.AST, repo_root: Path, path: Path,
                            all_py_modules: dict[str, str]) -> set[str]:
    """Resolve import statements to local repo files (best-effort).

    ``all_py_modules`` maps a dotted module name (e.g. ``"pkg.sub.mod"``) to the
    relative POSIX path of the corresponding ``.py`` file (or ``__init__.py``).
    Relative imports (``from . import x`` / ``from .x import y``) are resolved
    against the importing file's package.
    """
    resolved: set[str] = set()
    importer_pkg_parts = list(path.relative_to(repo_root).parts)
    importer_pkg_parts = importer_pkg_parts[:-1]  # drop filename, keep package dir

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                target = _resolve_dotted(alias.name, all_py_modules)
                if target:
                    resolved.add(target)
        elif isinstance(node, ast.ImportFrom):
            level = node.level or 0
            mod = node.module or ""
            if level > 0:
                base_parts = importer_pkg_parts[: len(importer_pkg_parts) - (level - 1)]
                if mod:
                    base_parts = base_parts + mod.split(".")
                dotted = ".".join(base_parts)
                target = _resolve_dotted(dotted, all_py_modules)
                if target:
                    resolved.add(target)
                else:
                    for alias in node.names:
                        sub = f"{dotted}.{alias.name}" if dotted else alias.name
                        t = _resolve_dotted(sub, all_py_modules)
                        if t:
                            resolved.add(t)
            else:
                target = _resolve_dotted(mod, all_py_modules)
                if target:
                    resolved.add(target)
                else:
                    for alias in node.names:
                        sub = f"{mod}.{alias.name}"
                        t = _resolve_dotted(sub, all_py_modules)
                        if t:
                            resolved.add(t)
    return resolved


def _resolve_dotted(dotted: str, all_py_modules: dict[str, str]) -> str | None:
    """Resolve a dotted module name to a relative POSIX file path.

    Tries the exact name, then progressively strips trailing components (so
    ``pkg.mod.attr`` resolves to ``pkg/mod.py``), and also progressively
    strips leading components so that imports prefixed with the repo's
    package name (e.g. ``python_repo.pkg.core`` when the repo root holds
    ``pkg/core.py``) still resolve.
    """
    if not dotted:
        return None
    parts = dotted.split(".")
    # Try the full name, then drop trailing components.
    for i in range(len(parts), 0, -1):
        prefix = ".".join(parts[:i])
        if prefix in all_py_modules:
            return all_py_modules[prefix]
    # Try dropping leading components (repo-basename prefix tolerance).
    for i in range(1, len(parts)):
        candidate = ".".join(parts[i:])
        if candidate in all_py_modules:
            return all_py_modules[candidate]
        for j in range(len(parts) - i, 0, -1):
            prefix = ".".join(parts[i:i + j])
            if prefix in all_py_modules:
                return all_py_modules[prefix]
    return None


def build_module_index(repo_root: Path, files: list[Path]) -> dict[str, str]:
    """Map every importable dotted module name to its relative POSIX file path.

    A file ``src/pkg/sub/mod.py`` is registered as ``"src.pkg.sub.mod"``.
    An ``__init__.py`` is registered as its package dotted name. This is a
    heuristic that works for the common flat-ish HAMI service layouts.
    """
    index: dict[str, str] = {}
    for f in files:
        rel = rel_posix(repo_root, f)
        if rel.endswith("/__init__.py"):
            pkg = rel[: -len("/__init__.py")]
            index[pkg.replace("/", ".")] = rel
        else:
            stem = rel[: -len(".py")]
            index[stem.replace("/", ".")] = rel
    return index


# ---------------------------------------------------------------------------
# Top-level orchestration
# ---------------------------------------------------------------------------

def _infer_purpose(analyses_by_module: dict[str, list[_FileAnalysis]]) -> dict[str, str]:
    """Pick the first non-empty docstring in a module as its one-line purpose."""
    purpose: dict[str, str] = {}
    for mod, analyses in analyses_by_module.items():
        for a in analyses:
            if a.docstring:
                first_line = a.docstring.strip().splitlines()[0].strip()
                if first_line:
                    purpose[mod] = first_line
                    break
    return purpose


def _is_test_file(rel: str) -> bool:
    name = rel.rsplit("/", 1)[-1].lower()
    if name.startswith("test_") or name.endswith("_test.py"):
        return True
    return ("/tests/" in rel + "/") or ("/test/" in rel + "/")


def generate(repo_path: str | os.PathLike[str],
             generated_at: str | None = None) -> tuple[RepoMap, GenerateReport]:
    """Generate a :class:`RepoMap` for the Python repo at ``repo_path``.

    Returns the map and a :class:`GenerateReport` with scan stats and any
    per-file crashes. Pure: does not write to disk. Use :func:`write_map` to
    persist, or call from the CLI.
    """
    repo_root = Path(repo_path).resolve()
    report = GenerateReport(repo_path=str(repo_root))

    files = list(iter_source_files(repo_root))
    module_index = build_module_index(repo_root, files)

    analyses: list[_FileAnalysis] = []
    for f in files:
        report.files_scanned += 1
        a = analyze_file(repo_root, f, module_index)
        if a is None:
            report.crashes.append(rel_posix(repo_root, f))
            report.files_skipped += 1
            continue
        analyses.append(a)

    by_module: dict[str, list[_FileAnalysis]] = {}
    for a in analyses:
        by_module.setdefault(a.module, []).append(a)
    purpose = _infer_purpose(by_module)

    modules = [
        Module(path=mod, language=LANGUAGE_PYTHON,
               file_count=len(analyses), purpose=purpose.get(mod, ""))
        for mod, analyses in by_module.items()
    ]

    symbols: list[Symbol] = []
    for a in analyses:
        symbols.extend(a.symbols)

    # Edges: source module -> target module, deduped.
    edge_pairs: set[tuple[str, str]] = set()
    file_to_module: dict[str, str] = {a.path: a.module for a in analyses}
    for a in analyses:
        for imported in a.imported_local_files:
            target_mod = file_to_module.get(imported)
            if target_mod and target_mod != a.module:
                edge_pairs.add((a.module, target_mod))
    edges = [Edge(from_=frm, to=to, kind=EDGE_IMPORT) for frm, to in edge_pairs]

    # Hot files: count importers per file.
    importer_counts: dict[str, int] = {}
    for a in analyses:
        for imported in a.imported_local_files:
            if imported in file_to_module:
                importer_counts[imported] = importer_counts.get(imported, 0) + 1
    hot_files = [HotFile(file=f, importers=n) for f, n in importer_counts.items()]

    entrypoints: list[str] = []
    for f in files:
        if f.name in ENTRYPOINT_BASENAMES:
            entrypoints.append(rel_posix(repo_root, f))

    tests = [rel_posix(repo_root, f) for f in files if _is_test_file(rel_posix(repo_root, f))]

    repo_map = RepoMap(
        generated_at=generated_at or _now_iso(),
        repo=repo_root.name,
        modules=modules,
        symbols=symbols,
        edges=edges,
        hot_files=hot_files,
        entrypoints=entrypoints,
        tests=tests,
    )
    repo_map = canonicalize(repo_map)

    report.modules_count = len(repo_map.modules)
    report.symbols_count = len(repo_map.symbols)
    report.edges_count = len(repo_map.edges)
    report.hot_files_top = repo_map.hot_files[:5]
    return repo_map, report


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def write_map(repo_map: RepoMap, repo_path: str | os.PathLike[str],
              out_dir: str | os.PathLike[str] = ".zagent") -> tuple[Path, Path]:
    """Write ``map.json`` and ``map.md`` under ``<repo_path>/<out_dir>``.

    Creates the output directory if needed. Returns the two written paths.
    """
    repo_root = Path(repo_path).resolve()
    out = repo_root / out_dir
    out.mkdir(parents=True, exist_ok=True)
    json_path = out / "map.json"
    md_path = out / "map.md"
    json_path.write_text(
        json.dumps(to_dict(repo_map), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    md_path.write_text(render_markdown(repo_map), encoding="utf-8")
    return json_path, md_path


def main(argv: list[str] | None = None) -> int:
    """CLI entry: ``python -m zagent_maps.generate <repo_path> [--out .zagent]``."""
    parser = argparse.ArgumentParser(prog="zagent-maps-generate", description=__doc__)
    parser.add_argument("repo_path", help="Path to the Python repo to map.")
    parser.add_argument("--out", default=".zagent",
                        help="Output dir name under the repo (default: .zagent).")
    parser.add_argument("--report", action="store_true",
                        help="Print the generate report JSON to stderr.")
    args = parser.parse_args(argv)

    repo_map, report = generate(args.repo_path)
    write_map(repo_map, args.repo_path, args.out)
    if args.report:
        print(json.dumps(report.as_dict(), indent=2), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())






