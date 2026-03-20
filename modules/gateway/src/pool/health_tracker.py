"""Health tracking for Bedrock accounts in the pool."""

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from src.pool.config import PoolSettings
from src.pool.models import BedrockAccount, HealthStatus

logger = logging.getLogger(__name__)


class HealthTracker:
    """Tracks health status of Bedrock accounts with cooldown management."""

    def __init__(self, settings: PoolSettings | None = None):
        """Initialize the health tracker.

        Args:
            settings: Pool settings for cooldown configuration
        """
        self._settings = settings or PoolSettings()
        self._lock = asyncio.Lock()

    async def mark_healthy(self, account: BedrockAccount) -> None:
        """Mark an account as healthy.

        Args:
            account: The account to mark as healthy
        """
        async with self._lock:
            account.health_status = HealthStatus.HEALTHY
            account.last_health_check = datetime.now(UTC)
            account.cooldown_until = None
            account.consecutive_failures = 0
            logger.info(f"Account {account.account_id} marked as healthy")

    async def mark_unhealthy(
        self,
        account: BedrockAccount,
        reason: str = "",
        apply_cooldown: bool = True,
    ) -> None:
        """Mark an account as unhealthy and optionally apply cooldown.

        Args:
            account: The account to mark as unhealthy
            reason: Optional reason for the unhealthy status
            apply_cooldown: Whether to apply cooldown period
        """
        async with self._lock:
            account.consecutive_failures += 1
            account.last_health_check = datetime.now(UTC)
            account.error_count += 1

            if apply_cooldown:
                # Calculate cooldown with exponential backoff (max 5 minutes)
                backoff_multiplier = min(account.consecutive_failures, 5)
                cooldown_seconds = self._settings.cooldown_duration_seconds * backoff_multiplier
                cooldown_seconds = min(cooldown_seconds, 300)  # Cap at 5 minutes

                account.cooldown_until = datetime.now(UTC) + timedelta(seconds=cooldown_seconds)
                account.health_status = HealthStatus.COOLDOWN
                logger.warning(f"Account {account.account_id} marked as unhealthy (cooldown: {cooldown_seconds}s): {reason}")
            else:
                account.health_status = HealthStatus.UNHEALTHY
                logger.warning(f"Account {account.account_id} marked as unhealthy: {reason}")

    async def is_healthy(self, account: BedrockAccount) -> bool:
        """Check if an account is currently healthy or has exited cooldown.

        Args:
            account: The account to check

        Returns:
            True if the account is healthy or cooldown has expired
        """
        async with self._lock:
            return self._is_healthy_unlocked(account)

    def _is_healthy_unlocked(self, account: BedrockAccount) -> bool:
        """Check health status without acquiring the lock (internal use)."""
        if account.health_status == HealthStatus.HEALTHY:
            return True

        if account.health_status == HealthStatus.COOLDOWN:
            if self._is_cooldown_expired_unlocked(account):
                # Cooldown has expired, but status remains COOLDOWN until next successful request
                return True

        return False

    def _is_cooldown_expired_unlocked(self, account: BedrockAccount) -> bool:
        """Check if cooldown has expired without lock."""
        if account.cooldown_until is None:
            return True
        return datetime.now(UTC) >= account.cooldown_until

    async def get_cooldown_remaining(self, account: BedrockAccount) -> float:
        """Get remaining cooldown time in seconds.

        Args:
            account: The account to check

        Returns:
            Remaining cooldown seconds, or 0 if not in cooldown
        """
        async with self._lock:
            if account.cooldown_until is None:
                return 0.0

            remaining = (account.cooldown_until - datetime.now(UTC)).total_seconds()
            return max(0.0, remaining)

    async def reset_cooldown(self, account: BedrockAccount) -> None:
        """Reset cooldown for an account (e.g., after successful retry).

        Args:
            account: The account to reset
        """
        async with self._lock:
            account.cooldown_until = None
            account.health_status = HealthStatus.HEALTHY
            account.consecutive_failures = 0
            logger.info(f"Cooldown reset for account {account.account_id}")

    async def record_request(self, account: BedrockAccount) -> None:
        """Record a request for statistics.

        Args:
            account: The account that handled the request
        """
        async with self._lock:
            account.request_count += 1
            account.last_used = datetime.now(UTC)

    async def record_success(self, account: BedrockAccount) -> None:
        """Record a successful request and ensure healthy status.

        Args:
            account: The account that succeeded
        """
        async with self._lock:
            account.request_count += 1
            account.last_used = datetime.now(UTC)
            account.last_health_check = datetime.now(UTC)

            # If recovering from cooldown, restore to healthy
            if account.health_status in (HealthStatus.COOLDOWN, HealthStatus.UNHEALTHY):
                account.health_status = HealthStatus.HEALTHY
                account.cooldown_until = None
                account.consecutive_failures = 0
                logger.info(f"Account {account.account_id} recovered and marked healthy")

    async def record_error(
        self,
        account: BedrockAccount,
        error: str = "",
        is_throttling: bool = False,
    ) -> None:
        """Record an error and update health status accordingly.

        Args:
            account: The account that had an error
            error: Error description
            is_throttling: Whether this was a throttling error
        """
        # Throttling errors should apply cooldown
        await self.mark_unhealthy(account, error, apply_cooldown=is_throttling)

    async def get_healthy_accounts(self, accounts: list[BedrockAccount]) -> list[BedrockAccount]:
        """Get list of healthy accounts from a list.

        Args:
            accounts: List of accounts to filter

        Returns:
            List of healthy accounts
        """
        async with self._lock:
            return [acc for acc in accounts if self._is_healthy_unlocked(acc)]

    async def get_status_summary(self, accounts: list[BedrockAccount]) -> dict[str, Any]:
        """Get a summary of health status across accounts.

        Args:
            accounts: List of accounts to summarize

        Returns:
            Summary dictionary with counts and per-account status
        """
        async with self._lock:
            healthy = 0
            unhealthy = 0
            cooldown = 0
            unknown = 0

            for account in accounts:
                if account.health_status == HealthStatus.HEALTHY:
                    healthy += 1
                elif account.health_status == HealthStatus.UNHEALTHY:
                    unhealthy += 1
                elif account.health_status == HealthStatus.COOLDOWN:
                    if self._is_cooldown_expired_unlocked(account):
                        healthy += 1  # Consider as healthy if cooldown expired
                    else:
                        cooldown += 1
                else:
                    unknown += 1

            return {
                "healthy": healthy,
                "unhealthy": unhealthy,
                "cooldown": cooldown,
                "unknown": unknown,
                "total": len(accounts),
            }
