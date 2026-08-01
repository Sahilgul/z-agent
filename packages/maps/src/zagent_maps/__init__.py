"""zagent-maps: repo-map generator + citation linter (Zagent context layer 1).

One grammar (see :mod:`zagent_maps.grammar`), two generators:
  * Python generator here (:mod:`zagent_maps.generate`) — analyzes Python repos via :mod:`ast`.
  * TypeScript generator in ``packages/maps-ts`` — analyzes TS/JS repos via regex.

CLIs:
  * ``python -m zagent_maps.generate <repo_path> [--out .zagent]``
  * ``python -m zagent_maps.lint <repo_path> <citations.json>``
"""

from zagent_maps.grammar import (
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
from zagent_maps.generate import GenerateReport, generate, write_map
from zagent_maps.lint import LintReport, lint, parse_citation

__all__ = [
    "SCHEMA_VERSION",
    "Edge",
    "HotFile",
    "Module",
    "RepoMap",
    "Symbol",
    "canonicalize",
    "from_dict",
    "render_markdown",
    "to_dict",
    "to_posix",
    "GenerateReport",
    "generate",
    "write_map",
    "LintReport",
    "lint",
    "parse_citation",
]
