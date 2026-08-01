"""API surface — builds a Service."""

from python_repo.pkg.core import Service, helper


def build() -> Service:
    """Construct a Service instance."""
    helper(1, 2)
    return Service()
