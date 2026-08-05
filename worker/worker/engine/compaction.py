"""Compaction — prune → summarize → splice.

The context window is a scarce resource. Compaction keeps it healthy without
lying about what was removed. Three stages, in order:

1. PRUNE — drop low-value messages by PromptOrigin. Tool outputs (long, low
   signal-to-noise) are pruned first; envelopes (per-turn fragments) are
   popped on exit; memory slices are pruned before user/assistant turns.
   System + user + nudge are NEVER pruned (verbatim-protected).

2. SUMMARIZE — the pruned span is condensed into ONE assistant message tagged
   origin=memory, carrying a structured summary (what was investigated, what
   was found, what was decided). The summary is the ONLY place compaction is
   allowed to lose fidelity — and it's honest about it (the compaction card).

3. SPLICE — replace the pruned span with the summary in the message list.

The HONESTY VALIDATOR runs after splice: it asserts that no verbatim-protected
message (system, user, nudge) was dropped, and that the summary carries the
compaction marker. A failed validator rolls back the compaction (the
conversation is left intact and the context-limit breach is reported instead).

PromptOrigin drives keep-vs-drop with one switch per origin.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from langchain_core.messages import BaseMessage

from worker.engine.state import PromptOrigin

# Origins that are NEVER pruned (verbatim-protected).
_PROTECTED_ORIGINS = frozenset({
    PromptOrigin.SYSTEM, PromptOrigin.USER, PromptOrigin.NUDGE,
})

# Origins pruned in order (most expendable first).
_PRUNE_ORDER = [PromptOrigin.TOOL, PromptOrigin.ENVELOPE, PromptOrigin.MEMORY, PromptOrigin.ASSISTANT]


def _repair_tool_pairs(msgs: list[BaseMessage]) -> list[BaseMessage]:
    """Restore tool-call/tool-result pairing after pruning.

    The origin-bucket prune can split a pair: an AIMessage(tool_calls) is
    pruned (ASSISTANT origin) while its ToolMessage survives in the recent
    window (or vice versa via floor restoration). OpenAI-compatible gateways
    REJECT a ToolMessage whose tool_call_id has no matching tool_call, and
    an AIMessage carrying tool_calls with no ToolMessage responses — both
    surface as a 400 that does NOT match the context-overflow signatures,
    terminally failing the run. Repair both directions:
      1. drop ToolMessages whose issuing AIMessage is gone;
      2. strip tool_calls from AIMessages whose ToolMessages are gone.
    Only TOOL/ASSISTANT-origin messages are touched, never protected ones.
    """
    from langchain_core.messages import ToolMessage

    call_ids: set[str] = set()
    for m in msgs:
        for tc in getattr(m, "tool_calls", None) or []:
            cid = tc.get("id")
            if cid:
                call_ids.add(cid)

    if not call_ids:
        # No tool calls anywhere in the kept list — there is nothing to pair
        # against (e.g. synthetic/prune-only conversations). Enforcing here
        # would drop every ToolMessage and break the output floor; leave
        # the list untouched.
        return msgs

    answered: set[str] = set()
    kept: list[BaseMessage] = []
    for m in msgs:
        if isinstance(m, ToolMessage):
            if m.tool_call_id and m.tool_call_id not in call_ids:
                continue  # orphaned result — the issuing AIMessage was pruned
            if m.tool_call_id:
                answered.add(m.tool_call_id)
        kept.append(m)

    final: list[BaseMessage] = []
    for m in kept:
        tcs = getattr(m, "tool_calls", None) or []
        if tcs and any(tc.get("id") not in answered for tc in tcs):
            live = [tc for tc in tcs if tc.get("id") in answered]
            # Unanswered calls are stripped (their results were pruned); the
            # message keeps its content so the conversation still reads.
            m = m.model_copy(update={"tool_calls": live})
        final.append(m)
    return final


@dataclass
class CompactionResult:
    """The outcome of one compaction pass — emitted as a compaction-card event."""
    pruned_count: int = 0
    summarized_count: int = 0
    kept_count: int = 0
    summary: str = ""
    rolled_back: bool = False
    rollback_reason: str = ""
    before_tokens: int = 0
    after_tokens: int = 0


@dataclass
class CompactionPolicy:
    """When + how hard to compact. Self-tuning."""
    # The soft limit (tokens). The agent compacts BEFORE breaching it.
    context_limit: int = 120_000
    # The floor — never compact below this many messages (keep recent context).
    floor_messages: int = 20
    # How many recent messages are immune to pruning (the recent window).
    recent_window: int = 10
    # The honest compaction marker, embedded in every summary.
    marker: str = "[compacted]"

    def should_compact(self, message_count: int, token_count: int) -> bool:
        # L-06: use >= so compaction triggers AT the context limit (before
        # breaching it), matching the "compacts BEFORE breaching" intent.
        # The old `>` only compacted once token_count strictly exceeded the
        # limit, so the very next turn ran over budget before compaction.
        return token_count >= self.context_limit and message_count > (self.floor_messages + self.recent_window)


class Compactor:
    """Prune → summarize → splice, with the honesty validator."""

    def __init__(self, policy: CompactionPolicy | None = None,
                 summarizer: Any = None) -> None:
        self.policy = policy or CompactionPolicy()
        # The summarizer is an async callable (str) -> str. If None, compaction
        # prunes only (no summarize stage) — the safe default until the LLM
        # summarizer is wired (self-tuning wires it).
        self.summarizer = summarizer

    def _origin_of(self, msg: BaseMessage) -> PromptOrigin:
        origin = (msg.additional_kwargs or {}).get("prompt_origin", "assistant")
        try:
            return PromptOrigin(origin)
        except ValueError:
            return PromptOrigin.ASSISTANT

    def _estimate_tokens(self, messages: list[BaseMessage]) -> int:
        # Cheap estimate: 1 token ~ 4 chars. Good enough for the should_compact
        # gate; the real count comes from the gateway usage on each turn.
        # L-07: content can be a list of blocks (e.g. [{"type": "text",
        # "text": "..."}]) — str(list) would count the repr's brackets,
        # quotes, and "type"/"text" keys and over-count by ~30%. Extract
        # the real text from block content instead.
        total = 0
        for m in messages:
            content = getattr(m, "content", "")
            if isinstance(content, str):
                total += len(content)
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict):
                        total += len(str(block.get("text", "") or block.get("content", "") or ""))
                    else:
                        total += len(str(block))
            else:
                total += len(str(content))
        return total // 4

    async def compact(self, messages: list[BaseMessage], *, force: bool = False) -> tuple[list[BaseMessage], CompactionResult]:
        """Run prune → summarize → splice. Returns (new messages, result).

        If the honesty validator fails, returns the ORIGINAL messages unchanged
        with rolled_back=True. `force` bypasses the should_compact gate (the
        context-overflow retry path — the floor and recent window still hold).
        """
        result = CompactionResult(before_tokens=self._estimate_tokens(messages))

        if not force and not self.policy.should_compact(len(messages), result.before_tokens):
            result.kept_count = len(messages)
            result.after_tokens = result.before_tokens
            return messages, result

        # Forced path guard: nothing prunable outside the recent window means
        # compaction cannot help — report honestly instead of adding noise.
        if force and len(messages) <= self.policy.recent_window:
            result.kept_count = len(messages)
            result.after_tokens = result.before_tokens
            result.summary = f"{self.policy.marker} nothing prunable (all within recent window)"
            return messages, result

        # 1. PRUNE — identify the prunable span (everything outside the recent
        # window, by origin, in prune order).
        recent_start = max(0, len(messages) - self.policy.recent_window)
        # Never open the recent window with ToolMessage(s): their issuing
        # AIMessage would sit in the prunable span and the pair would be
        # split (orphaned tool result -> gateway 400 on the next turn).
        from langchain_core.messages import ToolMessage as _ToolMessage
        while recent_start > 0 and isinstance(messages[recent_start], _ToolMessage):
            recent_start -= 1
        span = messages[:recent_start]
        recent = messages[recent_start:]

        # Sort the span into origin buckets; protected origins stay.
        buckets: dict[PromptOrigin, list[tuple[int, BaseMessage]]] = {}
        for i, msg in enumerate(span):
            origin = self._origin_of(msg)
            buckets.setdefault(origin, []).append((i, msg))

        pruned: list[tuple[int, BaseMessage]] = []
        kept_in_span: list[tuple[int, BaseMessage]] = []
        for origin in _PRUNE_ORDER:
            if origin not in buckets:
                continue
            pruned.extend(buckets[origin])
            del buckets[origin]

        # Any remaining buckets (protected origins) are kept.
        for items in buckets.values():
            kept_in_span.extend(items)
        kept_in_span.sort(key=lambda x: x[0])

        # H-08: floor_messages is a floor on the OUTPUT, not just the
        # trigger. The old code could collapse below the floor when most of
        # the span was prunable (e.g. 30 TOOL messages, floor=20, recent=10
        # -> output = 1 summary + 10 recent = 11 < floor). Restore the most
        # recent pruned messages until the projected output meets the floor
        # (or pruned is exhausted — the best-effort case keeps everything).
        min_output = self.policy.floor_messages
        projected = len(kept_in_span) + len(recent) + 1  # +1 for the summary
        if projected < min_output and pruned:
            deficit = min_output - projected
            pruned.sort(key=lambda x: x[0])
            restored = pruned[-deficit:] if deficit <= len(pruned) else pruned
            kept_in_span.extend(restored)
            pruned = pruned[:-deficit] if deficit <= len(pruned) else []
            kept_in_span.sort(key=lambda x: x[0])

        result.pruned_count = len(pruned)
        result.kept_count = len(kept_in_span) + len(recent)

        # 2. SUMMARIZE — condense the pruned span into one message.
        if self.summarizer is None:
            # Prune-only: drop the pruned messages, keep a marker.
            summary_text = f"{self.policy.marker} {result.pruned_count} messages pruned (no summarizer)."
        else:
            pruned_text = "\n\n".join(str(getattr(m, "content", ""))[:500] for _, m in pruned)
            # H-07: the summarizer may be sync OR async. The old code called it
            # synchronously, so an async summarizer returned a coroutine which
            # was then str()'d into the conversation memory ("<coroutine object
            # at 0x...>"). Await it when it returns a coroutine; use the value
            # directly when it doesn't.
            maybe = self.summarizer(pruned_text)
            if asyncio.iscoroutine(maybe):
                summary_text = await maybe
            else:
                summary_text = maybe or ""
            summary_text = f"{self.policy.marker} {summary_text}"
        result.summary = summary_text

        # 3. SPLICE — build the new list: kept-span + summary + recent.
        from langchain_core.messages import AIMessage
        summary_msg = AIMessage(content=summary_text)
        summary_msg.additional_kwargs = {"prompt_origin": PromptOrigin.MEMORY.value}
        new_messages = [m for _, m in kept_in_span] + [summary_msg] + recent
        new_messages = _repair_tool_pairs(new_messages)
        result.kept_count = len(new_messages) - 1  # exclude the summary message
        result.after_tokens = self._estimate_tokens(new_messages)
        result.summarized_count = 1

        # HONESTY VALIDATOR — no protected message was dropped OR replaced.
        protected_before = [m for m in messages if self._origin_of(m) in _PROTECTED_ORIGINS]
        # M-16: the old validator compared COUNTS only, so a protected message
        # REPLACED by a different protected message (same count) passed —
        # the original verbatim content was silently dropped. Require each
        # protected message to survive as the SAME object (identity), not just
        # a count match.
        missing = [m for m in protected_before
                   if not any(m is nm for nm in new_messages)]
        if missing:
            result.rolled_back = True
            result.rollback_reason = (
                f"protected message changed/dropped: {len(protected_before)} protected, "
                f"{len(missing)} missing after compaction"
            )
            result.after_tokens = result.before_tokens
            return messages, result

        return new_messages, result


# --- Self-tuning context limit ---

@dataclass
class SelfTuningLimit:
    """Adjusts the context limit based on observed model errors.

    If the model 400s on context length (observed via the gateway), the limit
    tightens. If N turns pass without error at the current limit, it relaxes
    toward the configured max. The floor prevents it from collapsing.
    """
    initial: int = 120_000
    floor: int = 32_000
    ceiling: int = 200_000
    step_down: int = 8_000
    step_up: int = 4_000
    healthy_turns_threshold: int = 20

    def __post_init__(self) -> None:
        self._current = self.initial
        self._healthy_turns = 0

    @property
    def current(self) -> int:
        return self._current

    def observe_error(self, error: str) -> None:
        """A context-length error was observed — tighten the limit."""
        if "context" in error.lower() and "length" in error.lower():
            self._current = max(self.floor, self._current - self.step_down)
            self._healthy_turns = 0

    def observe_healthy_turn(self) -> None:
        """A turn completed without context error — count toward relaxation."""
        self._healthy_turns += 1
        if self._healthy_turns >= self.healthy_turns_threshold:
            self._current = min(self.ceiling, self._current + self.step_up)
            self._healthy_turns = 0


__all__ = [
    "CompactionPolicy",
    "CompactionResult",
    "Compactor",
    "SelfTuningLimit",
]
