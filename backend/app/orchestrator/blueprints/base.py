"""Blueprint base (Stripe Minions pattern).

Each mode is a state machine mixing DETERMINISTIC nodes (fetch/stamp/lint/test/
push/PR-create/evidence-collect — pure code, zero LLM) and AGENTIC nodes
(investigate/plan/implement/fix). The control plane — not the agent — runs tests
and collects evidence, so evidence is tamper-proof and agents can't skip lint or
misreport tests. A new mode = one blueprint file + one DB row.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from zagent_contracts import RunStage

from app.db.models.run import Run
from app.services.runs import transition


@dataclass
class BlueprintContext:
    """Everything a node needs: the run row, plus lazy service handles set by
    the mode engine. No FastAPI imports anywhere below this line."""
    run: Run
    services: dict = field(default_factory=dict)
    artifacts: dict = field(default_factory=dict)  # node outputs passed forward


@dataclass
class Node:
    name: str
    fn: Callable[[BlueprintContext], Awaitable[None]]
    deterministic: bool
    stage: RunStage | None = None  # stage to enter before running this node


class Blueprint(abc.ABC):
    """ONE FILE PER MODE. Nodes run in order; a node raising marks the run failed
    (resumable via the session volume)."""

    name: str = "base"

    @abc.abstractmethod
    def nodes(self) -> list[Node]:
        ...

    async def execute(self, ctx: BlueprintContext) -> None:
        from app.db.base import get_session

        for node in self.nodes():
            if node.stage is not None:
                session = get_session()
                try:
                    run = session.get(Run, ctx.run.id)
                    transition(run, node.stage)
                    session.commit()
                    # M-49: commit expires run's attributes (expire_on_commit
                    # is on by default); close() then detaches it, so reading
                    # ctx.run.available_actions below used to hit a
                    # DetachedInstanceError on the expired+detached instance.
                    # Refresh (reload attrs) then expunge (detach with attrs
                    # intact) so ctx.run is safe to read after close.
                    session.refresh(run)
                    session.expunge(run)
                    ctx.run = run
                finally:
                    session.close()
                relay = ctx.services.get("relay")
                if relay:
                    await relay.publish_run_stage(ctx.run.id, node.stage.value,
                                                  ctx.run.available_actions)
            await node.fn(ctx)
