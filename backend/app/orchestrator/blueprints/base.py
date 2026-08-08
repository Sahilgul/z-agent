"""Blueprint base (Stripe Minions pattern).

Each mode is a state machine mixing DETERMINISTIC nodes (fetch/stamp/lint/test/
push/PR-create/evidence-collect — pure code, zero LLM) and AGENTIC nodes
(investigate/plan/implement/fix). The control plane — not the agent — runs tests
and collects evidence, so evidence is tamper-proof and agents can't skip lint or
misreport tests. A new mode = one blueprint file + one DB row.
"""

from __future__ import annotations

import abc
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from collegium_contracts import RunStage

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


def lane_override(ctx: BlueprintContext) -> tuple[str | None, str | None]:
    """The composer's (model, reasoning) choice for non-compare modes.

    create_run validation guarantees non-ask modes carry AT MOST one model,
    so every thread the blueprint spawns takes the same override. Returns
    (None, None) when the user didn't choose — the spawn then falls back to
    the deployment default model with provider-default thinking."""
    models = ctx.artifacts.get("models") or []
    model = models[0] if models else None
    reasoning_map = ctx.artifacts.get("reasoning") or {}
    if model is not None:
        return model, reasoning_map.get(model)
    # No selection: a reasoning entry may still target the default model.
    from app.core.config import get_settings
    default = get_settings().gateway_model
    return None, reasoning_map.get(default)


def media_args(ctx: BlueprintContext) -> dict:
    """The run's image attachments as spawn kwargs: {"images", "image_notes"}.

    Blueprints splat this into thread_manager.spawn alongside the model
    override; spawn does the vision/blind routing (native image staging for
    Kimi lanes, description-in-prompt for the rest). Empty dict when the run
    has no attachments."""
    paths = ctx.artifacts.get("image_paths")
    if not paths:
        return {}
    return {"images": paths, "image_notes": ctx.artifacts.get("image_notes")}


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
