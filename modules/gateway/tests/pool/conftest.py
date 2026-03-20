"""Test fixtures for pool module tests."""

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest

from src.pool.config import BedrockAccountConfig, PoolConfig, PoolSettings
from src.pool.health_tracker import HealthTracker
from src.pool.models import AssumedRoleCredentials, BedrockAccount, HealthStatus
from src.pool.selector import RoundRobinSelector
from src.pool.service import PoolService
from src.pool.sts_client import STSClient


@pytest.fixture
def sample_account_configs() -> list[BedrockAccountConfig]:
    """Create sample account configurations for testing."""
    return [
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
            external_id="test-external-id",
        ),
    ]


@pytest.fixture
def sample_accounts(sample_account_configs: list[BedrockAccountConfig]) -> list[BedrockAccount]:
    """Create sample BedrockAccount instances for testing."""
    return [
        BedrockAccount(
            account_id=config.account_id,
            role_arn=config.role_arn,
            region=config.region,
            external_id=config.external_id,
            health_status=HealthStatus.HEALTHY,
        )
        for config in sample_account_configs
    ]


@pytest.fixture
def pool_settings() -> PoolSettings:
    """Create pool settings for testing."""
    return PoolSettings(
        health_check_interval_seconds=60,
        cooldown_duration_seconds=30,
        max_retries_per_request=3,
        retry_delay_seconds=0.1,
        sts_session_duration_seconds=3600,
        sts_session_name="TestSession",
        credential_refresh_margin_seconds=60,
    )


@pytest.fixture
def pool_config(pool_settings: PoolSettings) -> PoolConfig:
    """Create pool configuration for testing. Creates fresh accounts each time to avoid test pollution."""
    fresh_configs = [
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
            external_id="test-external-id",
        ),
    ]
    return PoolConfig(accounts=fresh_configs, settings=pool_settings)


@pytest.fixture
def mock_credentials() -> AssumedRoleCredentials:
    """Create mock STS credentials."""
    return AssumedRoleCredentials(
        access_key_id="AKIAIOSFODNN7EXAMPLE",
        secret_access_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        session_token="AQoDYXdzEJr...",
        expiration=datetime.now(UTC) + timedelta(hours=1),
    )


@pytest.fixture
def expired_credentials() -> AssumedRoleCredentials:
    """Create expired STS credentials."""
    return AssumedRoleCredentials(
        access_key_id="AKIAIOSFODNN7EXAMPLE",
        secret_access_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        session_token="AQoDYXdzEJr...",
        expiration=datetime.now(UTC) - timedelta(hours=1),
    )


@pytest.fixture
def mock_sts_client():
    """Create a mock STS boto3 client."""
    mock_client = MagicMock()
    mock_client.assume_role.return_value = {
        "Credentials": {
            "AccessKeyId": "AKIAIOSFODNN7EXAMPLE",
            "SecretAccessKey": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
            "SessionToken": "AQoDYXdzEJr...",
            "Expiration": datetime.now(UTC) + timedelta(hours=1),
        },
        "AssumedRoleUser": {
            "AssumedRoleId": "AROA3XFRBF535EXAMPLE:TestSession",
            "Arn": "arn:aws:sts::111111111111:assumed-role/BedrockAccess/TestSession",
        },
    }
    return mock_client


@pytest.fixture
def sts_client_wrapper(pool_settings: PoolSettings, mock_sts_client) -> STSClient:
    """Create an STSClient with mocked boto3 client."""
    return STSClient(settings=pool_settings, sts_client=mock_sts_client)


@pytest.fixture
def health_tracker(pool_settings: PoolSettings) -> HealthTracker:
    """Create a health tracker for testing."""
    return HealthTracker(settings=pool_settings)


@pytest.fixture
def round_robin_selector(sample_accounts: list[BedrockAccount]) -> RoundRobinSelector:
    """Create a round-robin selector with sample accounts."""
    return RoundRobinSelector(accounts=sample_accounts)


@pytest.fixture
def mock_bedrock_client():
    """Create a mock Bedrock runtime client."""
    mock_client = MagicMock()
    mock_client.invoke_model.return_value = {
        "body": MagicMock(read=lambda: b'{"completion": "test"}'),
        "contentType": "application/json",
    }
    return mock_client


@pytest.fixture
def pool_service_with_mocks(
    pool_config: PoolConfig,
    sts_client_wrapper: STSClient,
    health_tracker: HealthTracker,
) -> PoolService:
    """Create a PoolService with mocked dependencies."""
    return PoolService(
        config=pool_config,
        sts_client=sts_client_wrapper,
        health_tracker=health_tracker,
    )


def make_healthy_account(account_id: str = "111111111111", region: str = "us-east-1") -> BedrockAccount:
    """Helper to create a healthy account."""
    return BedrockAccount(
        account_id=account_id,
        role_arn=f"arn:aws:iam::{account_id}:role/BedrockAccess",
        region=region,
        health_status=HealthStatus.HEALTHY,
    )


def make_unhealthy_account(
    account_id: str = "111111111111",
    region: str = "us-east-1",
    cooldown_seconds: int = 60,
) -> BedrockAccount:
    """Helper to create an unhealthy account in cooldown."""
    return BedrockAccount(
        account_id=account_id,
        role_arn=f"arn:aws:iam::{account_id}:role/BedrockAccess",
        region=region,
        health_status=HealthStatus.COOLDOWN,
        cooldown_until=datetime.now(UTC) + timedelta(seconds=cooldown_seconds),
    )


def make_expired_cooldown_account(account_id: str = "111111111111", region: str = "us-east-1") -> BedrockAccount:
    """Helper to create an account with expired cooldown."""
    return BedrockAccount(
        account_id=account_id,
        role_arn=f"arn:aws:iam::{account_id}:role/BedrockAccess",
        region=region,
        health_status=HealthStatus.COOLDOWN,
        cooldown_until=datetime.now(UTC) - timedelta(seconds=60),
    )
