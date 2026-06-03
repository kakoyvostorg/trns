"""Tests for production security guardrails in the bot server."""

import pytest

from trns.bot import server


def test_production_requires_webhook_and_admin_secrets(monkeypatch):
    monkeypatch.setattr(server, "_environment", "production")
    monkeypatch.setattr(server, "_webhook_secret", None)
    monkeypatch.setattr(server, "_admin_token", None)

    with pytest.raises(RuntimeError) as exc:
        server._validate_production_security()

    assert "WEBHOOK_SECRET" in str(exc.value)
    assert "ADMIN_TOKEN" in str(exc.value)


def test_development_allows_missing_security_secrets(monkeypatch):
    monkeypatch.setattr(server, "_environment", "development")
    monkeypatch.setattr(server, "_webhook_secret", None)
    monkeypatch.setattr(server, "_admin_token", None)

    server._validate_production_security()


def test_production_allows_configured_security_secrets(monkeypatch):
    monkeypatch.setattr(server, "_environment", "production")
    monkeypatch.setattr(server, "_webhook_secret", "webhook-secret")
    monkeypatch.setattr(server, "_admin_token", "admin-token")

    server._validate_production_security()
