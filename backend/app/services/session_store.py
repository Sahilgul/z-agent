"""Cross-host session store: session volumes are host paths;
when workers run on OTHER hosts (VM → AKS), a thread's durable session must
follow it. The store mirrors `sessions/<run_id>/<thread_id>/` into object
storage at thread end and materializes it back on resume — the bind-mount stays
the hot path, the store is the cross-host substrate.

The client is injectable: production wires Azure Blob (env-gated); tests use a
dict fake. The 30-day retention policy deletes both the volume AND the mirror —
replay-only afterwards (events remain to their TTL).
"""

from __future__ import annotations

import tarfile
import io
from pathlib import Path

from app.core.config import get_settings
from app.core.logging import get_logger

log = get_logger(service="session_store")


class _BlobClient:
    """Azure Blob default — imported lazily so the backend runs fine without
    the SDK (local era: sessions never leave the single host)."""

    def __init__(self) -> None:
        from azure.storage.blob import BlobServiceClient  # type: ignore
        settings = get_settings()
        self._container = (BlobServiceClient
                           .from_connection_string(settings.session_store_connection)
                           .get_container_client("zagent-sessions"))

    def put(self, key: str, data: bytes) -> None:
        self._container.upload_blob(key, data, overwrite=True)

    def get(self, key: str) -> bytes:
        return self._container.download_blob(key).readall()

    def delete(self, key: str) -> None:
        self._container.delete_blob(key)


def _key(run_id: str, thread_id: str) -> str:
    return f"{run_id}/{thread_id}.tar.gz"


def pack(volume: Path) -> bytes:
    """Thread session dir → a single tarball. Deterministic member order."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for path in sorted(volume.rglob("*")):
            if path.is_file():
                tar.add(path, arcname=path.relative_to(volume))
    return buf.getvalue()


def unpack(blob: bytes, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tar:
        for member in tar.getmembers():
            target = (dest / member.name).resolve()
            if not str(target).startswith(str(dest.resolve())):
                raise ValueError(f"unsafe member path: {member.name}")
        tar.extractall(dest, filter="data")


def upload(run_id: str, thread_id: str, volume: Path, client=None) -> bool:
    """Mirror a finished thread's session volume. Never raises — the thread already
    succeeded; a mirror hiccup is logged, not failed."""
    if not volume.exists():
        return False
    try:
        (client or _BlobClient()).put(_key(run_id, thread_id), pack(volume))
        return True
    except Exception as exc:  # noqa: BLE001 — mirror must not fail the thread
        log.warning("session upload failed", run_id=run_id, error=str(exc))
        return False


def materialize(run_id: str, thread_id: str, dest: Path, client=None) -> bool:
    """Fetch + unpack a thread session onto THIS host (resume path)."""
    try:
        blob = (client or _BlobClient()).get(_key(run_id, thread_id))
        unpack(blob, dest)
        return True
    except Exception as exc:  # noqa: BLE001
        log.info("session materialize missed", run_id=run_id, error=str(exc))
        return False


def purge(run_id: str, thread_id: str, client=None) -> bool:
    """Retention: the 30-day sweep removes the mirror too."""
    try:
        (client or _BlobClient()).delete(_key(run_id, thread_id))
        return True
    except Exception:  # noqa: BLE001 — absent blob counts as purged
        return True
