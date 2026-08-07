"""Collegium custom engine.

Replaces the Claude Agent SDK coupling with a LangGraph StateGraph that talks
to the LiteLLM gateway over the OpenAI-compatible protocol. The spike
validated the gateway fidelity (a–g); this is the production loop.

Layout:
  state.py        — EngineState, PromptOrigin, Mode, Budget
  llm.py          — ChatOpenAI via gateway, retry/backoff, capability registry
  security.py     — secrets redaction + quarantine
  events.py       — engine state -> canonical StepEvents (the event contract)
  checkpointer.py — LangGraph persistence + DeltaChannel JSONL mirror
  graph.py        — the StateGraph (agent -> tools -> agent ... -> end)
  tools/          — read-only tools; mutating + approvals
  prompts/        — system_prompt.md + per-turn envelopes
"""

from __future__ import annotations

from worker.engine.approvals import ApprovalBroker, ApprovalGate
from worker.engine.checkpointer import (
    DeltaChannel,
    make_checkpointer,
    open_checkpointer,
)
from worker.engine.compaction import (
    CompactionPolicy,
    CompactionResult,
    Compactor,
    SelfTuningLimit,
)
from worker.engine.events import EventEmitter
from worker.engine.graph import build_graph
from worker.engine.llm import estimate_cost, get_capabilities, make_llm
from worker.engine.mcp import MCPManager, mcp_manager
from worker.engine.memory import (
    EpisodicMemory,
    memory_search,
    read_handoff,
    set_episodic_memory,
)
from worker.engine.metrics import MetricsRegistry
from worker.engine.permissions import Effect
from worker.engine.state import (
    Autonomy,
    Budget,
    EngineState,
    Mode,
    PromptOrigin,
    tag_message,
)
from worker.engine.tools import resolve_tool_name, tools_for_mode

__all__ = [
    "ApprovalBroker",
    "ApprovalGate",
    "Autonomy",
    "Budget",
    "CompactionPolicy",
    "CompactionResult",
    "Compactor",
    "DeltaChannel",
    "Effect",
    "EngineState",
    "EpisodicMemory",
    "EventEmitter",
    "MCPManager",
    "MetricsRegistry",
    "Mode",
    "PromptOrigin",
    "SelfTuningLimit",
    "build_graph",
    "estimate_cost",
    "get_capabilities",
    "make_checkpointer",
    "make_llm",
    "mcp_manager",
    "memory_search",
    "open_checkpointer",
    "read_handoff",
    "resolve_tool_name",
    "set_episodic_memory",
    "tag_message",
    "tools_for_mode",
]
