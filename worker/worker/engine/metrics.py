"""Engine ops metrics — counters/histograms, NOT OTel.

In-process registry, created per-run by the runner and carried in the graph
config. Nodes record as they go; the runner flushes a snapshot to the
platform's metrics sink at turn end (and the final snapshot lands in the
run record). Explicitly ops-level: turn latency, tool-call latency,
compaction frequency, checkpoint size, recursion_limit hits, denial rate,
stuck-loop triggers, swarm worker idle vs wall time.
"""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any


class MetricsRegistry:
    """Lock-guarded counters + histograms. Histograms keep raw samples — the
    platform sink computes percentiles; engine-side p95 needs no math lib."""

    def __init__(self, run_id: str, thread_id: str) -> None:
        self.run_id = run_id
        self.thread_id = thread_id
        self._lock = threading.Lock()
        self.counters: dict[str, float] = {}
        self.histograms: dict[str, list[float]] = {}
        self.started = time.monotonic()

    def increment(self, name: str, value: float = 1.0) -> None:
        with self._lock:
            self.counters[name] = self.counters.get(name, 0.0) + value

    def observe(self, name: str, value: float) -> None:
        with self._lock:
            self.histograms.setdefault(name, []).append(round(value, 4))

    @contextmanager
    def timed(self, name: str) -> Iterator[None]:
        started = time.monotonic()
        try:
            yield
        finally:
            self.observe(name, time.monotonic() - started)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            histograms = {}
            for name, samples in self.histograms.items():
                ordered = sorted(samples)
                # L-08: p95 used int(n*0.95) which FLOORS — for n=20 that's
                # index 19 (the max), so p95 always returned the max for small
                # samples. Use the nearest-rank method: the p95 value is the
                # ceil(0.95*n)-th sample (1-indexed) → index ceil(0.95*n)-1.
                # For n=20 that's index 18 (not the max). Clamp to [0, n-1].
                if ordered:
                    idx = max(0, math.ceil(len(ordered) * 0.95) - 1)
                    p95 = ordered[min(idx, len(ordered) - 1)]
                else:
                    p95 = None
                histograms[name] = {
                    "count": len(samples),
                    "min": ordered[0] if ordered else None,
                    "p95": p95,
                    "max": ordered[-1] if ordered else None,
                }
            return {
                "run_id": self.run_id,
                "thread_id": self.thread_id,
                "uptime_s": round(time.monotonic() - self.started, 1),
                "counters": dict(self.counters),
                "histograms": histograms,
            }


def get_registry(config: Any) -> MetricsRegistry | None:
    """Fetch the per-run registry from a graph config (None-safe: tests that
    build graphs without a runner simply skip metrics)."""
    try:
        return config["configurable"].get("metrics")
    except (KeyError, TypeError):
        return None


__all__ = ["MetricsRegistry", "get_registry"]
