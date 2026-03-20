"""Unit tests for the PoolService."""

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

from src.pool.config import BedrockAccountConfig, PoolConfig, PoolSettings
from src.pool.exceptions import (
    AllAccountsUnhealthyError,
    NoAccountsConfiguredError,
    PoolExhaustedError,
)
from src.pool.models import HealthStatus, PoolClient
from src.pool.service import PoolService
from src.pool.sts_client import STSClient


class TestPoolService:
    """Tests for PoolService class."""

    async def test_get_client_returns_pool_client(
        self,
        pool_service_with_mocks: PoolService,
    ):
        """Test that get_client returns a PoolClient wrapper."""
        with patch("src.pool.service.boto3.client") as mock_boto:
            mock_boto.return_value = MagicMock()

            client = await pool_service_with_mocks.get_client()

            assert isinstance(client, PoolClient)
            assert client.account is not None
            assert client.client is not None

    async def test_get_client_round_robin_distribution(
        self,
        pool_config: PoolConfig,
        sts_client_wrapper: STSClient,
    ):
        """Test that get_client distributes requests round-robin (US-5.1)."""
        service = PoolService(
            config=pool_config,
            sts_client=sts_client_wrapper,
        )

        selected_accounts = []
        with patch("src.pool.service.boto3.client") as mock_boto:
            mock_boto.return_value = MagicMock()

            # Get clients multiple times
            for _ in range(len(pool_config.accounts)):
                client = await service.get_client()
                selected_accounts.append(client.account.account_id)

        # Should get each account once in order
        expected_accounts = [acc.account_id for acc in pool_config.accounts]
        assert selected_accounts == expected_accounts

    async def test_get_client_raises_no_accounts_configured(self):
        """Test that NoAccountsConfiguredError is raised for empty pool."""
        config = PoolConfig(accounts=[], settings=PoolSettings())
        service = PoolService(config=config)

        with pytest.raises(NoAccountsConfiguredError):
            await service.get_client()

    async def test_get_client_raises_all_accounts_unhealthy(
        self,
        pool_settings: PoolSettings,
        sts_client_wrapper: STSClient,
    ):
        """Test that AllAccountsUnhealthyError is raised when all unhealthy (US-9.4)."""
        # Create config with accounts
        configs = [
            BedrockAccountConfig(
                account_id="111111111111",
                role_arn="arn:aws:iam::111111111111:role/Test",
                region="us-east-1",
            ),
            BedrockAccountConfig(
                account_id="222222222222",
                role_arn="arn:aws:iam::222222222222:role/Test",
                region="us-east-1",
            ),
        ]
        config = PoolConfig(accounts=configs, settings=pool_settings)
        service = PoolService(config=config, sts_client=sts_client_wrapper)

        # Mark all accounts as unhealthy
        for account in service._accounts:
            account.health_status = HealthStatus.COOLDOWN
            account.cooldown_until = datetime.now(UTC) + timedelta(hours=1)

        with pytest.raises(AllAccountsUnhealthyError):
            await service.get_client()

    async def test_get_client_skips_unhealthy_accounts(
        self,
        pool_config: PoolConfig,
        sts_client_wrapper: STSClient,
    ):
        """Test that get_client skips unhealthy accounts."""
        service = PoolService(
            config=pool_config,
            sts_client=sts_client_wrapper,
        )

        # Mark first account as unhealthy
        service._accounts[0].health_status = HealthStatus.COOLDOWN
        service._accounts[0].cooldown_until = datetime.now(UTC) + timedelta(hours=1)

        with patch("src.pool.service.boto3.client") as mock_boto:
            mock_boto.return_value = MagicMock()

            client = await service.get_client()

            # Should get second account since first is unhealthy
            assert client.account.account_id == pool_config.accounts[1].account_id

    async def test_get_client_handles_client_creation_failure(
        self,
        pool_config: PoolConfig,
        sts_client_wrapper: STSClient,
    ):
        """Test failover when client creation fails."""
        service = PoolService(
            config=pool_config,
            sts_client=sts_client_wrapper,
        )

        call_count = 0

        def mock_boto_client(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ClientError(
                    {"Error": {"Code": "ServiceException", "Message": "Failed"}},
                    "CreateClient",
                )
            return MagicMock()

        with patch("src.pool.service.boto3.client", side_effect=mock_boto_client):
            client = await service.get_client()

            # Should succeed with second account after first fails
            assert client is not None
            assert call_count >= 2

    async def test_report_error_marks_account_unhealthy(
        self,
        pool_service_with_mocks: PoolService,
    ):
        """Test that report_error marks account as unhealthy."""
        account_id = pool_service_with_mocks._accounts[0].account_id

        await pool_service_with_mocks.report_error(account_id)

        account = pool_service_with_mocks._find_account(account_id)
        assert account.health_status in (HealthStatus.COOLDOWN, HealthStatus.UNHEALTHY)
        assert account.error_count > 0

    async def test_report_error_ignores_unknown_account(
        self,
        pool_service_with_mocks: PoolService,
    ):
        """Test that report_error handles unknown accounts gracefully."""
        # Should not raise
        await pool_service_with_mocks.report_error("unknown-account-id")

    async def test_get_pool_status(
        self,
        pool_service_with_mocks: PoolService,
    ):
        """Test getting pool status."""
        status = await pool_service_with_mocks.get_pool_status()

        assert isinstance(status, list)
        assert len(status) == len(pool_service_with_mocks._accounts)
        for account_status in status:
            assert "account_id" in account_status
            assert "health_status" in account_status
            assert "region" in account_status

    async def test_report_success_recovers_account(
        self,
        pool_service_with_mocks: PoolService,
    ):
        """Test that report_success recovers account from cooldown."""
        account = pool_service_with_mocks._accounts[0]
        account.health_status = HealthStatus.COOLDOWN
        account.cooldown_until = datetime.now(UTC) + timedelta(hours=1)
        account.consecutive_failures = 3

        await pool_service_with_mocks.report_success(account.account_id)

        assert account.health_status == HealthStatus.HEALTHY
        assert account.cooldown_until is None
        assert account.consecutive_failures == 0

    async def test_report_throttling_applies_cooldown(
        self,
        pool_service_with_mocks: PoolService,
    ):
        """Test that report_throttling applies cooldown."""
        account = pool_service_with_mocks._accounts[0]
        account.health_status = HealthStatus.HEALTHY

        await pool_service_with_mocks.report_throttling(account.account_id)

        assert account.health_status == HealthStatus.COOLDOWN
        assert account.cooldown_until is not None

    async def test_get_status(
        self,
        pool_service_with_mocks: PoolService,
    ):
        """Test getting comprehensive pool status."""
        status = await pool_service_with_mocks.get_status()

        assert status.total_accounts == len(pool_service_with_mocks._accounts)
        assert status.healthy_accounts >= 0
        assert status.unhealthy_accounts >= 0
        assert isinstance(status.accounts, list)

    async def test_initialize_validates_accounts(
        self,
        pool_service_with_mocks: PoolService,
    ):
        """Test that initialize validates all accounts."""
        with patch("src.pool.service.boto3.client") as mock_boto:
            mock_boto.return_value = MagicMock()

            await pool_service_with_mocks.initialize()

            # All accounts should be checked
            for account in pool_service_with_mocks._accounts:
                assert account.last_health_check is not None

    async def test_add_account(self):
        """Test adding a new account to the pool."""
        from datetime import UTC, datetime, timedelta

        # Create completely fresh objects inline
        mock_boto_sts = MagicMock()
        mock_boto_sts.assume_role.return_value = {
            "Credentials": {
                "AccessKeyId": "AKIAEXAMPLE",
                "SecretAccessKey": "secret",
                "SessionToken": "token",
                "Expiration": datetime.now(UTC) + timedelta(hours=1),
            },
        }

        settings = PoolSettings(
            health_check_interval_seconds=60,
            cooldown_duration_seconds=30,
            max_retries_per_request=3,
        )
        # Create fresh configs as a new list each time
        config = PoolConfig(
            accounts=[
                BedrockAccountConfig(
                    account_id="aaa111111111",
                    role_arn="arn:aws:iam::aaa111111111:role/Test",
                    region="us-east-1",
                ),
                BedrockAccountConfig(
                    account_id="bbb222222222",
                    role_arn="arn:aws:iam::bbb222222222:role/Test",
                    region="us-west-2",
                ),
            ],
            settings=settings,
        )
        sts_client = STSClient(settings=settings, sts_client=mock_boto_sts)
        service = PoolService(config=config, sts_client=sts_client)
        initial_count = len(service._accounts)
        assert initial_count == 2, f"Expected 2 accounts but got {initial_count}"

        new_config = BedrockAccountConfig(
            account_id="ccc333333333",
            role_arn="arn:aws:iam::ccc333333333:role/Test",
            region="eu-west-1",
        )

        await service.add_account(new_config)

        assert len(service._accounts) == 3
        assert service._find_account("ccc333333333") is not None

    async def test_remove_account(
        self,
        pool_service_with_mocks: PoolService,
    ):
        """Test removing an account from the pool."""
        account_id = pool_service_with_mocks._accounts[0].account_id
        initial_count = len(pool_service_with_mocks._accounts)

        result = await pool_service_with_mocks.remove_account(account_id)

        assert result is True
        assert len(pool_service_with_mocks._accounts) == initial_count - 1
        assert pool_service_with_mocks._find_account(account_id) is None

    async def test_remove_nonexistent_account(
        self,
        pool_service_with_mocks: PoolService,
    ):
        """Test removing a nonexistent account."""
        result = await pool_service_with_mocks.remove_account("nonexistent")

        assert result is False


class TestPoolServiceInterfaceCompliance:
    """Tests to verify PoolService implements IPoolService correctly."""

    async def test_implements_get_client(self, pool_service_with_mocks: PoolService):
        """Test that get_client is implemented."""
        assert hasattr(pool_service_with_mocks, "get_client")
        assert callable(pool_service_with_mocks.get_client)

    async def test_implements_report_error(self, pool_service_with_mocks: PoolService):
        """Test that report_error is implemented."""
        assert hasattr(pool_service_with_mocks, "report_error")
        assert callable(pool_service_with_mocks.report_error)

    async def test_implements_get_pool_status(self, pool_service_with_mocks: PoolService):
        """Test that get_pool_status is implemented."""
        assert hasattr(pool_service_with_mocks, "get_pool_status")
        assert callable(pool_service_with_mocks.get_pool_status)

    async def test_get_client_return_type(self, pool_service_with_mocks: PoolService):
        """Test that get_client returns correct type."""
        with patch("src.pool.service.boto3.client") as mock_boto:
            mock_boto.return_value = MagicMock()
            client = await pool_service_with_mocks.get_client()
            # Should return something with account info
            assert hasattr(client, "account")

    async def test_get_pool_status_return_type(self, pool_service_with_mocks: PoolService):
        """Test that get_pool_status returns list of dicts."""
        status = await pool_service_with_mocks.get_pool_status()
        assert isinstance(status, list)
        for item in status:
            assert isinstance(item, dict)


class TestPoolServiceErrorScenarios:
    """Tests for error scenarios in PoolService."""

    async def test_exhausts_all_accounts_raises_pool_exhausted(
        self,
        pool_settings: PoolSettings,
    ):
        """Test that PoolExhaustedError is raised when all accounts fail."""
        # Create mock STS that always fails
        mock_sts = MagicMock()
        mock_sts.assume_role.side_effect = ClientError(
            {"Error": {"Code": "AccessDenied", "Message": "Denied"}},
            "AssumeRole",
        )

        configs = [
            BedrockAccountConfig(
                account_id="111111111111",
                role_arn="arn:aws:iam::111111111111:role/Test",
                region="us-east-1",
            ),
        ]
        config = PoolConfig(accounts=configs, settings=pool_settings)
        sts_client = STSClient(settings=pool_settings, sts_client=mock_sts)
        service = PoolService(config=config, sts_client=sts_client)

        with pytest.raises((PoolExhaustedError, AllAccountsUnhealthyError)):
            await service.get_client()

    async def test_recovers_after_cooldown_expires(
        self,
        pool_config: PoolConfig,
        sts_client_wrapper: STSClient,
    ):
        """Test that service recovers accounts after cooldown expires."""
        service = PoolService(
            config=pool_config,
            sts_client=sts_client_wrapper,
        )

        # Put first account in expired cooldown
        service._accounts[0].health_status = HealthStatus.COOLDOWN
        service._accounts[0].cooldown_until = datetime.now(UTC) - timedelta(seconds=60)

        with patch("src.pool.service.boto3.client") as mock_boto:
            mock_boto.return_value = MagicMock()

            client = await service.get_client()

            # Should be able to use the first account again
            # (depends on round-robin order, might get any healthy account)
            assert client is not None
