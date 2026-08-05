"""Permission glob rulesets with findLast precedence.

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
without reordering. No match -> the capability-map default applies
("extends the capability map", never replaces it).

Effects:
  allow -> executes without a card (still audited via StepEvents)
  ask   -> the approval gate (two-phase verbatim)
  deny  -> typed error result, never reaches the gate (hard git policies)
"""

from __future__ import annotations

import fnmatch
import re
from enum import Enum
from typing import Any

# Shell statement boundaries a glob `*` must never cross. fnmatch's `*`
# matches EVERY character (including `;`, `&`, `|`, newlines), so an allow
# rule like "git status*" would otherwise also match "git status; rm -rf /".
_SHELL_METACHARS = re.compile(r"[;&|`$\n]|\$\(")


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
        # Fail-closed against glob injection: a wildcarded pattern must not
        # match a value that crosses a shell-statement boundary. A non-match
        # here falls back to the capability default (the gate), never allow.
        if ("*" in str(pattern) or "?" in str(pattern)) and _SHELL_METACHARS.search(str(value)):
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
            # M-18: a rule missing the `effect` key used to raise KeyError
            # here, which escapes the `except ValueError` and crashes the
            # turn — contradicting the docstring ("malformed rule: skip,
            # never crash a turn"). Read defensively and skip missing/None.
            effect = rule.get("effect")
            if effect is None:
                continue  # malformed rule (no effect): skip
            try:
                winner = Effect(effect)
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
