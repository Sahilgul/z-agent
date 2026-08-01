"""P0 validation smoke: fleet loader, models, gateway client import."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "fleet-config"))
sys.path.insert(0, str(ROOT / "backend"))

from loader import load_repos, load_fleet_graph  # noqa: E402

repos = load_repos(ROOT / "fleet-config")
graph = load_fleet_graph(ROOT / "fleet-config")

print(f"repos: {len(repos)}")
for r in repos:
    print(f"  {r.name:<22} -> origin/{r.integration_branch}")
print(f"services: {len(graph.services)}, rules: {len(graph.blast_radius_rules)}, unknowns: {len(graph.unconfirmed_from_code)}")
print("blast radius of Billing-Engine:", graph.blast_radius_for("Billing-Engine"))
print("blast radius of ServerApp:", graph.blast_radius_for("ServerApp"))

from app.db.base import Base  # noqa: E402
from app.db import models  # noqa: F401,E402
print(f"tables: {len(Base.metadata.tables)}")

from app.gateway.litellm import GatewayClient  # noqa: E402
from app.ado.client import AdoClient  # noqa: E402
from app.sandbox.fetcher import fetch_all  # noqa: E402
print("adapters import OK")
