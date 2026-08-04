"""Application configuration.

Auth: Gojo authenticates through the Claude Agent SDK using Claude Code's
stored subscription credentials. Do NOT set ANTHROPIC_API_KEY anywhere in
this project - it takes precedence over the subscription token and would
silently move billing to pay-as-you-go API rates. See GOJO-MASTER.md 6.2.
"""

import os
from functools import lru_cache

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Settings loaded from environment variables or a .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    routing_model: str = "claude-haiku-4-5-20251001"
    reasoning_model: str = "claude-sonnet-4-6"

    max_turns_per_agent: int = 8
    max_tokens_per_turn: int = 4096

    # Teams surface. Single-tenant: TENANT_ID is what restricts the accepted
    # token issuers to your tenant (the SDK builds them from it), so leaving it
    # unset silently widens who can call you. GOJO-MASTER.md 5.2.
    #
    # Note these are NOT the Bot Framework v4 names. That SDK used
    # MicrosoftAppType/MicrosoftAppId/MicrosoftAppTenantId; the Agents SDK reads
    # CLIENTID/TENANTID/CLIENTSECRET. Same concept, different keys.
    teams_client_id: str = ""
    teams_tenant_id: str = ""
    teams_client_secret: SecretStr = SecretStr("")

    # Comma-separated Entra object IDs allowed to talk to Gojo.
    #
    # JWT validation proves a request came from Azure Bot Service for our bot.
    # It says nothing about who typed it - any tenant user who installs the app
    # produces perfectly valid tokens. This is the check that makes Gojo single
    # -user in fact and not just in intent (1.3: one user, one tenant).
    #
    # Empty means nobody is authorised. Deliberate: an unset allow-list should
    # lock the door, not remove it.
    allowed_user_ids: str = ""

    @property
    def allowed_users(self) -> frozenset[str]:
        """Entra object IDs permitted to use the assistant."""
        return frozenset(u.strip() for u in self.allowed_user_ids.split(",") if u.strip())

    @property
    def teams_configured(self) -> bool:
        """True when the Teams surface has everything it needs to authenticate."""
        return bool(
            self.teams_client_id
            and self.teams_tenant_id
            and self.teams_client_secret.get_secret_value()
        )


def assert_subscription_auth() -> None:
    """Fail loudly if an API key would shadow subscription auth."""
    if os.environ.get("ANTHROPIC_API_KEY"):
        raise RuntimeError(
            "ANTHROPIC_API_KEY is set. It takes precedence over Claude Code's "
            "subscription credentials and would bill at API rates. Unset it."
        )


@lru_cache
def get_settings() -> Settings:
    """Return the cached settings singleton."""
    assert_subscription_auth()
    return Settings()
