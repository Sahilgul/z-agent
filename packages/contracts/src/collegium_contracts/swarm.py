"""Multi-thread run contracts (width fan-out; was "swarm").

Decomposition is the Lead's structured output in the decompose node: N DISTINCT
slices (by angle/module/concern — equal in count, never arithmetic clones of
the same prompt). The user states intent (count + target + goal); decomposition
and per-thread prompt authoring are the Lead's job, never the user's. When the
requested count is wasteful, the Lead counter-proposes (user may override —
user intent wins).

Note: the spawn_swarm TOOL keeps its name (it's a verb, not the concept). The
concept "swarm" is renamed to "multi-thread run" in UI chips and docs.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

SCHEMA_VERSION = 1


class SwarmSlice(BaseModel):
    schema_version: int = SCHEMA_VERSION
    title: str
    prompt: str = Field(description="Lead-authored per-thread task slice")
    repo: str | None = Field(default=None, description="Slice scope; null = run target repo")
    angle: str = Field(default="", description="What makes this slice distinct from the others")


class Decomposition(BaseModel):
    """Structured-output target for the multi-thread blueprint's decompose node."""

    schema_version: int = SCHEMA_VERSION
    slices: list[SwarmSlice] = Field(default_factory=list)
    counter_proposal: int | None = Field(
        default=None,
        description="Lead's proposed thread count when the requested count is wasteful",
    )
    rationale: str = ""
