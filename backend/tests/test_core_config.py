"""C-13: the shipped default jwt_secret ('dev-only-change-me') let anyone
forge a JWT and bypass PIN auth. The validator now fails fast at startup
unless COLLEGIUM_DEV_INSECURE_DEFAULTS opts in for local dev.

Built with Settings(_env_file=None) so the repo .env file can't mask the
test's intent, and monkeypatch.delenv removes the conftest's setdefault
COLLEGIUM_JWT_SECRET so each case starts from a known state.
"""

from __future__ import annotations

import pytest

from app.core.config import Settings


def _fresh_settings(monkeypatch: pytest.MonkeyPatch, **env) -> Settings:
    # Clear the conftest's setdefault defaults first, THEN apply the test's
    # specific env (the old order deleted the value it had just set).
    monkeypatch.delenv("COLLEGIUM_JWT_SECRET", raising=False)
    monkeypatch.delenv("COLLEGIUM_DEV_INSECURE_DEFAULTS", raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return Settings(_env_file=None)


def test_empty_jwt_secret_rejected_by_default(monkeypatch: pytest.MonkeyPatch):
    with pytest.raises(ValueError, match="COLLEGIUM_JWT_SECRET"):
        _fresh_settings(monkeypatch)


def test_shipped_default_jwt_secret_rejected(monkeypatch: pytest.MonkeyPatch):
    with pytest.raises(ValueError, match="COLLEGIUM_JWT_SECRET"):
        _fresh_settings(monkeypatch, COLLEGIUM_JWT_SECRET="dev-only-change-me")


def test_real_jwt_secret_accepted(monkeypatch: pytest.MonkeyPatch):
    s = _fresh_settings(monkeypatch, COLLEGIUM_JWT_SECRET="a-real-random-secret")
    assert s.jwt_secret == "a-real-random-secret"
    assert s.dev_insecure_defaults is False


def test_dev_insecure_defaults_allows_empty_secret(monkeypatch: pytest.MonkeyPatch):
    s = _fresh_settings(monkeypatch, COLLEGIUM_DEV_INSECURE_DEFAULTS="1")
    assert s.jwt_secret == ""
    assert s.dev_insecure_defaults is True


def test_bootstrap_admin_defaults_empty(monkeypatch: pytest.MonkeyPatch):
    """C-14: the bootstrap admin username/pin default to empty so a
    production deploy never silently seeds an active admin with a known PIN."""
    s = _fresh_settings(monkeypatch, COLLEGIUM_JWT_SECRET="a-real-random-secret")
    assert s.bootstrap_admin_username == ""
    assert s.bootstrap_admin_pin == ""


# ---------------------------------------------------------------- Wave 0

def test_memory_redis_rejected_outside_dev(monkeypatch: pytest.MonkeyPatch):
    """M1: a production-like config without COLLEGIUM_REDIS_URL must fail
    startup instead of silently running the in-process fakeredis."""
    monkeypatch.delenv("COLLEGIUM_REDIS_URL", raising=False)
    with pytest.raises(ValueError, match="COLLEGIUM_REDIS_URL"):
        _fresh_settings(monkeypatch, COLLEGIUM_JWT_SECRET="a-real-random-secret")


def test_memory_redis_allowed_in_dev(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("COLLEGIUM_REDIS_URL", raising=False)
    s = _fresh_settings(monkeypatch, COLLEGIUM_DEV_INSECURE_DEFAULTS="1")
    assert s.redis_url.startswith("memory://")


def test_durable_resume_requires_engine_db(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("COLLEGIUM_ENGINE_DATABASE_URL", raising=False)
    with pytest.raises(ValueError, match="COLLEGIUM_ENGINE_DATABASE_URL"):
        _fresh_settings(
            monkeypatch,
            COLLEGIUM_JWT_SECRET="a-real-random-secret",
            COLLEGIUM_FEATURE_DURABLE_RESUME="1",
        )


def test_worker_image_contract_mismatch_rejected(monkeypatch: pytest.MonkeyPatch):
    with pytest.raises(ValueError, match="Worker image tag"):
        _fresh_settings(
            monkeypatch,
            COLLEGIUM_JWT_SECRET="a-real-random-secret",
            COLLEGIUM_WORKER_IMAGE="collegium-worker:9.9.9",
        )


def test_worker_image_non_semver_tag_skips_check(monkeypatch: pytest.MonkeyPatch):
    s = _fresh_settings(
        monkeypatch,
        COLLEGIUM_JWT_SECRET="a-real-random-secret",
        COLLEGIUM_WORKER_IMAGE="collegium-worker:test",
    )
    assert s.worker_image == "collegium-worker:test"


def test_feature_flags_default_off(monkeypatch: pytest.MonkeyPatch):
    for flag in ("COLLEGIUM_FEATURE_DB_CONCURRENCY", "COLLEGIUM_FEATURE_BACKEND_SWARM",
                 "COLLEGIUM_FEATURE_CONTROL_ACKS", "COLLEGIUM_FEATURE_DURABLE_RESUME"):
        monkeypatch.delenv(flag, raising=False)
    s = _fresh_settings(monkeypatch, COLLEGIUM_JWT_SECRET="a-real-random-secret")
    assert s.feature_db_concurrency is False
    assert s.feature_backend_swarm is False
    assert s.feature_control_acks is False
    assert s.feature_durable_resume is False
