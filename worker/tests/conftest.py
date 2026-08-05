"""Pytest config for the worker package."""
import sys
from pathlib import Path

# Make `worker/` importable as top-level (spike.*, worker.*) for tests.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
