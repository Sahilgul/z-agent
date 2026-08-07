"""Backend-side fleet-config access: loads the repos seed + the in-memory Layer 0
fleet graph at startup via fleet-config/loader.py (path from settings).
"""

from __future__ import annotations

import importlib.util
import sys
from functools import lru_cache
from pathlib import Path
from types import ModuleType

from app.core.config import get_settings


def _load_loader_module(config_dir: Path) -> ModuleType:
    loader_path = config_dir / "loader.py"
    spec = importlib.util.spec_from_file_location("collegium_fleet_loader", loader_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"fleet-config loader not found at {loader_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("collegium_fleet_loader", module)
    spec.loader.exec_module(module)
    return module


@lru_cache
def get_fleet_config():
    """Returns (repos, fleet_graph) — loaded once per process."""
    settings = get_settings()
    module = _load_loader_module(settings.fleet_config_dir)
    repos = module.load_repos(settings.fleet_config_dir)
    graph = module.load_fleet_graph(settings.fleet_config_dir)
    return repos, graph
