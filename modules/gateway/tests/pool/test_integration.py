"""Integration tests for the pool module."""

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

from src.pool.config import BedrockAccountConfig, PoolConfig, PoolSettings
from src.pool.exceptions import AllAccountsUnhealthyError
from src.pool.health_tracker import HealthTracker
from src.pool.models import HealthStatus, PoolClient
from src.pool.service import PoolService
from src.pool.sts_client import STSClient


class TestPoolEndToEnd:
    """End-to-end integration tests for the pool module."""

    @pytest.fixture
    def integration_settings(self) -> PoolSettings:
        """Create settings for integration tests."""
        return PoolSettings(
            health_check_interval_seconds=60,
            cooldown_duration_seconds=2,  # Short cooldown for testing
            max_retries_per_request=3,
            retry_delay_seconds=0.1,
            sts_session_duration_seconds=3600,
            credential_refresh_margin_seconds=60,
        )

    @pytest.fixture
    def integration_config(self, integration_settings: PoolSettings) -> PoolConfig:
        """Create pool config for integration tests."""
        accounts = [
            BedrockAccountConfig(
                account_id="111111111111",
                role_arn="arn:aws:iam::111111111111:role/BedrockAccess",
                region="us-east-1",
            ),
            BedrockAccountConfig(
                account_id="222222222222",
                role_arn="arn:aws:iam::222222222222:role/BedrockAccess",
                region="us-west-2",
            ),
            BedrockAccountConfig(
                account_id="333333333333",
                role_arn="arn:aws:iam::333333333333:role/BedrockAccess",
                region="eu-west-1",
            ),
        ]
        return PoolConfig(accounts=accounts, settings=integration_settings)

    @pytest.fixture
    def mock_sts_for_integration(self) -> MagicMock:
        """Create mock STS client for integration tests."""
        mock_client = MagicMock()
        mock_client.assume_role.return_value = {
            "Credentials": {
                "AccessKeyId": "AKIAIOSFODNN7EXAMPLE",
                "SecretAccessKey": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
                "SessionToken": "AQoDYXdzEJr...",
                "Expiration": datetime.now(UTC) + timedelta(hours=1),
            },
        }
        return mock_client

    async def test_full_request_cycle(
        self,
        integration_config: PoolConfig,
        mock_sts_for_integration: MagicMock,
    ):
        """Test full request cycle: get client, use it, report success."""
        sts_client = STSClient(
            settings=integration_config.settings,
            sts_client=mock_sts_for_integration,
        )
        service = PoolService(config=integration_config, sts_client=sts_client)

        with patch("src.pool.service.boto3.client") as mock_boto:
            mock_boto.return_value = MagicMock()

            # Get a client
            pool_client = await service.get_client()
            assert isinstance(pool_client, PoolClient)
            account_id = pool_client.get_account_id()

            # Simulate successful request
            await service.report_success(account_id)

            # Verify account is healthy
            account = service._find_account(account_id)
            assert account.health_status == HealthStatus.HEALTHY
            assert account.request_count >= 1

    async def test_failover_on_error(
        self,
        integration_config: PoolConfig,
        mock_sts_for_integration: MagicMock,
    ):
        """Test failover when an account fails."""
        sts_client = STSClient(
            settings=integration_config.settings,
            sts_client=mock_sts_for_integration,
        )
        service = PoolService(config=integration_config, sts_client=sts_client)

        with patch("src.pool.service.boto3.client") as mock_boto:
            mock_boto.return_value = MagicMock()

            # Get first client
            client1 = await service.get_client()
            first_account = client1.account.account_id

            # Report error on first account
            await service.report_throttling(first_account)

            # Get second client - should skip the throttled account
            client2 = await service.get_client()
            second_account = client2.account.account_id

            # Should be different accounts
            assert second_account != first_account

    async def test_cooldown_recovery(
        self,
        integration_config: PoolConfig,
        mock_sts_for_integration: MagicMock,
    ):
        """Test that accounts recover after cooldown expires."""
        # Use very short cooldown for test
        integration_config.settings.cooldown_duration_seconds = 1

        sts_client = STSClient(
            settings=integration_config.settings,
            sts_client=mock_sts_for_integration,
        )
        health_tracker = HealthTracker(settings=integration_config.settings)
        service = PoolService(
            config=integration_config,
            sts_client=sts_client,
            health_tracker=health_tracker,
        )

        with patch("src.pool.service.boto3.client") as mock_boto:
            mock_boto.return_value = MagicMock()

            # Get client and mark all as throttled
            for account in service._accounts:
                await service.report_throttling(account.account_id)

            # All should be in cooldown
            status = await service.get_status()
            assert status.cooldown_accounts > 0

            # Wait for cooldown to expire
            await asyncio.sleep(1.5)

            # Now should be able to get a client
            client = await service.get_client()
            assert client is not None

    async def test_round_robin_distribution_under_load(
        self,
        integration_config: PoolConfig,
        mock_sts_for_integration: MagicMock,
    ):
        """Test that load is distributed evenly across accounts."""
        sts_client = STSClient(
            settings=integration_config.settings,
            sts_client=mock_sts_for_integration,
        )
        service = PoolService(config=integration_config, sts_client=sts_client)

        account_usage = {}
        num_requests = 30  # Should distribute evenly across 3 accounts

        with patch("src.pool.service.boto3.client") as mock_boto:
            mock_boto.return_value = MagicMock()

            for _ in range(num_requests):
                client = await service.get_client()
                account_id = client.account.account_id
                account_usage[account_id] = account_usage.get(account_id, 0) + 1
                await service.report_success(account_id)

        # Each account should have roughly equal usage
        for account_id, usage in account_usage.items():
            assert usage == num_requests // len(integration_config.accounts)

    async def test_concurrent_requests(
        self,
        integration_config: PoolConfig,
        mock_sts_for_integration: MagicMock,
    ):
        """Test handling concurrent requests."""
        sts_client = STSClient(
            settings=integration_config.settings,
            sts_client=mock_sts_for_integration,
        )
        service = PoolService(config=integration_config, sts_client=sts_client)

        with patch("src.pool.service.boto3.client") as mock_boto:
            mock_boto.return_value = MagicMock()

            # Launch many concurrent requests
            tasks = [service.get_client() for _ in range(50)]
            results = await asyncio.gather(*tasks)

            # All should succeed
            assert all(isinstance(r, PoolClient) for r in results)
            assert len(results) == 50

    async def test_all_accounts_unhealthy_error_us_9_4(
        self,
        integration_config: PoolConfig,
        mock_sts_for_integration: MagicMock,
    ):
        """Test US-9.4: All Bedrock Accounts Unhealthy."""
        sts_client = STSClient(
            settings=integration_config.settings,
            sts_client=mock_sts_for_integration,
        )
        service = PoolService(config=integration_config, sts_client=sts_client)

        # Put all accounts in long cooldown
        for account in service._accounts:
            account.health_status = HealthStatus.COOLDOWN
            account.cooldown_until = datetime.now(UTC) + timedelta(hours=1)

        with pytest.raises(AllAccountsUnhealthyError) as exc_info:
            await service.get_client()

        assert "unavailable" in str(exc_info.value).lower()

    async def test_pool_status_tracking(
        self,
        integration_config: PoolConfig,
        mock_sts_for_integration: MagicMock,
    ):
        """Test that pool status is tracked correctly."""
        sts_client = STSClient(
            settings=integration_config.settings,
            sts_client=mock_sts_for_integration,
        )
        service = PoolService(config=integration_config, sts_client=sts_client)

        with patch("src.pool.service.boto3.client") as mock_boto:
            mock_boto.return_value = MagicMock()

            # Initial status
            initial_status = await service.get_pool_status()
            assert len(initial_status) == len(integration_config.accounts)

            # Make some requests and errors
            client = await service.get_client()
            await service.report_success(client.account.account_id)

            client2 = await service.get_client()
            await service.report_throttling(client2.account.account_id)

            # Check updated status
            updated_status = await service.get_pool_status()
            throttled_account = next(s for s in updated_status if s["account_id"] == client2.account.account_id)
            assert throttled_account["health_status"] == "cooldown"

    async def test_initialization_validates_accounts(
        self,
        integration_config: PoolConfig,
        mock_sts_for_integration: MagicMock,
    ):
        """Test that initialization validates all accounts."""
        # Make first account fail STS assume role
        call_count = 0

        def mock_assume_role(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if kwargs.get("RoleArn", "").endswith("111111111111:role/BedrockAccess"):
                raise ClientError(
                    {"Error": {"Code": "AccessDenied", "Message": "Denied"}},
                    "AssumeRole",
                )
            return mock_sts_for_integration.assume_role.return_value

        mock_sts_for_integration.assume_role.side_effect = mock_assume_role

        sts_client = STSClient(
            settings=integration_config.settings,
            sts_client=mock_sts_for_integration,
        )
        service = PoolService(config=integration_config, sts_client=sts_client)

        await service.initialize()

        # First account should be unhealthy, others healthy
        status = await service.get_status()
        first_account = service._find_account("111111111111")
        assert first_account.health_status == HealthStatus.UNHEALTHY

        # At least some accounts should be healthy
        assert status.healthy_accounts >= 2


class TestPoolModuleImports:
    """Test that all exports are importable."""

    def test_main_exports(self):
        """Test main module exports."""
        from src.pool import (
            HealthTracker,
            PoolService,
            RoundRobinSelector,
            STSClient,
        )

        # All imports should work
        assert PoolService is not None
        assert HealthTracker is not None
        assert RoundRobinSelector is not None
        assert STSClient is not None

    def test_exception_hierarchy(self):
        """Test exception class hierarchy."""
        from src.pool.exceptions import (
            AllAccountsUnhealthyError,
            ClientCreationError,
            CredentialExpiredError,
            HealthCheckError,
            NoAccountsConfiguredError,
            PoolError,
            PoolExhaustedError,
            RoleAssumptionError,
        )

        # All should be subclasses of PoolError
        assert issubclass(AllAccountsUnhealthyError, PoolError)
        assert issubclass(ClientCreationError, PoolError)
        assert issubclass(CredentialExpiredError, PoolError)
        assert issubclass(HealthCheckError, PoolError)
        assert issubclass(NoAccountsConfiguredError, PoolError)
        assert issubclass(PoolExhaustedError, PoolError)
        assert issubclass(RoleAssumptionError, PoolError)
