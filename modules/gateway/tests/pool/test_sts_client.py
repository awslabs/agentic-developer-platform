"""Unit tests for the STS client wrapper."""

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

from src.pool.config import BedrockAccountConfig, PoolSettings
from src.pool.exceptions import CredentialExpiredError, RoleAssumptionError
from src.pool.models import AssumedRoleCredentials
from src.pool.sts_client import STSClient


class TestSTSClient:
    """Tests for STSClient class."""

    @pytest.fixture
    def account_config(self) -> BedrockAccountConfig:
        """Create test account config."""
        return BedrockAccountConfig(
            account_id="111111111111",
            role_arn="arn:aws:iam::111111111111:role/BedrockAccess",
            region="us-east-1",
        )

    @pytest.fixture
    def account_config_with_external_id(self) -> BedrockAccountConfig:
        """Create test account config with external ID."""
        return BedrockAccountConfig(
            account_id="222222222222",
            role_arn="arn:aws:iam::222222222222:role/BedrockAccess",
            region="us-west-2",
            external_id="external-id-123",
        )

    async def test_assume_role_success(
        self,
        sts_client_wrapper: STSClient,
        account_config: BedrockAccountConfig,
    ):
        """Test successful role assumption."""
        credentials = await sts_client_wrapper.assume_role(account_config)

        assert credentials is not None
        assert credentials.access_key_id == "AKIAIOSFODNN7EXAMPLE"
        assert credentials.secret_access_key == "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
        assert credentials.session_token == "AQoDYXdzEJr..."
        assert credentials.expiration > datetime.now(UTC)

    async def test_assume_role_with_external_id(
        self,
        mock_sts_client: MagicMock,
        pool_settings: PoolSettings,
        account_config_with_external_id: BedrockAccountConfig,
    ):
        """Test role assumption with external ID."""
        sts_client = STSClient(settings=pool_settings, sts_client=mock_sts_client)

        await sts_client.assume_role(account_config_with_external_id)

        # Verify ExternalId was passed
        call_args = mock_sts_client.assume_role.call_args
        assert call_args.kwargs.get("ExternalId") == "external-id-123"

    async def test_assume_role_caches_credentials(
        self,
        sts_client_wrapper: STSClient,
        account_config: BedrockAccountConfig,
        mock_sts_client: MagicMock,
    ):
        """Test that credentials are cached after assumption."""
        # First call
        credentials1 = await sts_client_wrapper.assume_role(account_config)

        # Second call should use cache
        credentials2 = await sts_client_wrapper.assume_role(account_config)

        # Should only call STS once
        assert mock_sts_client.assume_role.call_count == 1
        assert credentials1 == credentials2

    async def test_assume_role_force_refresh_bypasses_cache(
        self,
        sts_client_wrapper: STSClient,
        account_config: BedrockAccountConfig,
        mock_sts_client: MagicMock,
    ):
        """Test that force_refresh bypasses the cache."""
        # First call
        await sts_client_wrapper.assume_role(account_config)

        # Second call with force refresh
        await sts_client_wrapper.assume_role(account_config, force_refresh=True)

        # Should call STS twice
        assert mock_sts_client.assume_role.call_count == 2

    async def test_assume_role_access_denied_error(
        self,
        pool_settings: PoolSettings,
        account_config: BedrockAccountConfig,
    ):
        """Test handling of access denied error."""
        mock_client = MagicMock()
        mock_client.assume_role.side_effect = ClientError(
            {"Error": {"Code": "AccessDenied", "Message": "Not authorized"}},
            "AssumeRole",
        )

        sts_client = STSClient(settings=pool_settings, sts_client=mock_client)

        with pytest.raises(RoleAssumptionError) as exc_info:
            await sts_client.assume_role(account_config)

        assert "AccessDenied" in str(exc_info.value)
        assert account_config.role_arn in str(exc_info.value)

    async def test_assume_role_malformed_policy_error(
        self,
        pool_settings: PoolSettings,
        account_config: BedrockAccountConfig,
    ):
        """Test handling of malformed policy error."""
        mock_client = MagicMock()
        mock_client.assume_role.side_effect = ClientError(
            {"Error": {"Code": "MalformedPolicyDocument", "Message": "Invalid policy"}},
            "AssumeRole",
        )

        sts_client = STSClient(settings=pool_settings, sts_client=mock_client)

        with pytest.raises(RoleAssumptionError) as exc_info:
            await sts_client.assume_role(account_config)

        assert "MalformedPolicyDocument" in str(exc_info.value)

    async def test_get_cached_credentials_returns_valid(
        self,
        sts_client_wrapper: STSClient,
        account_config: BedrockAccountConfig,
    ):
        """Test getting valid cached credentials."""
        # Assume role to populate cache
        await sts_client_wrapper.assume_role(account_config)

        # Get cached credentials
        cached = await sts_client_wrapper.get_cached_credentials(account_config.role_arn)

        assert cached is not None
        assert cached.access_key_id == "AKIAIOSFODNN7EXAMPLE"

    async def test_get_cached_credentials_returns_none_for_unknown(
        self,
        sts_client_wrapper: STSClient,
    ):
        """Test that unknown role ARN returns None."""
        cached = await sts_client_wrapper.get_cached_credentials("arn:aws:iam::999999999999:role/Unknown")
        assert cached is None

    async def test_refresh_credentials_success(
        self,
        sts_client_wrapper: STSClient,
        account_config: BedrockAccountConfig,
    ):
        """Test successful credential refresh."""
        credentials = await sts_client_wrapper.refresh_credentials(account_config)
        assert credentials is not None
        assert not credentials.is_expired()

    async def test_refresh_credentials_failure_raises_error(
        self,
        pool_settings: PoolSettings,
        account_config: BedrockAccountConfig,
    ):
        """Test credential refresh failure raises CredentialExpiredError."""
        mock_client = MagicMock()
        mock_client.assume_role.side_effect = ClientError(
            {"Error": {"Code": "AccessDenied", "Message": "Not authorized"}},
            "AssumeRole",
        )

        sts_client = STSClient(settings=pool_settings, sts_client=mock_client)

        with pytest.raises(CredentialExpiredError) as exc_info:
            await sts_client.refresh_credentials(account_config)

        assert account_config.account_id in str(exc_info.value)

    async def test_clear_cache_specific_role(
        self,
        sts_client_wrapper: STSClient,
        account_config: BedrockAccountConfig,
        mock_sts_client: MagicMock,
    ):
        """Test clearing cache for a specific role."""
        # Assume role to populate cache
        await sts_client_wrapper.assume_role(account_config)
        assert mock_sts_client.assume_role.call_count == 1

        # Clear specific cache
        sts_client_wrapper.clear_cache(account_config.role_arn)

        # Next call should hit STS again
        await sts_client_wrapper.assume_role(account_config)
        assert mock_sts_client.assume_role.call_count == 2

    async def test_clear_cache_all(
        self,
        sts_client_wrapper: STSClient,
        account_config: BedrockAccountConfig,
        mock_sts_client: MagicMock,
    ):
        """Test clearing all cached credentials."""
        # Assume role to populate cache
        await sts_client_wrapper.assume_role(account_config)

        # Clear all cache
        sts_client_wrapper.clear_cache()

        # Next call should hit STS again
        await sts_client_wrapper.assume_role(account_config)
        assert mock_sts_client.assume_role.call_count == 2

    async def test_get_cache_status(
        self,
        sts_client_wrapper: STSClient,
        account_config: BedrockAccountConfig,
    ):
        """Test getting cache status."""
        # Assume role to populate cache
        await sts_client_wrapper.assume_role(account_config)

        # Get status
        status = sts_client_wrapper.get_cache_status()

        assert account_config.role_arn in status
        assert "expires_at" in status[account_config.role_arn]
        assert "remaining_seconds" in status[account_config.role_arn]
        assert status[account_config.role_arn]["is_valid"] is True


class TestAssumedRoleCredentials:
    """Tests for AssumedRoleCredentials model."""

    def test_is_expired_returns_false_for_valid(self, mock_credentials: AssumedRoleCredentials):
        """Test that valid credentials are not expired."""
        assert mock_credentials.is_expired() is False

    def test_is_expired_returns_true_for_expired(self, expired_credentials: AssumedRoleCredentials):
        """Test that expired credentials are detected."""
        assert expired_credentials.is_expired() is True

    def test_is_expired_with_margin(self):
        """Test expiration check with margin."""
        # Credentials that expire in 30 seconds
        credentials = AssumedRoleCredentials(
            access_key_id="AKIAIOSFODNN7EXAMPLE",
            secret_access_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
            session_token="AQoDYXdzEJr...",
            expiration=datetime.now(UTC) + timedelta(seconds=30),
        )

        # Without margin, not expired
        assert credentials.is_expired(margin_seconds=0) is False

        # With 60 second margin, considered expired
        assert credentials.is_expired(margin_seconds=60) is True
