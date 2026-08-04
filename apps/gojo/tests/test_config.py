"""Tests for the subscription-auth guard.

This is the highest-consequence code in the repo: if it stops working, the
failure is silent and the bill arrives at API rates a month later
(GOJO-MASTER.md 6.2). It gets tested before anything else does.
"""

import pytest

from gojo.config import Settings, assert_subscription_auth, get_settings


def test_raises_when_api_key_present(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-whatever")
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        assert_subscription_auth()


def test_passes_when_api_key_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert_subscription_auth()  # does not raise


def test_empty_api_key_is_not_treated_as_set(monkeypatch: pytest.MonkeyPatch) -> None:
    """An exported-but-empty var does not shadow the subscription token."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    assert_subscription_auth()  # does not raise


def test_get_settings_enforces_the_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    """The guard runs on the settings path, not only when called directly."""
    get_settings.cache_clear()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-whatever")
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        get_settings()
    get_settings.cache_clear()


def test_settings_has_no_api_key_field() -> None:
    """6.2: a field that does not exist cannot be populated by accident.

    Fails if anyone adds an anthropic_api_key setting, which would give the
    key a legitimate-looking home and defeat the guard above.
    """
    forbidden = {"anthropic_api_key", "api_key", "anthropic_auth_token"}
    assert forbidden.isdisjoint(Settings.model_fields)
