"""Two-tier tool surface: deferred discovery.

Tier 0 — DEFAULT_TOOLS (8) bound every turn; everything else DEFERRED behind
tool_search. This module is the codex-style index + kimi-style exact-name
load + the roster fragment renderer.

Index entries tokenize: name + name-with-spaces + description + recursive
JSON-schema property names + capability/mode tags. MCP catalog folds into
the same index (MCP tools are never bound unless discovered). Fail-closed:
mode-denied tools are ABSENT from both the index and the roster.
"""

from __future__ import annotations

import re
from typing import Any

from langchain_core.tools import tool

MAX_RESULTS_DEFAULT = 5
# roster fragment budget: <=0.5K tokens (~4 chars/token conservative)
ROSTER_CHAR_BUDGET = 1800


class IndexEntry:
    def __init__(self, name: str, description: str, schema: dict[str, Any],
                 capability: str, modes: list[str], mcp: bool = False) -> None:
        self.name = name
        self.description = description
        self.schema = schema
        self.capability = capability
        self.modes = modes
        self.mcp = mcp
        self.tokens = _tokenize(name) | _tokenize(name.replace("_", " ")) \
            | _tokenize(description) | _schema_tokens(schema) \
            | {capability, *(f"mode:{m}" for m in modes)}

    def one_liner(self) -> str:
        first = self.description.strip().split("\n")[0]
        return first[:80]


def _tokenize(text: str) -> set[str]:
    return {t for t in re.split(r"[^a-z0-9]+", text.lower()) if len(t) >= 2}


def _schema_tokens(schema: dict[str, Any]) -> set[str]:
    """Recursive schema property names (codex tool_search.rs parity)."""
    out: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, val in node.items():
                if key == "properties" and isinstance(val, dict):
                    out.update(_tokenize(" ".join(val.keys())))
                walk(val)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(schema)
    return out


def _tool_schema(t: Any) -> dict[str, Any]:
    try:
        args = getattr(t, "args_schema", None)
        if args is not None and hasattr(args, "model_json_schema"):
            return args.model_json_schema()
    except Exception:
        return {}
    return {}


def _modes_for(name: str) -> list[str]:
    from worker.engine.tools import MODE_ALLOWED
    return [m for m, allowed in MODE_ALLOWED.items() if name in allowed]


def build_index(*, include_mcp: bool = True) -> dict[str, IndexEntry]:
    """Snapshot the index over the full registry (+ MCP catalog fold-in)."""
    from worker.engine.tools import (
        ALL_BUILT_TOOL_BY_NAME,
        capability_of,
    )

    entries: dict[str, IndexEntry] = {}
    for real_name, t in ALL_BUILT_TOOL_BY_NAME.items():
        modes = _modes_for(real_name)
        entries[real_name] = IndexEntry(
            real_name, (t.description or "").strip(), _tool_schema(t),
            capability_of(real_name).value, modes)
        # Index under the contract alias too (code_search -> file_search).
        if real_name == "file_search":
            entries["code_search"] = IndexEntry(
                "code_search", (t.description or "").strip(), _tool_schema(t),
                capability_of(real_name).value, modes)
    if include_mcp:
        from worker.engine.mcp import mcp_manager
        for server, names in mcp_manager().catalog().items():
            for exposed in names:
                entries[exposed] = IndexEntry(
                    exposed, f"MCP tool on server {server}", {},
                    "mcp", _modes_for_mcp(), mcp=True)
    # Drop aliased duplicates from the canonical-name view only at render.
    return entries


def _modes_for_mcp() -> list[str]:
    from worker.engine.tools import MODE_ALLOWED
    return [m for m, allowed in MODE_ALLOWED.items() if "mcp__*" in allowed]


def visible_index(mode: str) -> dict[str, IndexEntry]:
    """Fail-closed: mode-denied tools are absent from the index."""
    from worker.engine.tools import mode_allowed
    return {n: e for n, e in build_index().items()
            if mode_allowed(resolve_alias(n), mode)}


def resolve_alias(name: str) -> str:
    from worker.engine.tools import resolve_tool_name
    return resolve_tool_name(name)


# --- query search (token match over the codex-style index) ---

def search(query: str, *, mode: str, max_results: int = MAX_RESULTS_DEFAULT) -> list[IndexEntry]:
    q_tokens = _tokenize(query)
    if not q_tokens:
        return []
    scored: list[tuple[int, IndexEntry]] = []
    for entry in visible_index(mode).values():
        hits = len(q_tokens & entry.tokens)
        if hits:
            scored.append((hits, entry))
    scored.sort(key=lambda kv: (-kv[0], kv[1].name))
    return [e for _h, e in scored[:max_results]]


# --- exact-name load (kimi select_tools parity: toLoad|alreadyAvailable|unknown) ---

def exact_names(names: list[str], *, mode: str, bound: list[str]) -> dict[str, list[str]]:
    index = visible_index(mode)
    to_load, already, unknown = [], [], []
    bound_set = {resolve_alias(n) for n in bound}
    for name in names:
        resolved = resolve_alias(name)
        if resolved in bound_set:
            already.append(name)
        elif resolved in index or name in index:
            to_load.append(resolved)
        else:
            unknown.append(name)
    return {"toLoad": to_load, "alreadyAvailable": already, "unknown": unknown}


def full_schemas(names: list[str], *, mode: str) -> dict[str, dict[str, Any]]:
    index = visible_index(mode)
    return {n: {"name": n, "description": index[n].description,
                "schema": index[n].schema, "capability": index[n].capability}
            for n in names if n in index}


# --- roster fragment (deferred names + one-liners, <=0.5K tokens) ---

def roster_fragment(mode: str, *, bound: list[str], char_budget: int = ROSTER_CHAR_BUDGET) -> str:
    bound_set = {resolve_alias(n) for n in bound}
    lines = []
    for name, entry in sorted(visible_index(mode).items()):
        if resolve_alias(name) in bound_set or name != resolve_alias(name):
            continue  # skip bound tools and alias duplicates
        if entry.mcp:
            continue  # MCP entries fold in only after a catalog snapshot
        lines.append(f"  {name} — {entry.one_liner()}")
    mcp_entries = [(n, e) for n, e in sorted(visible_index(mode).items()) if e.mcp]
    for name, entry in mcp_entries:
        lines.append(f"  {name} — {entry.one_liner()}")
    header = ("<tool-roster>\nDeferred tools (NOT bound — load with tool_search "
              "before calling; loaded tools stay bound for the session):")
    body = "\n".join(lines)
    footer = "</tool-roster>"
    out = f"{header}\n{body}\n{footer}"
    if len(out) > char_budget:
        out = out[:char_budget] + "\n  ... roster truncated ...\n" + footer
    return out


def skills_roster_fragment() -> str:
    """K7: the skills list the system prompt promises. One line per playbook
    (name + first-line description), mirroring the playbook_load surface."""
    from pathlib import Path

    pdir = Path(__file__).resolve().parent.parent / "prompts" / "playbooks"
    if not pdir.exists():
        return ""
    lines = []
    for path in sorted(pdir.glob("*.md")):
        first = ""
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip().lstrip("#").strip()
            if stripped:
                first = stripped
                break
        lines.append(f"  {path.stem} — {first or '(no description)'}")
    if not lines:
        return ""
    return ("Available skills (load the full procedure with "
            "playbook_load(name)):\n" + "\n".join(lines))


# --- the tool_search tool itself (Tier 0 — always bound) ---

@tool
def tool_search(query: str | None = None, names: list[str] | None = None,
                max_results: int = MAX_RESULTS_DEFAULT) -> str:
    """Discover deferred tools. Call this BEFORE using any tool that is not in
    your default set — never guess tool names.

    Args:
        query: capability keywords (e.g. "fetch url web page") — returns the
            top matches with FULL JSON schemas; matches bind for the session.
        names: exact tool names to load (e.g. ["web_fetch"]) — returns
            toLoad | alreadyAvailable | unknown buckets.
        max_results: cap on query matches (default 5).
    """
    return "ok: dispatched by the engine (discovery in tools/discovery.py)"


async def tool_search_async(args: dict[str, Any], *, mode: str,
                            bound: list[str]) -> dict[str, Any]:
    """Real dispatch: query search AND/OR exact-name load. Returns the matched
    names as "discovered" so the graph merges them into state.discovered_tools."""
    discovered: list[str] = []
    sections: list[str] = []
    query = args.get("query")
    names = args.get("names")
    max_results = int(args.get("max_results", MAX_RESULTS_DEFAULT))

    if query:
        matches = search(query, mode=mode, max_results=max_results)
        schemas = full_schemas([e.name for e in matches], mode=mode)
        sections.append(f"Found {len(matches)} tool(s) matching {query!r}:")
        import json
        for name, info in schemas.items():
            sections.append(json.dumps(info, default=str))
            if resolve_alias(name) not in {resolve_alias(b) for b in bound}:
                discovered.append(resolve_alias(name))
        if not matches:
            sections.append("(no matches — try other keywords, or names=[...] "
                            "if you know the exact name)")

    if names:
        buckets = exact_names(names, mode=mode, bound=bound)
        schemas = full_schemas(buckets["toLoad"], mode=mode)
        discovered.extend(buckets["toLoad"])
        import json
        sections.append(f"toLoad: {buckets['toLoad']}")
        sections.append(f"alreadyAvailable: {buckets['alreadyAvailable']}")
        sections.append(f"unknown: {buckets['unknown']}")
        for info in schemas.values():
            sections.append(json.dumps(info, default=str))

    if not query and not names:
        return {"kind": "error", "ok": False,
                "output": "error: provide query and/or names"}

    return {
        "kind": "success", "ok": True,
        "output": "\n".join(sections),
        "tool": "tool_search", "args": args,
        "discovered": sorted(set(discovered)),
    }


__all__ = [
    "MAX_RESULTS_DEFAULT",
    "ROSTER_CHAR_BUDGET",
    "IndexEntry",
    "build_index",
    "exact_names",
    "full_schemas",
    "roster_fragment",
    "search",
    "tool_search",
    "tool_search_async",
    "visible_index",
]
