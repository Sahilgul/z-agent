"""Permission glob rulesets with findLast precedence (plan §7 cross-cutting R6).

Every tool belongs to a permission CLASS (the capability map in tools/) and a
call may carry glob RULESETS that refine the class per-call. Rules match on
the tool name plus glob patterns over selected argument values (e.g. the
terminal_exec command, the file path):

  ruleset = [
    {"effect": "allow", "tool": "terminal_exec", "args": {"command": "git status*"}},
    {"effect": "ask",   "tool": "terminal_exec", "args": {"command": "rm *"}},
    {"effect": "deny",  "tool": "file_write",    "args": {"file_path": ".env*"}},
  ]

findLast precedence (donor: opencode permission.ts): the LAST matching rule
wins — later, more-specific team/user overlays refine earlier broad presets
without reordering. No match -> the capability-map default applies (R6:
"extends the capability map", never replaces it).

Effects:
  allow -> executes without a card (still audited via StepEvents)
  ask   -> the approval gate (two-phase verbatim)
  deny  -> typed error result, never reaches the gate (hard git policies §11)
"""

from __future__ import annotations

import fnmatch
from enum import Enum
from typing import Any


class Effect(str, Enum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


def _matches(rule: dict[str, Any], tool_name: str, args: dict[str, Any]) -> bool:
    tool_pat = rule.get("tool", "*")
    if not fnmatch.fnmatchcase(tool_name, tool_pat):
        return False
    for arg_name, pattern in (rule.get("args") or {}).items():
        value = args.get(arg_name)
        if value is None:
            return False
        if not fnmatch.fnmatchcase(str(value), str(pattern)):
            return False
    return True


def evaluate(tool_name: str, args: dict[str, Any] | None,
             rulesets: list[dict[str, Any]] | None) -> Effect | None:
    """findLast over the merged ruleset. Returns the winning Effect, or None
    when no rule matches (caller falls back to the capability default)."""
    if not rulesets:
        return None
    winner: Effect | None = None
    for rule in rulesets:
        if _matches(rule, tool_name, args or {}):
            try:
                winner = Effect(rule["effect"])
            except ValueError:
                continue  # malformed rule: skip, never crash a turn
    return winner


def decision_for_call(tool_name: str, args: dict[str, Any] | None,
                      rulesets: list[dict[str, Any]] | None,
                      *, capability_default_needs_approval: bool) -> tuple[Effect, bool]:
    """Fold ruleset + capability default into one gate decision.

    Returns (effect, needs_approval):
      explicit allow/deny wins; ask or (no-rule + mutating default) -> gate.
    """
    effect = evaluate(tool_name, args, rulesets)
    if effect is Effect.ALLOW:
        return Effect.ALLOW, False
    if effect is Effect.DENY:
        return Effect.DENY, False
    if effect is Effect.ASK:
        return Effect.ASK, True
    return (Effect.ASK if capability_default_needs_approval else Effect.ALLOW), \
        capability_default_needs_approval


__all__ = ["Effect", "decision_for_call", "evaluate"]
