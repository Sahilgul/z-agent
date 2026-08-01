"""Core service — the thing everything else builds on."""

from __future__ import annotations


class Service:
    """A service that runs a value through a transform."""

    def run(self, x: int) -> int:
        return x + 1


def helper(a: int, b: int) -> int:
    """Add two numbers and return the sum."""
    return a + b
