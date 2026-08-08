"""LiteLLM gateway adapter — the cost data path.

The control plane mints a VIRTUAL KEY PER LANE with a per-key max_budget, injects
it as the worker's gateway credential at container start, and reads spend back at
run end (eventually consistent — reconcile with a short poll + grace window).
Per-key budgets give CORRECTLY PRICED enforcement at the gateway; SDK
max_budget_usd is demoted to last-resort backstop. gateway-db loss = keys dead,
threads fail safe + resumable, keys re-minted on recovery (disposable by design).

CLI (day-1 spike needs exactly one key):
  python -m app.gateway.litellm mint --alias spike --budget 5.00
  python -m app.gateway.litellm spend --key sk-...
  python -m app.gateway.litellm delete --key sk-...
"""

from __future__ import annotations

import argparse
import asyncio
import time
from dataclasses import dataclass

import httpx

from app.core.config import get_settings
from app.core.logging import get_logger

log = get_logger(service="gateway.litellm")


@dataclass
class VirtualKey:
    key: str
    alias: str
    max_budget: float


class GatewayClient:
    def __init__(self, base_url: str | None = None, master_key: str | None = None) -> None:
        settings = get_settings()
        self.base_url = (base_url or settings.gateway_url).rstrip("/")
        self.master_key = master_key if master_key is not None else settings.litellm_master_key
        self._headers = {"Authorization": f"Bearer {self.master_key}"}

    async def mint_key(self, alias: str, max_budget_usd: float, models: list[str] | None = None) -> VirtualKey:
        settings = get_settings()
        async with httpx.AsyncClient(base_url=self.base_url, headers=self._headers, timeout=30) as client:
            resp = await client.post("/key/generate", json={
                "key_alias": alias,
                "max_budget": max_budget_usd,
                "models": models or [settings.gateway_model],
                # A minted key is never meant to outlive its thread. The TTL is
                # the backstop for every cleanup path that could miss (process
                # crash between mint and persist, gateway drift) — an orphaned
                # key self-destructs instead of holding budget forever.
                "duration": settings.gateway_key_ttl,
            })
            resp.raise_for_status()
            data = resp.json()
            return VirtualKey(key=data["key"], alias=alias, max_budget=max_budget_usd)

    async def delete_key(self, key: str) -> None:
        async with httpx.AsyncClient(base_url=self.base_url, headers=self._headers, timeout=30) as client:
            resp = await client.post("/key/delete", json={"keys": [key]})
            # F6: deleting an already-deleted/expired key is SUCCESS for our
            # purposes — the goal state (key unusable) holds. The request
            # shape is static, so a 400/404 can only be about the key itself;
            # gating the tolerance on the gateway's error-body wording broke
            # whenever LiteLLM rephrased it. Only propagate real gateway errors.
            if resp.status_code in (400, 404):
                log.info("gateway key already gone", tail=key[-4:])
                return
            resp.raise_for_status()

    async def key_spend(self, key: str) -> float:
        async with httpx.AsyncClient(base_url=self.base_url, headers=self._headers, timeout=30) as client:
            resp = await client.get("/key/info", params={"key": key})
            resp.raise_for_status()
            return float(resp.json().get("info", {}).get("spend", 0.0))

    async def read_spend_reconciled(self, key: str, grace_seconds: float = 5.0, polls: int = 3) -> float:
        """Gateway metering is eventually consistent — poll with a grace window
        before declaring a thread's final cost."""
        await asyncio.sleep(grace_seconds)
        spend = 0.0
        for _ in range(polls):
            spend = await self.key_spend(key)
            await asyncio.sleep(1.0)
        return spend

    async def health(self) -> bool:
        try:
            async with httpx.AsyncClient(base_url=self.base_url, timeout=5) as client:
                resp = await client.get("/health/liveliness")
                return resp.status_code == 200
        except httpx.HTTPError:
            return False


async def retry_with_backoff(fn, attempts: int = 5, base_delay: float = 1.0):
    """Worker-side gateway failure story: bounded backoff on
    429/5xx/timeout/transport-blip, then the thread FAILS SAFE (stage=failed,
    resumable)."""
    for attempt in range(attempts):
        try:
            return await fn()
        # M-52: TransportError (connection refused / network blip / DNS) used
        # to NOT be caught here, so a transient connection blip aborted the
        # thread immediately with no retry. Retry transport errors too.
        except (httpx.HTTPStatusError, httpx.TimeoutException,
                httpx.TransportError) as exc:
            if attempt == attempts - 1:
                raise
            await asyncio.sleep(base_delay * (2 ** attempt))
            _ = exc


def _cli() -> None:
    parser = argparse.ArgumentParser(prog="app.gateway.litellm")
    sub = parser.add_subparsers(dest="cmd", required=True)
    mint = sub.add_parser("mint")
    mint.add_argument("--alias", required=True)
    mint.add_argument("--budget", type=float, default=5.0)
    spend = sub.add_parser("spend")
    spend.add_argument("--key", required=True)
    delete = sub.add_parser("delete")
    delete.add_argument("--key", required=True)
    args = parser.parse_args()

    client = GatewayClient()
    if args.cmd == "mint":
        vk = asyncio.run(client.mint_key(args.alias, args.budget))
        print(vk.key)
    elif args.cmd == "spend":
        started = time.monotonic()
        print(f"${asyncio.run(client.key_spend(args.key)):.4f}  ({time.monotonic()-started:.1f}s)")
    elif args.cmd == "delete":
        asyncio.run(client.delete_key(args.key))
        print("deleted")


if __name__ == "__main__":
    _cli()
