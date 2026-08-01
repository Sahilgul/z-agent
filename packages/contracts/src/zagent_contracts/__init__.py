"""zagent-contracts: the single schema package installed by BOTH backend and worker.
Frontend TS types in apps/web/src/types mirror these by hand.
"""

from zagent_contracts.events import SCHEMA_VERSION, StepEvent, StepKind, TypingDelta
from zagent_contracts.intents import (
    IRREVERSIBLE_INTENTS,
    ActionKind,
    IntentSource,
    RunStage,
    UserIntent,
)
from zagent_contracts.plan import Evidence, Notebook, Plan, PlanStep, PlanStepStatus
from zagent_contracts.swarm import Decomposition, SwarmSlice
from zagent_contracts.triggers import TriggerEvent, TriggerSource

__all__ = [
    "SCHEMA_VERSION",
    "StepEvent",
    "StepKind",
    "TypingDelta",
    "ActionKind",
    "IntentSource",
    "RunStage",
    "UserIntent",
    "IRREVERSIBLE_INTENTS",
    "Plan",
    "PlanStep",
    "PlanStepStatus",
    "Notebook",
    "Evidence",
    "TriggerEvent",
    "TriggerSource",
    "Decomposition",
    "SwarmSlice",
]
