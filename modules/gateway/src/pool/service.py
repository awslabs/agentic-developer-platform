"""PoolService implementation for cross-account Bedrock access.

Implements IPoolService interface with round-robin distribution (US-5.1)
and proper handling when all accounts are unhealthy (US-9.4).
"""

from typing import Any

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from src.pool.config import BedrockAccountConfig, PoolConfig
from src.pool.exceptions import (
    AllAccountsUnhealthyError,
    ClientCreationError,
    NoAccountsConfiguredError,
    PoolExhaustedError,
)
from src.pool.health_tracker import HealthTracker
from src.pool.models import BedrockAccount, HealthStatus, PoolClient, PoolStatus
from src.pool.selector import RoundRobinSelector
from src.pool.sts_client import STSClient
from src.shared.interfaces.pool import IPoolService
from src.shared.logging import get_logger
from src.shared.metrics import emit_pool_health

logger = get_logger(__name__)


class PoolService(IPoolService):
    """Service for managing a pool of Bedrock accounts with round-robin distribution.

    Implements:
    - US-5.1: Round-Robin Request Distribution
    - US-9.4: All Bedrock Accounts Unhealthy error handling
    """

    def __init__(
        self,
        config: PoolConfig | None = None,
        sts_client: STSClient | None = None,
        health_tracker: HealthTracker | None = None,
    ):
        """Initialize the pool service.

        Args:
            config: Pool configuration with account list and settings
            sts_client: Optional STS client for role assumption
            health_tracker: Optional health tracker instance
        """
        self._config = config or PoolConfig()
        self._settings = self._config.settings
        self._sts_client = sts_client or STSClient(settings=self._settings)
        self._health_tracker = health_tracker or HealthTracker(settings=self._settings)

        # Initialize accounts from config with HEALTHY status
        # (validation happens during initialize() if called)
        self._accounts: list[BedrockAccount] = []
        for acc_config in self._config.accounts:
            account = BedrockAccount(
                account_id=acc_config.account_id,
                role_arn=acc_config.role_arn,
                region=acc_config.region,
                external_id=acc_config.external_id,
                health_status=HealthStatus.HEALTHY,  # Start as healthy, validate later if needed
            )
            self._accounts.append(account)

        # Initialize round-robin selector
        self._selector = RoundRobinSelector(
            accounts=self._accounts,
            health_check=lambda acc: acc.is_healthy(),
        )

        logger.info(f"PoolService initialized with {len(self._accounts)} accounts")

    async def get_client(self) -> Any:
        """Get a Bedrock client from the pool using round-robin distribution.

        Returns:
            A PoolClient wrapper with account info and Bedrock client

        Raises:
            AllAccountsUnhealthyError: When all accounts in the pool are unhealthy (US-9.4)
            NoAccountsConfiguredError: When no accounts are configured
            PoolExhaustedError: When all accounts fail to serve the request
        """
        if not self._accounts:
            raise NoAccountsConfiguredError()

        max_attempts = min(len(self._accounts), self._settings.max_retries_per_request)
        attempts = 0
        errors: list[tuple[str, str]] = []

        while attempts < max_attempts:
            # Get next healthy account using round-robin
            account = await self._selector.get_next_healthy_account()

            if account is None:
                # All accounts are unhealthy - raise US-9.4 error
                logger.error("All Bedrock accounts are unhealthy")
                raise AllAccountsUnhealthyError()

            try:
                client = await self._create_bedrock_client(account)
                await self._health_tracker.record_request(account)
                return PoolClient(account=account, client=client)

            except ClientCreationError as e:
                logger.warning(f"Failed to create client for account {account.account_id}: {e}")
                await self._health_tracker.mark_unhealthy(account, str(e))
                errors.append((account.account_id, str(e)))
                attempts += 1

        # All attempts exhausted
        logger.error(f"All {attempts} pool attempts failed: {errors}")
        raise PoolExhaustedError(attempts)

    async def report_error(self, account_id: str) -> None:
        """Report an error for a specific account.

        Called by the Proxy unit when a request to an account fails.
        This triggers health tracking and potential cooldown.

        Args:
            account_id: The ID of the account that had an error
        """
        account = self._find_account(account_id)
        if account:
            await self._health_tracker.record_error(
                account,
                error="Error reported by proxy",
                is_throttling=True,  # Assume throttling for conservative handling
            )
            logger.info(f"Error reported for account {account_id}")
        else:
            logger.warning(f"Unknown account ID reported: {account_id}")

    async def get_pool_status(self) -> list[dict[str, Any]]:
        """Get the status of all accounts in the pool.

        Returns:
            List of dictionaries with account status information
        """
        return [account.to_status_dict() for account in self._accounts]

    async def report_success(self, account_id: str) -> None:
        """Report a successful request for an account.

        Called by the Proxy unit when a request succeeds.
        This helps recover accounts from cooldown.

        Args:
            account_id: The ID of the account that succeeded
        """
        account = self._find_account(account_id)
        if account:
            await self._health_tracker.record_success(account)

    async def report_throttling(self, account_id: str) -> None:
        """Report a throttling error for an account.

        This puts the account into cooldown.

        Args:
            account_id: The ID of the throttled account
        """
        account = self._find_account(account_id)
        if account:
            await self._health_tracker.record_error(
                account,
                error="Throttling error",
                is_throttling=True,
            )
            logger.warning(f"Throttling reported for account {account_id}")

    async def _create_bedrock_client(self, account: BedrockAccount) -> Any:
        """Create a Bedrock runtime client for an account.

        Args:
            account: The account to create a client for

        Returns:
            boto3 Bedrock runtime client

        Raises:
            ClientCreationError: If client creation fails
        """
        try:
            # Get account config for credential lookup
            acc_config = self._find_account_config(account.account_id)
            if not acc_config:
                raise ClientCreationError(account.account_id, "Account config not found")

            # Assume role to get credentials
            credentials = await self._sts_client.assume_role(acc_config)

            # Create Bedrock runtime client with assumed credentials
            client = boto3.client(
                "bedrock-runtime",
                region_name=account.region,
                aws_access_key_id=credentials.access_key_id,
                aws_secret_access_key=credentials.secret_access_key,
                aws_session_token=credentials.session_token,
            )

            # Update cached credentials in account
            account.credentials = credentials

            return client

        except ClientError as e:
            error_msg = str(e)
            raise ClientCreationError(account.account_id, error_msg) from e
        except BotoCoreError as e:
            raise ClientCreationError(account.account_id, str(e)) from e
        except Exception as e:
            raise ClientCreationError(account.account_id, str(e)) from e

    def _find_account(self, account_id: str) -> BedrockAccount | None:
        """Find an account by ID.

        Args:
            account_id: The account ID to find

        Returns:
            The account if found, None otherwise
        """
        for account in self._accounts:
            if account.account_id == account_id:
                return account
        return None

    def _find_account_config(self, account_id: str) -> BedrockAccountConfig | None:
        """Find account configuration by ID.

        Args:
            account_id: The account ID to find

        Returns:
            The account config if found, None otherwise
        """
        for config in self._config.accounts:
            if config.account_id == account_id:
                return config
        return None

    async def get_status(self) -> PoolStatus:
        """Get comprehensive pool status.

        Returns:
            PoolStatus with counts and account details
        """
        summary = await self._health_tracker.get_status_summary(self._accounts)

        # Emit pool health metrics
        emit_pool_health(
            healthy_count=summary["healthy"],
            unhealthy_count=summary["unhealthy"] + summary["cooldown"],
        )

        logger.debug(
            "Pool status retrieved",
            extra={
                "total_accounts": len(self._accounts),
                "healthy": summary["healthy"],
                "unhealthy": summary["unhealthy"],
                "cooldown": summary["cooldown"],
            },
        )

        return PoolStatus(
            total_accounts=len(self._accounts),
            healthy_accounts=summary["healthy"],
            unhealthy_accounts=summary["unhealthy"],
            cooldown_accounts=summary["cooldown"],
            accounts=[acc.to_status_dict() for acc in self._accounts],
        )

    async def initialize(self) -> None:
        """Initialize the pool by validating all accounts.

        Attempts to assume role for each account to verify access.
        Marks accounts as healthy or unhealthy based on results.
        """
        logger.info("Initializing pool service, validating account access...")

        for account in self._accounts:
            try:
                acc_config = self._find_account_config(account.account_id)
                if acc_config:
                    await self._sts_client.assume_role(acc_config)
                    await self._health_tracker.mark_healthy(account)
                    logger.info(f"Account {account.account_id} validated successfully")
            except Exception as e:
                await self._health_tracker.mark_unhealthy(account, str(e), apply_cooldown=False)
                logger.error(f"Account {account.account_id} validation failed: {e}")

        # Log summary
        status = await self.get_status()
        logger.info(f"Pool initialization complete: {status.healthy_accounts}/{status.total_accounts} accounts healthy")

    async def add_account(self, config: BedrockAccountConfig) -> None:
        """Add a new account to the pool.

        Args:
            config: Configuration for the new account
        """
        account = BedrockAccount(
            account_id=config.account_id,
            role_arn=config.role_arn,
            region=config.region,
            external_id=config.external_id,
            health_status=HealthStatus.HEALTHY,  # Start as healthy
        )
        # Note: Only append to _accounts. The selector shares the same list reference,
        # so it will automatically see the new account.
        self._accounts.append(account)
        self._config.accounts.append(config)
        logger.info(f"Added account {config.account_id} to pool")

    async def remove_account(self, account_id: str) -> bool:
        """Remove an account from the pool.

        Args:
            account_id: ID of the account to remove

        Returns:
            True if account was removed, False if not found
        """
        # Remove from accounts list. The selector shares the same list reference,
        # so we only need to remove from _accounts directly.
        account_removed = False
        for i, acc in enumerate(self._accounts):
            if acc.account_id == account_id:
                self._accounts.pop(i)
                account_removed = True
                break

        # Remove from config
        for i, conf in enumerate(self._config.accounts):
            if conf.account_id == account_id:
                self._config.accounts.pop(i)
                break

        if account_removed:
            logger.info(f"Removed account {account_id} from pool")
        return account_removed
