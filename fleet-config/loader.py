"""fleet-config loader (plan §5 Layer 0 + §7 registry).

stdlib-only on purpose: importable by scripts AND the backend without dragging
dependencies. Two artifacts:
  - repos list (from hami-repos.json) -> seeds the `repos` DB table (bootstrap
    SEED for the initial 10; the DB row is the LIVE registry afterwards).
  - FleetGraph (from hami-services.json) -> in-memory Layer 0 inter-repo system
    map, loaded at startup, edited via PR, NEVER auto-generated.

Ownership split: machine-manageable data in the DB, human judgment in files.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class RepoSpec:
    name: str
    local_path: str
    integration_branch: str
    remote: str = "origin"
    stack: str = ""
    notes: str = ""


@dataclass(frozen=True)
class ServiceEdge:
    to: str
    mechanism: str
    citation: str


@dataclass(frozen=True)
class ServiceNode:
    name: str
    stack: str
    role: str
    calls: tuple[ServiceEdge, ...] = ()
    called_by: tuple[str, ...] = ()
    notes: str = ""


@dataclass
class FleetGraph:
    call_chain: str
    live_services: list[str]
    legacy_services: list[str]
    services: dict[str, ServiceNode]
    shared_stores: dict[str, dict]
    blast_radius_rules: list[str]
    unconfirmed_from_code: list[str] = field(default_factory=list)

    def blast_radius_for(self, service: str) -> list[str]:
        """Downstream services affected by a change to `service` (reverse of calls)."""
        affected: set[str] = set()
        frontier = [service]
        seen = {service}
        while frontier:
            current = frontier.pop()
            for node in self.services.values():
                if any(edge.to == current for edge in node.calls) and node.name not in seen:
                    affected.add(node.name)
                    seen.add(node.name)
                    frontier.append(node.name)
        return sorted(affected)

    def condensed_for_prompt(self) -> str:
        """Condensed callChain + rules for Lead/planner/Debug prompts ONLY —
        never injected into single-repo specialist lanes (plan §5)."""
        rules = "\n".join(f"- {r}" for r in self.blast_radius_rules)
        return f"Fleet call chain: {self.call_chain}\nBlast-radius rules:\n{rules}"


def load_repos(config_dir: Path) -> list[RepoSpec]:
    data = json.loads((config_dir / "hami-repos.json").read_text(encoding="utf-8"))
    return [
        RepoSpec(
            name=r["name"],
            local_path=r.get("path", ""),
            integration_branch=r["integrationBranch"],
            remote=r.get("remote", "origin"),
            stack=r.get("stack", ""),
            notes=r.get("notes", ""),
        )
        for r in data["repos"]
    ]


def load_fleet_graph(config_dir: Path) -> FleetGraph:
    data = json.loads((config_dir / "hami-services.json").read_text(encoding="utf-8"))
    services = {
        s["name"]: ServiceNode(
            name=s["name"],
            stack=s.get("stack", ""),
            role=s.get("role", ""),
            calls=tuple(
                ServiceEdge(to=c["to"], mechanism=c.get("mechanism", ""), citation=c.get("citation", ""))
                for c in s.get("calls", [])
            ),
            called_by=tuple(s.get("calledBy", [])),
            notes=s.get("notes", ""),
        )
        for s in data.get("services", [])
    }
    return FleetGraph(
        call_chain=data.get("callChain", ""),
        live_services=data.get("liveServices", []),
        legacy_services=data.get("legacyServicesStillPartiallyLive", []),
        services=services,
        shared_stores=data.get("sharedStores", {}),
        blast_radius_rules=data.get("blastRadiusRules", []),
        unconfirmed_from_code=data.get("unconfirmedFromCode", []),
    )
