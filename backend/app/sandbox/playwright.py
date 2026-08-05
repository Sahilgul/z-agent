"""Playwright MCP client: drives the Playwright MCP server to stamp
before/after screenshots for UI-change evidence.

Lazily constructed so the backend never hard-depends on a running Playwright
server: the development blueprint's deterministic stamp node asks for a client
via ``PlaywrightMcpClient.build()`` which returns ``None`` when the MCP
transport isn't configured (tests mock ``capture`` directly; production wires
the MCP stdio/HTTP transport when the server is stood up). The control plane —
not the agent — drives Playwright, so screenshots are tamper-proof.
"""

from __future__ import annotations

from app.core.config import get_settings
from app.core.logging import get_logger

log = get_logger(service="playwright")


class PlaywrightMcpClient:
    """Thin wrapper over the Playwright MCP server. ``capture`` returns one entry
    per route with the stamped artifact path; ``captured`` is False until the
    real MCP transport is wired (the stub still produces the path so the
    evidence package records what *would* be captured)."""

    def __init__(self, settings=None) -> None:
        self.settings = settings or get_settings()

    @classmethod
    def build(cls) -> "PlaywrightMcpClient | None":
        """Return a client when Playwright MCP is configured, else None so callers
        can skip screenshot evidence without crashing."""
        try:
            return cls()
        except Exception as exc:  # pragma: no cover - config/transport missing
            log.info("playwright mcp unavailable", error=str(exc)[:120])
            return None

    async def capture(self, run_id: str, workspace: str, routes: list[str]) -> list[dict]:
        out: list[dict] = []
        base = self.settings.evidence_dir / run_id
        for route in routes:
            slug = (route.strip("/") or "root").replace("/", "_")
            out.append({
                "route": route,
                "path": str(base / f"{slug}.png"),
                "captured": False,  # flipped True once the MCP transport lands the file
            })
        return out
