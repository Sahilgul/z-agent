"""C-13: the shipped default jwt_secret ('dev-only-change-me') let anyone
forge a JWT and bypass PIN auth. The validator now fails fast at startup
unless ZAGENT_DEV_INSECURE_DEFAULTS opts in for local dev.

Built with Settings(_env_file=None) so the repo .env file can't mask the
test's intent, and monkeypatch.delenv removes the conftest's setdefault
ZAGENT_JWT_SECRET so each case starts from a known state.
"""

from __future__ import annotations

import pytest

from app.core.config import Settings


def _fresh_settings(monkeypatch: pytest.MonkeyPatch, **env) -> Settings:
    # Clear the conftest's setdefault defaults first, THEN apply the test's
    # specific env (the old order deleted the value it had just set).
    monkeypatch.delenv("ZAGENT_JWT_SECRET", raising=False)
    monkeypatch.delenv("ZAGENT_DEV_INSECURE_DEFAULTS", raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return Settings(_env_file=None)


def test_empty_jwt_secret_rejected_by_default(monkeypatch: pytest.MonkeyPatch):
    with pytest.raises(ValueError, match="ZAGENT_JWT_SECRET"):
        _fresh_settings(monkeypatch)


def test_shipped_default_jwt_secret_rejected(monkeypatch: pytest.MonkeyPatch):
    with pytest.raises(ValueError, match="ZAGENT_JWT_SECRET"):
        _fresh_settings(monkeypatch, ZAGENT_JWT_SECRET="dev-only-change-me")


def test_real_jwt_secret_accepted(monkeypatch: pytest.MonkeyPatch):
    s = _fresh_settings(monkeypatch, ZAGENT_JWT_SECRET="a-real-random-secret")
    assert s.jwt_secret == "a-real-random-secret"
    assert s.dev_insecure_defaults is False


def test_dev_insecure_defaults_allows_empty_secret(monkeypatch: pytest.MonkeyPatch):
    s = _fresh_settings(monkeypatch, ZAGENT_DEV_INSECURE_DEFAULTS="1")
    assert s.jwt_secret == ""
    assert s.dev_insecure_defaults is True


def test_bootstrap_admin_defaults_empty(monkeypatch: pytest.MonkeyPatch):
    """C-14: the bootstrap admin username/pin default to empty so a
    production deploy never silently seeds an active admin with a known PIN."""
    s = _fresh_settings(monkeypatch, ZAGENT_JWT_SECRET="a-real-random-secret")
    assert s.bootstrap_admin_username == ""
    assert s.bootstrap_admin_pin == ""
