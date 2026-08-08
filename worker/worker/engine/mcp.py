"""MCP integration (mcp_engine).

Catalog snapshot at startup + runtime re-discovery; lazy auth (once, on
needsAuth/401); connect retry 250ms/1s x3; per-server isolation + timeouts;
PARTIAL SUCCESS: results are per-server — one server's failure never
fails the batch, failures are their own typed results.

MCP tools surface as `mcp__<server>__<tool>` and are NEVER bound by default
— they fold into the tool_search index; here they are resolvable
via the registry so discovery can load them.

Server config comes from env MCP_SERVERS (JSON list):
  [{"name": "docs", "transport": "stdio", "command": "uvx", "args": ["docs-mcp"],
    "env": {"KEY": "..."}},
   {"name": "web", "transport": "streamable_http", "url": "http://host/mcp"}]
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any

CONNECT_RETRY_DELAYS = (0.25, 1.0, 1.0)  # 250ms/1s x3
CALL_TIMEOUT_S = 60.0


class MCPServerStatus:
    def __init__(self, name: str) -> None:
        self.name = name
        self.connected = False
        self.needs_auth = False
        self.error: str | None = None
        self.tools: dict[str, Any] = {}  # exposed name -> tool


class MCPManager:
    """Per-server isolated MCP client pool with partial-success semantics."""

    def __init__(self, servers: list[dict[str, Any]] | None = None) -> None:
        self.servers = servers or _servers_from_env()
        self.status: dict[str, MCPServerStatus] = {
            s.get("name", f"server-{i}"): MCPServerStatus(s.get("name", f"server-{i}"))
            for i, s in enumerate(self.servers)
        }
        self._clients: dict[str, Any] = {}
        self._authed: set[str] = set()

    # --- catalog snapshot + re-discovery ---

    async def refresh(self, server: str | None = None) -> dict[str, Any]:
        """(Re)load tool catalogs. Per-server results — partial success:
        one server's failure is reported, never fails the batch."""
        names = [server] if server else list(self.status)
        results: dict[str, Any] = {}
        for name in names:
            results[name] = await self._refresh_one(name)
        return results

    async def _refresh_one(self, name: str) -> dict[str, Any]:
        st = self.status.get(name)
        # Use the SAME name fallback as __init__ (server-{i}) — the old
        # `s.get("name", "")` lookup never matched a nameless config, so a
        # server without an explicit `name` was permanently unreachable.
        cfg = next(
            (s for i, s in enumerate(self.servers) if (s.get("name") or f"server-{i}") == name),
            None,
        )
        if st is None or cfg is None:
            return {"kind": "error", "ok": False, "error": f"unknown server {name}"}
        last_error: str | None = None
        for attempt, delay in enumerate([0.0, *CONNECT_RETRY_DELAYS]):
            if delay:
                await asyncio.sleep(delay)
            try:
                tools = await asyncio.wait_for(self._list_tools(name, cfg), timeout=CALL_TIMEOUT_S)
                st.connected = True
                st.error = None
                st.tools = {f"mcp__{name}__{getattr(t, 'name', str(t))}": t for t in tools}
                return {"kind": "success", "ok": True, "tools": sorted(st.tools),
                        "attempts": attempt + 1}
            except Exception as exc:
                last_error = str(exc)
                # M-19: a dead MCP subprocess leaves a stale cached client
                # here; every retry reused it -> permanent outage (the
                # server could never recover until the worker restarted). Drop
                # the cache so the next attempt builds a fresh client.
                self._clients.pop(name, None)
                if _is_auth_error(exc) and name not in self._authed:
                    st.needs_auth = True
                    try:
                        await self._lazy_auth(name, cfg)
                    except Exception as auth_exc:
                        # A failing auth hook must not escape the retry loop
                        # and crash the whole batch refresh — the contract is
                        # "one server's failure never fails the batch".
                        last_error = f"{last_error}; auth hook failed: {auth_exc}"
                    continue
        st.connected = False
        st.error = last_error
        return {"kind": "error", "ok": False, "server": name, "error": last_error}

    async def _list_tools(self, name: str, cfg: dict[str, Any]) -> list[Any]:
        client = await self._client(name, cfg)
        return await client.get_tools()

    async def _client(self, name: str, cfg: dict[str, Any]) -> Any:
        if name in self._clients:
            return self._clients[name]
        from langchain_mcp_adapters.client import MultiServerMCPClient
        conn: dict[str, Any] = {}
        transport = cfg.get("transport", "stdio")
        if transport == "stdio":
            conn = {"transport": "stdio", "command": cfg["command"],
                    "args": cfg.get("args", []), "env": cfg.get("env") or {}}
        elif transport in ("streamable_http", "http", "sse", "websocket"):
            conn = {"transport": "streamable_http" if transport == "http" else transport,
                    "url": cfg["url"]}
            if cfg.get("headers"):
                conn["headers"] = cfg["headers"]
        else:
            raise ValueError(f"unsupported transport {transport!r}")
        client = MultiServerMCPClient({name: conn})
        self._clients[name] = client
        return client

    # --- lazy auth (once per server per process) ---

    async def _lazy_auth(self, name: str, cfg: dict[str, Any]) -> None:
        hook = cfg.get("auth_hook")  # async callable supplied by the platform
        self._authed.add(name)  # once, even if the hook fails (lazy ONCE)
        if callable(hook):
            await hook(name)

    # --- tool calls ---

    async def call(self, exposed_name: str, args: dict[str, Any]) -> dict[str, Any]:
        """Invoke an mcp__<server>__<tool> with per-server isolation/timeout."""
        parts = exposed_name.split("__", 2)
        if len(parts) != 3:
            return {"kind": "error", "ok": False, "output": f"bad mcp tool name {exposed_name}"}
        _, server, tool_name = parts
        st = self.status.get(server)
        if st is None:
            return {"kind": "error", "ok": False, "output": f"unknown mcp server {server}"}
        if not st.connected:
            refreshed = await self._refresh_one(server)
            if not refreshed.get("ok"):
                return {"kind": "error", "ok": False,
                        "output": f"mcp server {server} unavailable: {refreshed.get('error')}"}
        t = st.tools.get(exposed_name)
        if t is None:
            return {"kind": "error", "ok": False,
                    "output": f"unknown tool {tool_name} on {server} (known: {sorted(st.tools)})"}
        try:
            result = await asyncio.wait_for(t.ainvoke(args), timeout=CALL_TIMEOUT_S)
            from worker.engine.security import wrap_untrusted
            return {"kind": "success", "ok": True,
                    "output": wrap_untrusted(str(result), source=f"mcp__{server}"),
                    "tool": exposed_name, "args": args}
        except Exception as exc:
            return {"kind": "error", "ok": False, "output": f"error: {exc}",
                    "tool": exposed_name, "args": args}

    def catalog(self) -> dict[str, list[str]]:
        """The folded-in catalog snapshot for the tool_search index."""
        return {name: sorted(st.tools) for name, st in self.status.items() if st.connected}


def _is_auth_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return "401" in text or "needsauth" in text or "unauthorized" in text


def _servers_from_env() -> list[dict[str, Any]]:
    servers: list[dict[str, Any]] = []
    raw = os.environ.get("MCP_SERVERS", "").strip()
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                servers.extend(parsed)
        except json.JSONDecodeError:
            pass
    # C7: the backend stamps a .mcp.json into UI-repo workspaces (Playwright
    # MCP opt-in). The legacy SDK read it at session start; the custom engine
    # must load it explicitly or the stamped config is a dead stub.
    mcp_json = os.path.join(os.environ.get("WORKSPACE_DIR", "/workspace"), ".mcp.json")
    try:
        with open(mcp_json, encoding="utf-8") as f:
            stamped = json.load(f)
        for name, cfg in (stamped.get("mcpServers") or {}).items():
            if isinstance(cfg, dict) and not any(s.get("name") == name for s in servers):
                servers.append({"name": name, "transport": "stdio", **cfg})
    except (OSError, json.JSONDecodeError):
        pass
    return servers


_MANAGER: MCPManager | None = None


def mcp_manager() -> MCPManager:
    global _MANAGER
    if _MANAGER is None:
        _MANAGER = MCPManager()
    return _MANAGER


__all__ = ["CONNECT_RETRY_DELAYS", "MCPManager", "MCPServerStatus", "mcp_manager"]
