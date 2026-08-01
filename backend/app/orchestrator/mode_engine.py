"""Mode engine: resolves a mode name to its blueprint (plan §6 — modes as DB rows
+ one blueprint file per mode).

Resolution order (plan §6 Phase 2):
  1. The Mode DB row's ``topology`` column selects the blueprint (modes are data:
     persona/permissions/playbooks live on the row; topology stays code).
  2. Fallback: the mode ``name`` equals a blueprint name (Phase 1 ask-mode path).

The DB lookup degrades to the name fallback when the modes table is unavailable
(pre-migration boot, isolated unit tests that never swap the global engine) so
the resolver never hard-fails on a missing table — a missing row is the only
real error and surfaces as KeyError.
"""

from __future__ import annotations

from app.orchestrator.blueprints.ask import AskBlueprint
from app.orchestrator.blueprints.base import Blueprint
from app.orchestrator.blueprints.debug import DebugBlueprint
from app.orchestrator.blueprints.development import DevelopmentBlueprint
from app.orchestrator.blueprints.plan import PlanBlueprint
from app.orchestrator.blueprints.swarm import SwarmBlueprint

BLUEPRINTS: dict[str, type[Blueprint]] = {
    "ask": AskBlueprint,
    "plan": PlanBlueprint,
    "development": DevelopmentBlueprint,
    "debug": DebugBlueprint,
    "width-swarm": SwarmBlueprint,
}


def _topology_for(mode_name: str) -> str | None:
    """Read the Mode row's topology, or None when the table/row is unavailable."""
    try:
        from app.db.base import get_session
        from app.db.models.mode import Mode

        session = get_session()
        try:
            row = session.query(Mode).filter_by(name=mode_name).one_or_none()
            return row.topology if row is not None else None
        finally:
            session.close()
    except Exception:
        return None


def blueprint_for(mode_name: str) -> Blueprint:
    key = None
    topology = _topology_for(mode_name)
    if topology in BLUEPRINTS:
        key = topology
    if key is None and mode_name in BLUEPRINTS:
        key = mode_name
    if key is None:
        raise KeyError(f"no blueprint registered for mode '{mode_name}'")
    return BLUEPRINTS[key]()
