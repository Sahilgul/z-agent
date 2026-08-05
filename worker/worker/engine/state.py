"""Engine state schema (plan §6 state.py).

The state flows through the LangGraph graph. Every node inspects and mutates
only the keys it owns (plan §3 — context scope isolation). PromptOrigin tags
every message so compaction can keep-vs-drop with one switch (plan §9).
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from langchain_core.messages import BaseMessage
from pydantic import BaseModel
from typing_extensions import TypedDict


class PromptOrigin(str, Enum):
    """Origin of a context message — drives compaction keep-vs-drop policy."""

    SYSTEM = "system"          # the byte-stable base prompt (never dropped)
    USER = "user"              # a human message (verbatim-protected in compaction)
    ASSISTANT = "assistant"    # the model's own prior turns
    TOOL = "tool"              # tool results (prunable)
    ENVELOPE = "envelope"      # per-turn mode/env/AGENTS.md fragment (ephemeral, popped on exit)
    MEMORY = "memory"          # T2/T3 memory slices (prunable, lower priority than tool)
    NUDGE = "nudge"            # injected steering message (protected)


class Mode(str, Enum):
    ASK = "ask"
    PLAN = "plan"
    DEVELOPMENT = "development"
    DEBUG = "debug"
    GOAL = "goal"


class Autonomy(str, Enum):
    SUPERVISED = "supervised"
    GATED = "gated"
    AUTONOMOUS = "autonomous"


class Budget(BaseModel):
    used: float = 0.0
    cap: float = 5.0

    def remaining(self) -> float:
        return max(0.0, self.cap - self.used)

    def would_exceed(self, cost: float) -> bool:
        return self.used + cost > self.cap


# --- The LangGraph state (TypedDict so reducers merge per-key) ---


class EngineState(TypedDict, total=False):
    """The state that flows through the agent graph.

    Keys are added by nodes; `total=False` so partial updates are valid.
    """

    # The conversation (PromptOrigin-tagged via additional_kwargs)
    messages: list[BaseMessage]

    # Identity (plan §1 — identifiers.py)
    run_id: str
    thread_id: str
    context_id: str          # == thread_id for top-level; derived for subagents
    task_id: str             # the current turn

    # Mode + autonomy (plan §8)
    mode: Mode
    autonomy: Autonomy

    # Budget (plan §13 — gateway > thread > goal)
    budget: Budget | dict[str, Any]  # dict form: msgpack-safe (see graph.py)

    # The frozen plan artifact + live execution tracker (plan §7 update_tasks)
    plan_artifact: dict[str, Any] | None
    task_tracker: list[dict[str, Any]] | None
    # RC two-artifact task model (R24-amends-R23):
    # {"artifact": [{id, content, scope, acceptance}], "tracker": {id: status}}
    tasks: dict[str, Any]
    # RC T4 knowledge drafts staged by knowledge_draft (draft -> approve path)
    knowledge_drafts: list[dict[str, Any]]

    # Goal-mode artifact (plan §8 goal mode)
    goal_artifact: dict[str, Any] | None

    # Drift/collision flags (plan §11 team layer)
    drift_detected: bool
    collision_warning: str | None

    # Compaction bookkeeping (plan §9)
    compacted_event_ids: list[str]
    last_compaction_at: float | None
    compaction_count: int
    needs_compaction: bool        # agent node hit a context-length error
    force_compact: bool           # bypass the should_compact gate once
    compaction_retries: int       # cap forced retries (context overflow loop guard)

    # Approvals (RA — interrupt-driven gate; plan §11 Redis driver)
    approved_calls: dict[str, dict[str, Any]]  # tool_call_id -> decision record
    denial_streak: int            # consecutive denials; 3 -> blocked-escalation

    # Watchdogs (plan §13)
    tool_streak: dict[str, int]   # failing-call signature -> consecutive count
    turn_count: int
    last_usage: dict[str, Any] | None  # usage_metadata of the last AI turn

    # Goal-mode routing (RA — stage subgraph mount)
    stage_envelope: str | None    # current goal stage (drives the per-turn envelope)
    critic_iterations: int
    blocked_reason: str | None

    # Turn control
    done: bool
    error: str | None


def tag_message(msg: BaseMessage, origin: PromptOrigin | str) -> BaseMessage:
    """Tag a message with its PromptOrigin so compaction can keep-vs-drop.

    Accepts the enum or its string value (call sites historically passed raw
    strings, which crashed on `.value` — coerce instead).
    """
    if isinstance(origin, str):
        origin = PromptOrigin(origin)
    msg.additional_kwargs = {**(msg.additional_kwargs or {}), "prompt_origin": origin.value}
    return msg


__all__ = [
    "Autonomy",
    "Budget",
    "EngineState",
    "Mode",
    "PromptOrigin",
    "tag_message",
]
