"""Structured Plan output (Plan mode's output_format json_schema target) and the
lane Notebook schema specialists report back to the Lead.
"""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field


class PlanStepStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"


class PlanStep(BaseModel):
    index: int = Field(ge=0)
    title: str
    description: str
    repo: str | None = None
    files: list[str] = Field(default_factory=list)
    success_criterion: str = Field(description="Verifiable by the deterministic evidence nodes")
    status: PlanStepStatus = PlanStepStatus.PENDING


class Plan(BaseModel):
    """The planner-critic bundle's structured output. The approval card, the
    evidence collector, and the fresh-context evaluator all consume this shape."""

    schema_version: int = 1
    title: str
    summary: str
    steps: list[PlanStep]
    blast_radius: list[str] = Field(
        default_factory=list,
        description="Services flagged by the fleet graph (Layer 0) hydration",
    )
    risks: list[str] = Field(default_factory=list)
    evidence_contract: list[str] = Field(
        default_factory=list,
        description="What the run must produce: tests passing, diff summary, CI green, screenshots",
    )


class Evidence(BaseModel):
    file: str
    line_start: int = Field(ge=1)
    line_end: int = Field(ge=1)
    note: str = ""


class Notebook(BaseModel):
    """Structured report from a specialist lane to the Lead (Pydantic-validated)."""

    schema_version: int = 1
    findings: list[str] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    confidence: Literal["high", "medium", "low"] = "medium"
    open_questions: list[str] = Field(default_factory=list)
