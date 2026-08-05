"""Run stages, action vocabulary, and the UserIntent contract (plan §1a/§8).

One intent bus: buttons, classified text, chips, voice, triggers, cron, and webhooks
all feed POST /runs/{id}/intent. available_actions on the run row is the ONLY legal
move set; irreversible intents require confirmed=true.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class RunStage(str, Enum):
    QUEUED = "queued"
    PROVISIONING = "provisioning"
    INVESTIGATING = "investigating"
    PLANNING = "planning"
    AWAITING_USER = "awaiting_user"
    DEVELOPING = "developing"
    VERIFYING = "verifying"
    PR_READY = "pr_ready"
    INTERRUPTED = "interrupted"
    COMPLETED = "completed"
    FAILED = "failed"
    ABANDONED = "abandoned"


class ActionKind(str, Enum):
    # plan lifecycle
    REVIEW_PLAN = "review_plan"
    APPROVE_PLAN = "approve_plan"
    REJECT_PLAN = "reject_plan"
    # tool permission
    ALLOW_ONCE = "allow_once"
    ALWAYS_ALLOW = "always_allow"
    DENY_TOOL = "deny_tool"
    # thread control
    STOP_THREAD = "stop_thread"
    NUDGE = "nudge"
    LET_IT_RUN = "let_it_run"
    PIN_FINDING = "pin_finding"
    KILL_REPLACE = "kill_replace"
    # development / PR
    REVIEW_EVIDENCE = "review_evidence"
    CREATE_PR = "create_pr"
    REVIEW_DIFF = "review_diff"
    MERGE_PR = "merge_pr"
    # mode transitions
    START_PLANNING = "start_planning"
    MOVE_TO_DEVELOPMENT = "move_to_development"
    SWITCH_TO_AGENT_MODE = "switch_to_agent_mode"
    START_PLAN = "start_plan"
    # mid-session mode switch — takes effect on the next send_message, not
    # immediately: the current turn finishes undisturbed and the new
    # blueprint runs (respawning the lane on the prior session volume).
    SWITCH_MODE = "switch_mode"
    # run lifecycle
    STOP_RUN = "stop_run"
    ABANDON_RUN = "abandon_run"
    EDIT_AND_RESEND = "edit_and_resend"
    RESUME_RUN = "resume_run"
    SEND_MESSAGE = "send_message"
    # ideas / knowledge
    ASK_COUNSEL = "ask_counsel"
    SUMMARIZE_THREAD = "summarize_thread"
    PROMOTE_TO_PLAN = "promote_to_plan"
    APPROVE_KNOWLEDGE = "approve_knowledge"
    DISMISS_PROPOSAL = "dismiss_proposal"
    ACCEPT_PROPOSAL = "accept_proposal"


# Intents that mutate state and therefore require confirmed=true when they arrive
# as classified TEXT (buttons execute directly after their own card UX).
IRREVERSIBLE_INTENTS: frozenset[ActionKind] = frozenset(
    {ActionKind.MERGE_PR, ActionKind.ABANDON_RUN, ActionKind.KILL_REPLACE}
)


class IntentSource(str, Enum):
    BUTTON = "button"
    TEXT = "text"
    CHIP = "chip"
    VOICE = "voice"
    TRIGGER = "trigger"
    CRON = "cron"
    WEBHOOK = "webhook"


class UserIntent(BaseModel):
    schema_version: int = 1
    run_id: str
    intent: ActionKind
    source: IntentSource = IntentSource.BUTTON
    thread_id: str | None = None
    text: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    confirmed: bool = False
