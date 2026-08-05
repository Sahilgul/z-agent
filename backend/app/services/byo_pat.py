"""BYO-PAT opt-in (ADO credentials section).

A teammate may paste their own ADO PAT so push operations can attribute to
them. Rules from the plan:
  * Code+WorkItems scopes ONLY (enforced by UX; ADO exposes no scope-introspection
    endpoint, so the field's help text is the gate — documented, not verified).
  * 90-day expiry, with a 7-day expiry warning in the status payload.
  * WRITE-ONLY: the secret is never returned by any API — status only.
  * Encrypted at rest. Local-era cipher is stdlib-only (SHA-256 CTR keystream +
    HMAC-SHA256 tag, encrypt-then-MAC); Key Vault takes over at the VM move.
  * connectionData identity PROOF: the PAT is verified against ADO's
    connectionData endpoint and the returned descriptor MUST equal the user's
    bound ado_descriptor — fail-closed, never "pick the likelier Ali".
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import math
import os
from datetime import datetime, timedelta, timezone

import httpx

from app.core.config import get_settings
from app.core.logging import get_logger
from app.db.base import get_session
from app.db.models.user import User

log = get_logger(service="byo_pat")

PAT_TTL_DAYS = 90
EXPIRY_WARNING_DAYS = 7


class ByoPatError(ValueError):
    pass


# ------------------------------------------------------------------- crypto
def _keystream(key: bytes, nonce: bytes, n: int) -> bytes:
    out = b""
    counter = 0
    while len(out) < n:
        out += hashlib.sha256(key + nonce + counter.to_bytes(8, "big")).digest()
        counter += 1
    return out[:n]


def _key() -> bytes:
    raw = get_settings().byo_pat_encryption_key
    if not raw:
        raise ByoPatError("byo_pat_encryption_key is not configured")
    return hashlib.sha256(raw.encode()).digest()


def encrypt(plaintext: str) -> str:
    key = _key()
    nonce = os.urandom(16)
    data = plaintext.encode()
    ct = bytes(a ^ b for a, b in zip(data, _keystream(key, nonce, len(data))))
    tag = hmac.new(key, nonce + ct, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(nonce + tag + ct).decode()


def decrypt(blob: str) -> str:
    key = _key()
    raw = base64.urlsafe_b64decode(blob.encode())
    nonce, tag, ct = raw[:16], raw[16:48], raw[48:]
    expected = hmac.new(key, nonce + ct, hashlib.sha256).digest()
    if not hmac.compare_digest(tag, expected):
        raise ByoPatError("stored PAT failed integrity check")
    data = bytes(a ^ b for a, b in zip(ct, _keystream(key, nonce, len(ct))))
    return data.decode()


# --------------------------------------------------------------- verify+store
async def connection_data_descriptor(pat: str, org: str | None = None) -> str:
    """ADO connectionData: the PAT resolves its owner's descriptor. Raises on
    any non-200 — an unverifiable PAT is never stored (fail-closed)."""
    settings = get_settings()
    org = org or settings.ado_org
    token = base64.b64encode(f":{pat}".encode()).decode()
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(
            f"https://dev.azure.com/{org}/_apis/connectionData",
            headers={"Authorization": f"Basic {token}"},
            params={"api-version": "7.1"},
        )
        resp.raise_for_status()
    data = resp.json()
    descriptor = (data.get("authenticatedUser") or {}).get("id")
    if not descriptor:
        raise ByoPatError("connectionData returned no authenticated user")
    return str(descriptor)


async def store_pat(user_id: int, pat: str, verify=None) -> dict:
    """Verify identity proof, then store encrypted with a 90-day expiry.
    The descriptor from connectionData MUST equal the user's bound
    ado_descriptor — a PAT for a different ADO identity is rejected."""
    if not pat.strip():
        raise ByoPatError("empty PAT")
    verify = verify or connection_data_descriptor
    descriptor = await verify(pat)
    session = get_session()
    try:
        user = session.get(User, user_id)
        if user is None:
            raise ByoPatError("user not found")
        if not user.ado_descriptor:
            raise ByoPatError("user has no ADO identity binding")
        if descriptor != user.ado_descriptor:
            raise ByoPatError("PAT identity does not match the bound ADO identity")
        user.byo_pat_encrypted = encrypt(pat)
        user.byo_pat_expires_at = datetime.now(timezone.utc) + timedelta(days=PAT_TTL_DAYS)
        session.commit()
        return pat_status(user_id)
    finally:
        session.close()


def revoke(user_id: int) -> None:
    session = get_session()
    try:
        user = session.get(User, user_id)
        if user:
            user.byo_pat_encrypted = None
            user.byo_pat_expires_at = None
            session.commit()
    finally:
        session.close()


def pat_status(user_id: int) -> dict:
    """Write-only contract: the status payload NEVER carries the secret."""
    session = get_session()
    try:
        user = session.get(User, user_id)
        if user is None or not user.byo_pat_encrypted:
            return {"configured": False}
        now = datetime.now(timezone.utc)
        days = (max(0, math.ceil((user.byo_pat_expires_at - now).total_seconds() / 86400))
                if user.byo_pat_expires_at else 0)
        return {
            "configured": True,
            "expires_at": user.byo_pat_expires_at.isoformat() if user.byo_pat_expires_at else None,
            "days_remaining": days,
            "expiring_soon": days <= EXPIRY_WARNING_DAYS,
        }
    finally:
        session.close()


def pat_for_push(user_id: int) -> str | None:
    """Internal use ONLY (push operations) — never exposed over the API.
    An expired PAT is treated as absent so pushes fall back to FLEET_PAT."""
    session = get_session()
    try:
        user = session.get(User, user_id)
        if user is None or not user.byo_pat_encrypted:
            return None
        if user.byo_pat_expires_at and user.byo_pat_expires_at <= datetime.now(timezone.utc):
            return None
        return decrypt(user.byo_pat_encrypted)
    finally:
        session.close()
