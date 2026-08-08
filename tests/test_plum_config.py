"""Plum deployment settings and one-release env compatibility."""

import pytest

from app.config import Settings


def test_plum_settings_read_new_environment_names(monkeypatch):
    monkeypatch.setenv("PLUM_SESSION_COOKIE_NAME", "plum_public_session")
    monkeypatch.setenv("PLUM_SESSION_COOKIE_SECURE", "true")

    config = Settings(_env_file=None)

    assert config.plum_session_cookie_name == "plum_public_session"
    assert config.plum_session_cookie_secure is True


def test_plum_settings_accept_legacy_environment_names(monkeypatch):
    monkeypatch.delenv("PLUM_SESSION_COOKIE_NAME", raising=False)
    monkeypatch.delenv("PLUM_SESSION_COOKIE_SECURE", raising=False)
    monkeypatch.setenv("FIBRE_SESSION_COOKIE_NAME", "legacy_session")
    monkeypatch.setenv("FIBRE_SESSION_COOKIE_SECURE", "true")

    config = Settings(_env_file=None)

    assert config.plum_session_cookie_name == "legacy_session"
    assert config.plum_session_cookie_secure is True


def test_plum_public_auth_requires_secure_cookie_in_production():
    with pytest.raises(ValueError, match="PLUM_SESSION_COOKIE_SECURE"):
        Settings(
            _env_file=None,
            app_env="production",
            ai4all_bridge_secret="production-bridge-secret",
            admin_token="production-admin-token",
            plum_public_test_auth_enabled=True,
            plum_session_cookie_secure=False,
        )
