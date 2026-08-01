"""BYO-PAT tests (plan §1b Phase 3): stdlib cipher round-trip + tamper detection,
connectionData identity proof (fail-closed), 90-day expiry, 7-day warning,
write-only status. The ADO verify call is injected — no sockets.
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.db.models.user import User
from app.services import byo_pat


async def _verify_ok(pat):
    return "descriptor-ali"


# --------------------------------------------------------------------- crypto
def test_encrypt_decrypt_round_trip():
    blob = byo_pat.encrypt("secret-pat-value")
    assert blob != "secret-pat-value"
    assert "secret" not in blob
    assert byo_pat.decrypt(blob) == "secret-pat-value"


def test_tampered_blob_fails_integrity():
    blob = byo_pat.encrypt("secret-pat-value")
    tampered = blob[:-4] + ("AAAA" if not blob.endswith("AAAA") else "BBBB")
    with pytest.raises(byo_pat.ByoPatError):
        byo_pat.decrypt(tampered)


# --------------------------------------------------------------- store/proof
async def test_store_requires_matching_descriptor(session, make_user):
    u = make_user()
    u.ado_descriptor = "descriptor-ali"
    session.commit()
    status = await byo_pat.store_pat(u.id, "pat-123", verify=_verify_ok)
    assert status["configured"] is True
    assert status["days_remaining"] == 90
    session.expire(u)  # service wrote through its own session — drop the stale copy
    stored = session.get(User, u.id)
    assert stored.byo_pat_encrypted
    assert "pat-123" not in stored.byo_pat_encrypted
    assert byo_pat.decrypt(stored.byo_pat_encrypted) == "pat-123"


async def test_store_rejects_mismatched_identity(session, make_user):
    u = make_user()
    u.ado_descriptor = "descriptor-ali"
    session.commit()

    async def other_person(pat):
        return "descriptor-bilal"

    with pytest.raises(byo_pat.ByoPatError, match="does not match"):
        await byo_pat.store_pat(u.id, "pat-123", verify=other_person)
    assert session.get(User, u.id).byo_pat_encrypted is None


async def test_store_requires_ado_binding(session, make_user):
    u = make_user()  # no ado_descriptor
    with pytest.raises(byo_pat.ByoPatError, match="no ADO identity"):
        await byo_pat.store_pat(u.id, "pat-123", verify=_verify_ok)


async def test_store_rejects_empty_pat(session, make_user):
    u = make_user()
    with pytest.raises(byo_pat.ByoPatError):
        await byo_pat.store_pat(u.id, "   ", verify=_verify_ok)


# ------------------------------------------------------------------- status
def test_status_is_write_only_and_warns_near_expiry(session, make_user):
    u = make_user()
    u.byo_pat_encrypted = byo_pat.encrypt("secret")
    u.byo_pat_expires_at = datetime.now(timezone.utc) + timedelta(days=5)
    session.commit()
    status = byo_pat.pat_status(u.id)
    assert status == {
        "configured": True,
        "expires_at": u.byo_pat_expires_at.isoformat(),
        "days_remaining": 5,
        "expiring_soon": True,
    }
    assert "secret" not in str(status)


def test_status_unconfigured(session, make_user):
    u = make_user()
    assert byo_pat.pat_status(u.id) == {"configured": False}


# ---------------------------------------------------------------- push + revoke
def test_pat_for_push_returns_secret_until_expiry(session, make_user):
    u = make_user()
    u.byo_pat_encrypted = byo_pat.encrypt("push-secret")
    u.byo_pat_expires_at = datetime.now(timezone.utc) + timedelta(days=30)
    session.commit()
    assert byo_pat.pat_for_push(u.id) == "push-secret"
    u.byo_pat_expires_at = datetime.now(timezone.utc) - timedelta(days=1)
    session.commit()
    assert byo_pat.pat_for_push(u.id) is None  # expired → fall back to FLEET_PAT


def test_revoke_clears_everything(session, make_user):
    u = make_user()
    u.byo_pat_encrypted = byo_pat.encrypt("x")
    u.byo_pat_expires_at = datetime.now(timezone.utc) + timedelta(days=90)
    session.commit()
    byo_pat.revoke(u.id)
    session.expire(u)  # service wrote through its own session — drop the stale copy
    stored = session.get(User, u.id)
    assert stored.byo_pat_encrypted is None
    assert stored.byo_pat_expires_at is None
