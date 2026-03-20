"""Pool configuration settings for cross-account Bedrock access."""

from dataclasses import dataclass, field
from typing import Self

from pydantic import Field
from pydantic_settings import BaseSettings


@dataclass
class BedrockAccountConfig:
    """Configuration for a single Bedrock account in the pool."""

    account_id: str
    role_arn: str
    region: str = "us-east-1"
    external_id: str | None = None

    @classmethod
    def from_dict(cls, data: dict) -> Self:
        """Create a BedrockAccountConfig from a dictionary."""
        return cls(
            account_id=data["account_id"],
            role_arn=data["role_arn"],
            region=data.get("region", "us-east-1"),
            external_id=data.get("external_id"),
        )


class PoolSettings(BaseSettings):
    """Pool configuration settings loaded from environment."""

    # Health check settings
    health_check_interval_seconds: int = Field(default=60, description="Interval between health checks")
    cooldown_duration_seconds: int = Field(default=60, description="Duration to keep unhealthy accounts in cooldown")

    # Retry settings
    max_retries_per_request: int = Field(default=3, description="Max retries across different accounts")
    retry_delay_seconds: float = Field(default=0.5, description="Delay between retries")

    # STS settings
    sts_session_duration_seconds: int = Field(default=3600, description="Duration for assumed role sessions")
    sts_session_name: str = Field(default="BedrockGateway", description="Session name for STS AssumeRole")

    # Credential refresh settings
    credential_refresh_margin_seconds: int = Field(default=300, description="Time before expiration to refresh credentials")

    model_config = {"env_prefix": "BG_POOL_", "env_file": ".env"}


@dataclass
class PoolConfig:
    """Complete pool configuration including accounts and settings."""

    accounts: list[BedrockAccountConfig] = field(default_factory=list)
    settings: PoolSettings = field(default_factory=PoolSettings)

    @classmethod
    def from_dict(cls, data: dict) -> Self:
        """Create a PoolConfig from a dictionary configuration."""
        accounts = [BedrockAccountConfig.from_dict(acc) for acc in data.get("accounts", [])]
        settings = PoolSettings()
        return cls(accounts=accounts, settings=settings)


def get_pool_settings() -> PoolSettings:
    """Get pool settings singleton."""
    return PoolSettings()
