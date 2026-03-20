"""Data models for the Bedrock account pool."""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any


class HealthStatus(Enum):
    """Health status for a Bedrock account."""

    HEALTHY = "healthy"
    UNHEALTHY = "unhealthy"
    COOLDOWN = "cooldown"
    UNKNOWN = "unknown"


@dataclass
class AssumedRoleCredentials:
    """Credentials obtained from STS AssumeRole."""

    access_key_id: str
    secret_access_key: str
    session_token: str
    expiration: datetime

    def is_expired(self, margin_seconds: int = 0) -> bool:
        """Check if credentials are expired or about to expire."""
        from datetime import timedelta

        expiration_with_margin = self.expiration - timedelta(seconds=margin_seconds)
        return datetime.now(UTC) >= expiration_with_margin


@dataclass
class BedrockAccount:
    """Represents a Bedrock account in the pool with health tracking."""

    account_id: str
    role_arn: str
    region: str
    external_id: str | None = None

    # Health tracking
    health_status: HealthStatus = HealthStatus.UNKNOWN
    last_health_check: datetime | None = None
    cooldown_until: datetime | None = None
    consecutive_failures: int = 0

    # Statistics
    request_count: int = 0
    error_count: int = 0
    last_used: datetime | None = None

    # Cached credentials
    credentials: AssumedRoleCredentials | None = None

    def is_healthy(self) -> bool:
        """Check if the account is currently healthy and available."""
        if self.health_status == HealthStatus.HEALTHY:
            return True
        if self.health_status == HealthStatus.COOLDOWN:
            return self._is_cooldown_expired()
        return False

    def _is_cooldown_expired(self) -> bool:
        """Check if the cooldown period has expired."""
        if self.cooldown_until is None:
            return True

        return datetime.now(UTC) >= self.cooldown_until

    def to_status_dict(self) -> dict[str, Any]:
        """Convert to a status dictionary for API responses."""
        return {
            "account_id": self.account_id,
            "region": self.region,
            "health_status": self.health_status.value,
            "last_health_check": self.last_health_check.isoformat() if self.last_health_check else None,
            "cooldown_until": self.cooldown_until.isoformat() if self.cooldown_until else None,
            "request_count": self.request_count,
            "error_count": self.error_count,
            "last_used": self.last_used.isoformat() if self.last_used else None,
        }


@dataclass
class PoolClient:
    """A Bedrock client wrapper for a specific account."""

    account: BedrockAccount
    client: Any  # boto3 Bedrock runtime client

    def get_account_id(self) -> str:
        """Get the account ID for this client."""
        return self.account.account_id


@dataclass
class PoolStatus:
    """Overall pool status summary."""

    total_accounts: int
    healthy_accounts: int
    unhealthy_accounts: int
    cooldown_accounts: int
    accounts: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Convert to a dictionary for API responses."""
        return {
            "total_accounts": self.total_accounts,
            "healthy_accounts": self.healthy_accounts,
            "unhealthy_accounts": self.unhealthy_accounts,
            "cooldown_accounts": self.cooldown_accounts,
            "accounts": self.accounts,
        }
