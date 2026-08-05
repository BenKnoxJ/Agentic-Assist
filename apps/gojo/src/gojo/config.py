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

    # How long a Teams turn may run before Gojo says something rather than
    # leaving you looking at a typing indicator. Under it, the answer arrives
    # as a single message with no "on it" noise.
    #
    # Must stay comfortably below Azure Bot Service's 10-15s response timeout
    # (ADR 0006) - this budget is spent inside that window. Do not raise this
    # above ~8: overshooting the channel gives the user a 504, which is worse
    # than an acknowledgement.
    #
    # Measured 4 Aug 2026: 3.5s with no session to resume, 5.9-7.1s once a
    # session is being resumed. The SDK replays the transcript on every turn,
    # so the cost grows with conversation length - raising this number buys
    # time, it does not fix the trend. /compact is the actual fix.
    fast_reply_seconds: float = 8.0

    # Where conversation state lives. SQLite, not Postgres: at one user's
    # write volume it is lower overhead, needs no extra process, and is a
    # single file to back up (6.1). Gitignored via checkpoints/.
    checkpoint_path: str = "checkpoints/gojo.sqlite"

    # 9.3 mandates BOTH guards below, because they catch different failures.
    #
    # Wall clock. The only thing that catches a *hung* Agent SDK subprocess:
    # a stuck call has no turn count and no recursion, it simply never
    # returns. Generous on purpose - this is a backstop, not a latency
    # target; fast_reply_seconds handles the user-facing side.
    graph_timeout_seconds: float = 180.0

    # LangGraph's framework guard against a cyclic graph. 25 is its default;
    # setting it explicitly means a future default change cannot move it
    # underneath us.
    recursion_limit: int = 25

    # Agent invocations allowed in one turn. Catches an agent that is
    # progressing but should not keep going - the runaway loop 9.3 ranks as
    # the best-evidenced failure mode of single-VPS agent systems.
    max_agent_calls_per_turn: int = 5

    # How long an owed reply stays worth delivering. An answer to a question
    # asked before a restart the user has long since forgotten is noise, not
    # service - and 9.1 requires a ceiling at the moment a store is created,
    # not a retention routine bolted on later. ADR 0008.
    owed_reply_max_age_seconds: float = 21600.0  # 6 hours

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
