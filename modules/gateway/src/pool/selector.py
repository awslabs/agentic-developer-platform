"""Round-robin selector for Bedrock account distribution (US-5.1)."""

import asyncio
import logging
from collections.abc import Callable

from src.pool.models import BedrockAccount

logger = logging.getLogger(__name__)


class RoundRobinSelector:
    """Selects Bedrock accounts using round-robin distribution.

    Implements round-robin selection with support for skipping unhealthy accounts.
    Thread-safe using asyncio locks.
    """

    def __init__(
        self,
        accounts: list[BedrockAccount],
        health_check: Callable[[BedrockAccount], bool] | None = None,
    ):
        """Initialize the round-robin selector.

        Args:
            accounts: List of Bedrock accounts to select from
            health_check: Optional callable to check if account is healthy
        """
        self._accounts = accounts
        self._health_check = health_check or (lambda acc: acc.is_healthy())
        self._current_index = 0
        self._lock = asyncio.Lock()

    async def get_next_account(self, skip_unhealthy: bool = True) -> BedrockAccount | None:
        """Get the next account in round-robin order.

        Args:
            skip_unhealthy: If True, skip unhealthy accounts

        Returns:
            The next available account, or None if no healthy accounts available
        """
        async with self._lock:
            if not self._accounts:
                return None

            start_index = self._current_index
            attempts = 0
            max_attempts = len(self._accounts)

            while attempts < max_attempts:
                account = self._accounts[self._current_index]
                self._advance_index()

                if not skip_unhealthy or self._health_check(account):
                    logger.debug(f"Selected account {account.account_id} (index {start_index})")
                    return account

                attempts += 1

            # All accounts are unhealthy
            logger.warning("No healthy accounts available in the pool")
            return None

    async def get_next_healthy_account(self) -> BedrockAccount | None:
        """Get the next healthy account (convenience method).

        Returns:
            The next healthy account, or None if all are unhealthy
        """
        return await self.get_next_account(skip_unhealthy=True)

    async def get_all_healthy_accounts(self) -> list[BedrockAccount]:
        """Get all currently healthy accounts.

        Returns:
            List of healthy accounts
        """
        async with self._lock:
            return [acc for acc in self._accounts if self._health_check(acc)]

    async def skip_account(self, account_id: str) -> None:
        """Skip a specific account in the next selection.

        This advances the index past the specified account if it's next.

        Args:
            account_id: ID of the account to skip
        """
        async with self._lock:
            if not self._accounts:
                return

            # If the current next account matches, advance past it
            if self._accounts[self._current_index].account_id == account_id:
                self._advance_index()
                logger.debug(f"Skipped account {account_id}")

    async def reset_position(self) -> None:
        """Reset the selector to the beginning of the list."""
        async with self._lock:
            self._current_index = 0
            logger.debug("Selector position reset to 0")

    async def set_position(self, index: int) -> None:
        """Set the selector position to a specific index.

        Args:
            index: The index to set (will be wrapped if out of bounds)
        """
        async with self._lock:
            if self._accounts:
                self._current_index = index % len(self._accounts)
            else:
                self._current_index = 0

    async def get_current_position(self) -> int:
        """Get the current selector position.

        Returns:
            Current index position
        """
        async with self._lock:
            return self._current_index

    async def add_account(self, account: BedrockAccount) -> None:
        """Add an account to the pool.

        Args:
            account: Account to add
        """
        async with self._lock:
            self._accounts.append(account)
            logger.info(f"Added account {account.account_id} to pool")

    async def remove_account(self, account_id: str) -> bool:
        """Remove an account from the pool.

        Args:
            account_id: ID of the account to remove

        Returns:
            True if account was removed, False if not found
        """
        async with self._lock:
            for i, acc in enumerate(self._accounts):
                if acc.account_id == account_id:
                    self._accounts.pop(i)
                    # Adjust index if needed
                    if i <= self._current_index and self._current_index > 0:
                        self._current_index -= 1
                    if self._accounts and self._current_index >= len(self._accounts):
                        self._current_index = 0
                    logger.info(f"Removed account {account_id} from pool")
                    return True
            return False

    async def get_account_count(self) -> int:
        """Get the total number of accounts in the pool.

        Returns:
            Number of accounts
        """
        async with self._lock:
            return len(self._accounts)

    async def get_healthy_count(self) -> int:
        """Get the number of healthy accounts.

        Returns:
            Number of healthy accounts
        """
        async with self._lock:
            return sum(1 for acc in self._accounts if self._health_check(acc))

    def _advance_index(self) -> None:
        """Advance the current index (must be called with lock held)."""
        if self._accounts:
            self._current_index = (self._current_index + 1) % len(self._accounts)

    async def get_accounts_in_order(self) -> list[BedrockAccount]:
        """Get accounts in their current order starting from current position.

        Returns:
            List of accounts starting from current position
        """
        async with self._lock:
            if not self._accounts:
                return []

            # Return accounts starting from current position
            result = []
            for i in range(len(self._accounts)):
                idx = (self._current_index + i) % len(self._accounts)
                result.append(self._accounts[idx])
            return result
