"""collegium-contracts: the single schema package installed by BOTH backend and worker.
Frontend TS types in apps/web/src/types mirror these by hand.
"""

from collegium_contracts.events import SCHEMA_VERSION, StepEvent, StepKind, TypingDelta
from collegium_contracts.identifiers import IDENTIFIER_LAYERS
from collegium_contracts.intents import (
    IRREVERSIBLE_INTENTS,
    ActionKind,
    IntentSource,
    RunStage,
    UserIntent,
)
from collegium_contracts.plan import Evidence, Notebook, Plan, PlanStep, PlanStepStatus
from collegium_contracts.swarm import Decomposition, SwarmSlice
from collegium_contracts.triggers import TriggerEvent, TriggerSource

__all__ = [
    "SCHEMA_VERSION",
    "StepEvent",
    "StepKind",
    "TypingDelta",
    "IDENTIFIER_LAYERS",
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
