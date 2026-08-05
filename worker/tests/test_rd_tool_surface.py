"""RD acceptance tests (Phase 10, R34) — P10 exit criteria verbatim:

1. default bind <=10 schemas
2. roster fragment <=0.5K tokens
3. tool_search finds a deferred tool by capability keyword AND by exact name
   (unknown names bucketed, never crash)
4. a discovered tool is callable next turn as a native typed call
5. a mode-denied tool is absent from BOTH index and roster
6. discovered_tools survives checkpoint resume + compaction
7. MCP tools discoverable after catalog snapshot, per-server failures isolated
"""

from __future__ import annotations

from pathlib import Path

import pytest
from langchain_core.messages import ToolMessage
from test_spine_contract import EventCollector, _ai, _config, _initial, _patch_llm, _tc

from worker.engine.tools import (
    DEFAULT_TOOLS,
    default_tool_names,
    mode_allowed,
    tools_for_mode,
)
from worker.engine.tools.discovery import (
    ROSTER_CHAR_BUDGET,
    exact_names,
    roster_fragment,
    search,
    visible_index,
)

# ----------------------------------------------------------- 1. default bind


class TestDefaultBind:
    def test_tier0_contract_names(self):
        assert DEFAULT_TOOLS == ["file_read", "file_edit", "file_write", "terminal_exec",
                                 "code_search", "file_glob", "update_tasks", "tool_search"]

    @pytest.mark.parametrize("mode", ["ask", "plan", "development", "debug", "goal"])
    def test_default_bind_le_10_schemas(self, mode: str):
        bound = tools_for_mode(mode)
        assert len(bound) <= 10, f"{mode} binds {len(bound)} schemas"
        # every bound tool is mode-allowed (fail-closed binding)
        for t in bound:
            assert mode_allowed(t.name, mode), f"{t.name} bound but mode-denied in {mode}"

    def test_ask_mode_binds_only_readonly(self):
        names = {t.name for t in tools_for_mode("ask")}
        assert "file_write" not in names and "file_edit" not in names
        assert {"file_read", "file_search", "file_glob", "terminal_exec",
                "update_tasks", "tool_search"} <= names

    def test_goal_adds_fanout_but_stays_in_budget(self):
        names = {t.name for t in tools_for_mode("goal")}
        assert {"spawn_agent", "spawn_swarm"} <= names
        assert len(names) <= 10


# ----------------------------------------------------------- 2. roster budget


class TestRoster:
    @pytest.mark.parametrize("mode", ["ask", "plan", "development", "debug", "goal"])
    def test_roster_fragment_within_half_k_tokens(self, mode: str):
        frag = roster_fragment(mode, bound=default_tool_names(mode))
        assert len(frag) <= ROSTER_CHAR_BUDGET + 60  # +truncation notice slack

    def test_roster_excludes_bound_and_discovered(self):
        frag = roster_fragment("development",
                               bound=default_tool_names("development") + ["web_fetch"])
        assert "file_read —" not in frag  # bound tools are not "deferred"
        assert "web_fetch —" not in frag  # discovered tools leave the roster
        assert "playbook_load —" in frag  # deferred tools are listed


# ----------------------------------------------------------- 3. search


class TestToolSearch:
    def test_query_finds_by_capability_keyword(self):
        matches = search("fetch url web page", mode="development")
        assert any(e.name == "web_fetch" for e in matches)

    def test_query_finds_by_recursive_schema_property(self):
        # 'provenance' is a property name inside knowledge_draft's schema —
        # the recursive schema index must find it.
        matches = search("provenance", mode="development")
        assert any(e.name == "knowledge_draft" for e in matches)

    def test_exact_name_buckets(self):
        buckets = exact_names(["web_fetch", "file_read", "no_such_tool"],
                              mode="development",
                              bound=default_tool_names("development"))
        assert buckets["toLoad"] == ["web_fetch"]
        assert buckets["alreadyAvailable"] == ["file_read"]
        assert buckets["unknown"] == ["no_such_tool"]

    def test_alias_search_resolves_code_search(self):
        matches = search("regex search code", mode="development")
        names = {e.name for e in matches}
        assert "code_search" in names or "file_search" in names


# ----------------------------------------------------------- 5. fail-closed


class TestFailClosedDiscovery:
    def test_mode_denied_absent_from_index_and_roster(self):
        index = visible_index("ask")
        assert "file_write" not in index and "spawn_agent" not in index
        frag = roster_fragment("ask", bound=default_tool_names("ask"))
        assert "file_write" not in frag and "spawn_agent" not in frag

    def test_mcp_gated_by_mode(self):
        assert not mode_allowed("mcp__srv__tool", "plan") is False  # mcp__* in plan set
        assert mode_allowed("mcp__srv__tool", "development")


# ----------------------------------------------------------- 7. MCP fold-in


class TestMCPFoldIn:
    def test_mcp_catalog_discoverable_per_server_isolated(self, monkeypatch):
        import worker.engine.tools.discovery as disco
        from worker.engine.mcp import MCPManager

        mgr = MCPManager(servers=[{"name": "docs"}, {"name": "broken"}])
        mgr.status["docs"].connected = True
        mgr.status["docs"].tools = {"mcp__docs__search": object()}
        mgr.status["broken"].connected = False
        mgr.status["broken"].error = "connection refused"

        monkeypatch.setattr(disco, "mcp_manager", lambda: mgr, raising=False)
        import worker.engine.mcp as mcp_mod
        monkeypatch.setattr(mcp_mod, "_MANAGER", mgr)

        index = visible_index("development")
        assert "mcp__docs__search" in index  # connected server folds in
        assert not any(n.startswith("mcp__broken") for n in index)  # failure isolated
        matches = search("mcp docs", mode="development")
        assert any(e.name == "mcp__docs__search" for e in matches)


# ----------------------------------------------------------- 4 + 6. graph-level


@pytest.mark.asyncio
async def test_discovered_tool_callable_next_turn_native(monkeypatch: pytest.MonkeyPatch,
                                                         tmp_path: Path):
    """tool_search (turn 1) -> playbook_load (turn 2) executes as a NATIVE
    typed call; the discovered set is in state (checkpointed)."""
    from langgraph.checkpoint.memory import MemorySaver

    from worker.engine.graph import build_graph

    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path))
    collector = EventCollector()
    graph = build_graph(checkpointer=MemorySaver())
    config = _config(collector)
    _patch_llm(monkeypatch, [
        _ai("", [_tc("tc1", "tool_search", {"names": ["playbook_load"]})]),
        _ai("", [_tc("tc2", "playbook_load", {"name": "pr-from-story"})]),
        _ai("loaded the playbook"),
    ])
    result = await graph.ainvoke(_initial(), config)
    assert result.get("error") is None
    assert "playbook_load" in (result.get("discovered_tools") or [])
    tool_msgs = [m for m in result["messages"] if isinstance(m, ToolMessage)]
    assert any("Story to PR playbook" in str(m.content) for m in tool_msgs)


@pytest.mark.asyncio
async def test_discovered_tools_survive_compaction(monkeypatch: pytest.MonkeyPatch,
                                                   tmp_path: Path):
    """discovered_tools is STATE (not messages): compaction prunes messages,
    the discovered set survives — the next turn re-binds without re-search."""
    from langgraph.checkpoint.memory import MemorySaver

    from worker.engine.compaction import CompactionPolicy, Compactor
    from worker.engine.graph import build_graph

    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path))
    collector = EventCollector()
    graph = build_graph(checkpointer=MemorySaver())
    compactor = Compactor(CompactionPolicy(context_limit=10))  # force compaction
    config = _config(collector, compactor=compactor)
    _patch_llm(monkeypatch, [
        _ai("", [_tc("tc1", "tool_search", {"names": ["playbook_load"]})]),
        _ai("second turn after compaction"),
    ])
    result = await graph.ainvoke(_initial(), config)
    assert result.get("error") is None
    assert "playbook_load" in (result.get("discovered_tools") or [])


@pytest.mark.asyncio
async def test_discovered_tools_survive_checkpoint_resume(monkeypatch: pytest.MonkeyPatch,
                                                          tmp_path: Path):
    """Graph rebuild from the same checkpointer keeps discovered_tools."""
    from langgraph.checkpoint.memory import MemorySaver

    from worker.engine.graph import build_graph

    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path))
    collector = EventCollector()
    saver = MemorySaver()
    graph = build_graph(checkpointer=saver)
    config = _config(collector)
    _patch_llm(monkeypatch, [
        _ai("", [_tc("tc1", "tool_search", {"names": ["git_snapshot"]})]),
        _ai("ok"),
    ])
    await graph.ainvoke(_initial(), config)

    # Simulate container replacement: brand-new graph object, same checkpointer.
    graph2 = build_graph(checkpointer=saver)
    snap = await graph2.aget_state(config)
    assert "git_snapshot" in (snap.values.get("discovered_tools") or [])


@pytest.mark.asyncio
async def test_tool_search_unknown_names_never_crash(monkeypatch: pytest.MonkeyPatch,
                                                     tmp_path: Path):
    from langgraph.checkpoint.memory import MemorySaver

    from worker.engine.graph import build_graph

    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path))
    collector = EventCollector()
    graph = build_graph(checkpointer=MemorySaver())
    config = _config(collector)
    _patch_llm(monkeypatch, [
        _ai("", [_tc("tc1", "tool_search", {"names": ["hallucinated_tool"]})]),
        _ai("no such tool"),
    ])
    result = await graph.ainvoke(_initial(), config)
    assert result.get("error") is None
    tool_msgs = [m for m in result["messages"] if isinstance(m, ToolMessage)]
    assert any("unknown" in str(m.content) for m in tool_msgs)
    assert not result.get("discovered_tools")


@pytest.mark.asyncio
async def test_mode_request_transitions_after_approval(monkeypatch: pytest.MonkeyPatch,
                                                       tmp_path: Path):
    """R34: mode_request is approval-routed (MUTATING) — allow -> mode changes."""
    from langgraph.checkpoint.memory import MemorySaver
    from langgraph.types import Command

    from worker.engine.graph import build_graph

    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path))
    collector = EventCollector()
    graph = build_graph(checkpointer=MemorySaver())
    config = _config(collector)
    _patch_llm(monkeypatch, [
        _ai("", [_tc("tc1", "mode_request",
                     {"target_mode": "development", "reason": "need to edit files"})]),
        _ai("switched"),
    ])
    await graph.ainvoke(_initial(mode=__import__("worker.engine.state", fromlist=["Mode"]).Mode.PLAN), config)
    snap = await graph.aget_state(config)
    interrupts = [i for task in snap.tasks for i in task.interrupts]
    assert len(interrupts) == 1  # approval card for the transition
    result = await graph.ainvoke(Command(resume={"decision": "allow"}), config)
    mode = result.get("mode")
    assert (mode.value if hasattr(mode, "value") else mode) == "development"
