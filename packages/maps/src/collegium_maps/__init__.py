"""collegium-maps: repo-map generator + citation linter (Collegium context layer 1).

One grammar (see :mod:`collegium_maps.grammar`), two generators:
  * Python generator here (:mod:`collegium_maps.generate`) — analyzes Python repos via :mod:`ast`.
  * TypeScript generator in ``packages/maps-ts`` — analyzes TS/JS repos via regex.

CLIs:
  * ``python -m collegium_maps.generate <repo_path> [--out .collegium]``
  * ``python -m collegium_maps.lint <repo_path> <citations.json>``
"""

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
from collegium_maps.generate import GenerateReport, generate, write_map
from collegium_maps.lint import LintReport, lint, parse_citation

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
