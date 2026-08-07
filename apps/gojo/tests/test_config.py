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


# Connector credentials (step 4). Same shape as the teams_* block: defaults
# empty, secrets as SecretStr, a *_configured property that is the single
# truth for "can this connector authenticate".


def test_graph_not_configured_by_default() -> None:
    assert not Settings(_env_file=None).graph_configured


def test_graph_configured_when_all_four_fields_set() -> None:
    settings = Settings(
        _env_file=None,
        graph_client_id="app-id",
        graph_tenant_id="tenant-id",
        graph_client_secret="graph-secret-value",
        graph_owner_upn="owner@example.com",
    )
    assert settings.graph_configured


def test_graph_not_configured_when_one_field_missing() -> None:
    """A partially-filled connector must read as absent, not misconfigured."""
    settings = Settings(
        _env_file=None,
        graph_client_id="app-id",
        graph_tenant_id="tenant-id",
        graph_client_secret="graph-secret-value",
        # graph_owner_upn missing - app-only Graph cannot call /me (8.3)
    )
    assert not settings.graph_configured


def test_jira_not_configured_by_default() -> None:
    assert not Settings(_env_file=None).jira_configured


def test_jira_configured_when_all_three_fields_set() -> None:
    settings = Settings(
        _env_file=None,
        jira_base_url="https://example.atlassian.net",
        jira_email="owner@example.com",
        jira_api_token="jira-token-value",
    )
    assert settings.jira_configured


def test_jira_not_configured_when_token_missing() -> None:
    settings = Settings(
        _env_file=None,
        jira_base_url="https://example.atlassian.net",
        jira_email="owner@example.com",
    )
    assert not settings.jira_configured


def test_connector_secrets_do_not_leak_via_repr() -> None:
    """SecretStr keeps credentials out of logs and tracebacks.

    The field checks stop this passing vacuously while the fields do not
    exist yet (extra="ignore" would silently drop the kwargs below).
    """
    assert "graph_client_secret" in Settings.model_fields
    assert "jira_api_token" in Settings.model_fields
    settings = Settings(
        _env_file=None,
        graph_client_secret="graph-secret-value",
        jira_api_token="jira-token-value",
    )
    shown = repr(settings)
    assert "graph-secret-value" not in shown
    assert "jira-token-value" not in shown
