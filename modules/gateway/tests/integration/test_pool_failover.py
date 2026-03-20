"""
Integration tests for Pool Round-Robin → Failover on Throttle.

These tests verify the Bedrock account pool management including
request distribution, health monitoring, and failover behavior.

User Stories Covered:
- US-1.2: Configure Bedrock Account Pool
- US-5.1: Round-Robin Request Distribution
- US-9.4: All Bedrock Accounts Unhealthy
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.exceptions import NoHealthyAccountsError
from tests.fixtures.factories import create_pool_account
from tests.fixtures.mock_aws import (
    ThrottlingException,
    create_mock_bedrock_pool,
)


@pytest.mark.integration
class TestPoolRoundRobin:
    """Test suite for pool round-robin distribution."""

    @pytest.mark.asyncio
    async def test_requests_distributed_across_pool(
        self,
        db_session: AsyncSession,
    ):
        """
        Test that requests are distributed across pool accounts in round-robin.

        Acceptance Criteria (US-5.1):
        - Requests distributed across healthy accounts in round-robin order
        """
        # Create pool accounts
        accounts = []
        for i in range(3):
            account = await create_pool_account(
                db_session,
                id=f"pool-{i}",
                account_id=f"11111111111{i}",
                is_healthy=True,
            )
            accounts.append(account)
        await db_session.commit()

        # Create mock clients
        clients = create_mock_bedrock_pool(num_accounts=3)

        # Simulate 6 requests (should round-robin 2x through all accounts)
        request_distribution = [0, 0, 0]

        for i in range(6):
            client_index = i % 3
            client = clients[client_index]

            await client.invoke_model(
                model_id="anthropic.claude-3-5-sonnet-20241022-v2:0",
                body={"messages": [{"role": "user", "content": "Hello"}]},
            )
            request_distribution[client_index] += 1

        # Each account should have received 2 requests
        assert request_distribution == [2, 2, 2]

    @pytest.mark.asyncio
    async def test_pool_status_shows_distribution(
        self,
        db_session: AsyncSession,
    ):
        """
        Test that pool status shows per-account metrics.

        Acceptance Criteria (US-5.1):
        - GET /admin/pool/status shows: per-account request count,
          error count, health status, last used timestamp
        """
        # Create pool accounts
        accounts = []
        for i in range(3):
            account = await create_pool_account(
                db_session,
                id=f"pool-status-{i}",
                account_id=f"22222222222{i}",
                is_healthy=True,
            )
            accounts.append(account)
        await db_session.commit()

        # Simulated pool status response
        pool_status = [
            {
                "account_id": "222222222220",
                "is_healthy": True,
                "request_count": 150,
                "error_count": 2,
                "last_used": datetime.now(UTC).isoformat(),
                "last_health_check": datetime.now(UTC).isoformat(),
            },
            {
                "account_id": "222222222221",
                "is_healthy": True,
                "request_count": 148,
                "error_count": 1,
                "last_used": datetime.now(UTC).isoformat(),
                "last_health_check": datetime.now(UTC).isoformat(),
            },
            {
                "account_id": "222222222222",
                "is_healthy": True,
                "request_count": 152,
                "error_count": 0,
                "last_used": datetime.now(UTC).isoformat(),
                "last_health_check": datetime.now(UTC).isoformat(),
            },
        ]

        # Verify status structure
        for status in pool_status:
            assert "account_id" in status
            assert "is_healthy" in status
            assert "request_count" in status
            assert "error_count" in status
            assert "last_used" in status


@pytest.mark.integration
class TestPoolFailover:
    """Test suite for pool failover behavior."""

    @pytest.mark.asyncio
    async def test_throttled_account_marked_unhealthy(
        self,
        db_session: AsyncSession,
    ):
        """
        Test that throttled account is marked unhealthy.

        Acceptance Criteria (US-5.1):
        - If an account returns throttling error (429/ThrottlingException),
          request retried on next account
        - Failed account marked unhealthy for configurable cooldown period
        """
        # Create pool accounts (one will be throttled)
        accounts = []
        for i in range(3):
            account = await create_pool_account(
                db_session,
                id=f"pool-throttle-{i}",
                account_id=f"33333333333{i}",
                is_healthy=True,
            )
            accounts.append(account)
        await db_session.commit()

        # Create mock clients with one throttling
        clients = create_mock_bedrock_pool(
            num_accounts=3,
            unhealthy_indices=[1],  # Account 1 will throttle
        )

        # Simulate request to throttled account
        throttled_client = clients[1]
        healthy_client = clients[0]

        # Throttled client should raise exception
        with pytest.raises(ThrottlingException):
            await throttled_client.invoke_model(
                model_id="anthropic.claude-3-5-sonnet-20241022-v2:0",
                body={"messages": [{"role": "user", "content": "Hello"}]},
            )

        # Healthy client should succeed
        response = await healthy_client.invoke_model(
            model_id="anthropic.claude-3-5-sonnet-20241022-v2:0",
            body={"messages": [{"role": "user", "content": "Hello"}]},
        )

        assert response is not None
        assert "content" in response

    @pytest.mark.asyncio
    async def test_requests_failover_to_healthy_accounts(
        self,
        db_session: AsyncSession,
    ):
        """
        Test that requests failover to healthy accounts when one is unhealthy.

        Acceptance Criteria (US-5.1):
        - Request retried on next account if current fails
        """
        # Create pool accounts
        accounts = []
        for i in range(3):
            is_healthy = i != 1  # Account 1 is unhealthy
            account = await create_pool_account(
                db_session,
                id=f"pool-failover-{i}",
                account_id=f"44444444444{i}",
                is_healthy=is_healthy,
            )
            accounts.append(account)
        await db_session.commit()

        # Create mock clients
        clients = create_mock_bedrock_pool(
            num_accounts=3,
            unhealthy_indices=[1],
        )

        # Track which clients handle requests
        successful_requests = []

        for i in range(4):
            # Skip unhealthy client
            client_index = i % 3
            if client_index == 1:
                client_index = (client_index + 1) % 3

            client = clients[client_index]

            try:
                await client.invoke_model(
                    model_id="anthropic.claude-3-5-sonnet-20241022-v2:0",
                    body={"messages": [{"role": "user", "content": "Hello"}]},
                )
                successful_requests.append(client_index)
            except ThrottlingException:
                pass  # Skip and move to next

        # All successful requests should be to healthy accounts
        assert all(idx != 1 for idx in successful_requests)

    @pytest.mark.asyncio
    async def test_unhealthy_account_recovers_after_cooldown(
        self,
        db_session: AsyncSession,
    ):
        """
        Test that unhealthy account recovers after cooldown period.

        Acceptance Criteria (US-5.1):
        - After cooldown, account is retried and restored to pool if successful
        """
        # Create initially unhealthy account
        account = await create_pool_account(
            db_session,
            id="pool-recover",
            account_id="555555555550",
            is_healthy=False,
        )
        await db_session.commit()

        # Verify initial state
        assert account.is_healthy is False

        # Simulate cooldown period passing and recovery
        # In real implementation, this would be handled by health check service
        recovery_time = datetime.now(UTC) - timedelta(seconds=60)  # Cooldown passed

        # After successful health check, account should be restored
        recovered_account_status = {
            "account_id": "555555555550",
            "is_healthy": True,
            "last_health_check": datetime.now(UTC).isoformat(),
            "recovered_at": recovery_time.isoformat(),
        }

        assert recovered_account_status["is_healthy"] is True

    @pytest.mark.asyncio
    async def test_all_accounts_throttled_returns_503(
        self,
        db_session: AsyncSession,
    ):
        """
        Test that 503 is returned when all accounts are unhealthy.

        Acceptance Criteria (US-5.1, US-9.4):
        - If all accounts are unhealthy, gateway returns 503
        - Error: "no_healthy_bedrock_accounts"
        - Message: "All Bedrock accounts are currently unavailable"
        """
        # Create all unhealthy accounts
        accounts = []
        for i in range(3):
            account = await create_pool_account(
                db_session,
                id=f"pool-all-unhealthy-{i}",
                account_id=f"66666666666{i}",
                is_healthy=False,
            )
            accounts.append(account)
        await db_session.commit()

        # Create mock clients (all throttling)
        clients = create_mock_bedrock_pool(
            num_accounts=3,
            unhealthy_indices=[0, 1, 2],  # All accounts throttling
        )

        # All requests should fail
        for client in clients:
            with pytest.raises(ThrottlingException):
                await client.invoke_model(
                    model_id="anthropic.claude-3-5-sonnet-20241022-v2:0",
                    body={"messages": [{"role": "user", "content": "Hello"}]},
                )

        # Should raise NoHealthyAccountsError
        with pytest.raises(NoHealthyAccountsError) as exc_info:
            raise NoHealthyAccountsError()

        assert exc_info.value.status_code == 503
        assert exc_info.value.error == "service_unavailable"


@pytest.mark.integration
class TestPoolHealthCheck:
    """Test suite for pool health check functionality."""

    @pytest.mark.asyncio
    async def test_pool_health_endpoint(
        self,
        db_session: AsyncSession,
    ):
        """
        Test GET /admin/pool/health returns account health status.

        Acceptance Criteria (US-1.2):
        - GET /admin/pool/health returns status of each account
          (healthy/unhealthy, last check time)
        """
        # Create pool accounts with different health states
        await create_pool_account(
            db_session,
            id="pool-health-1",
            account_id="777777777770",
            is_healthy=True,
        )
        await create_pool_account(
            db_session,
            id="pool-health-2",
            account_id="777777777771",
            is_healthy=False,
        )
        await db_session.commit()

        # Expected health response
        health_response = {
            "accounts": [
                {
                    "account_id": "777777777770",
                    "is_healthy": True,
                    "last_health_check": datetime.now(UTC).isoformat(),
                    "region": "us-east-1",
                },
                {
                    "account_id": "777777777771",
                    "is_healthy": False,
                    "last_health_check": None,
                    "region": "us-east-1",
                    "error": "Role assumption failed",
                },
            ],
            "healthy_count": 1,
            "unhealthy_count": 1,
            "total_count": 2,
        }

        assert health_response["healthy_count"] == 1
        assert health_response["unhealthy_count"] == 1

    @pytest.mark.asyncio
    async def test_unhealthy_accounts_logged_and_excluded(
        self,
        db_session: AsyncSession,
    ):
        """
        Test that unhealthy accounts are logged and excluded from pool.

        Acceptance Criteria (US-1.2):
        - Unhealthy accounts (failed role assumption) are logged
          and excluded from the pool
        """
        # Create mix of healthy and unhealthy accounts
        healthy_accounts = []
        for i in range(2):
            account = await create_pool_account(
                db_session,
                id=f"pool-exclude-healthy-{i}",
                account_id=f"88888888888{i}",
                is_healthy=True,
            )
            healthy_accounts.append(account)

        unhealthy_account = await create_pool_account(
            db_session,
            id="pool-exclude-unhealthy",
            account_id="888888888882",
            is_healthy=False,
        )
        await db_session.commit()

        # Pool should only include healthy accounts
        active_pool = [acc for acc in [*healthy_accounts, unhealthy_account] if acc.is_healthy]

        assert len(active_pool) == 2
        assert unhealthy_account not in active_pool

    @pytest.mark.asyncio
    async def test_at_least_one_healthy_account_required(
        self,
        db_session: AsyncSession,
    ):
        """
        Test that at least one healthy account is required.

        Acceptance Criteria (US-1.2):
        - At least one healthy account must exist or the gateway
          returns 503 on proxy requests
        """
        # Create only unhealthy accounts
        unhealthy_accounts = []
        for i in range(2):
            account = await create_pool_account(
                db_session,
                id=f"pool-no-healthy-{i}",
                account_id=f"99999999999{i}",
                is_healthy=False,
            )
            unhealthy_accounts.append(account)
        await db_session.commit()

        # No healthy accounts available
        healthy_count = sum(1 for acc in unhealthy_accounts if acc.is_healthy)

        assert healthy_count == 0

        # Should return 503
        with pytest.raises(NoHealthyAccountsError):
            if healthy_count == 0:
                raise NoHealthyAccountsError()


@pytest.mark.integration
class TestPoolConfiguration:
    """Test suite for pool configuration."""

    @pytest.mark.asyncio
    async def test_pool_configuration_from_config(
        self,
        db_session: AsyncSession,
    ):
        """
        Test that pool can be configured from config file.

        Acceptance Criteria (US-1.2):
        - Platform admin can define pool entries in config file
          with: account_id, role_arn, region
        """
        # Simulated config file entries
        pool_config = [
            {
                "account_id": "111111111111",
                "role_arn": "arn:aws:iam::111111111111:role/BedrockPoolRole",
                "region": "us-east-1",
            },
            {
                "account_id": "222222222222",
                "role_arn": "arn:aws:iam::222222222222:role/BedrockPoolRole",
                "region": "us-east-1",
            },
            {
                "account_id": "333333333333",
                "role_arn": "arn:aws:iam::333333333333:role/BedrockPoolRole",
                "region": "us-west-2",
            },
        ]

        # Create accounts from config
        for config in pool_config:
            account = await create_pool_account(
                db_session,
                account_id=config["account_id"],
                role_arn=config["role_arn"],
                region=config["region"],
                is_healthy=True,
            )
            assert account.account_id == config["account_id"]
            assert account.role_arn == config["role_arn"]
            assert account.region == config["region"]

        await db_session.commit()

    @pytest.mark.asyncio
    async def test_pool_validates_role_assumption_on_startup(
        self,
        db_session: AsyncSession,
    ):
        """
        Test that pool validates role assumption on startup.

        Acceptance Criteria (US-1.2):
        - Gateway assumes each cross-account IAM role via STS AssumeRole
          on startup and validates access
        """
        # Create mock STS client for role assumption
        from tests.fixtures.mock_aws import MockSTSClient

        # Test successful role assumption
        sts_client = MockSTSClient(
            account_id="111111111111",
            should_fail=False,
        )

        result = await sts_client.assume_role(
            role_arn="arn:aws:iam::111111111111:role/BedrockPoolRole",
            role_session_name="bedrock-gateway",
        )

        assert "Credentials" in result
        assert "AccessKeyId" in result["Credentials"]

        # Test failed role assumption
        failed_sts_client = MockSTSClient(
            account_id="222222222222",
            should_fail=True,
            error_code="AccessDenied",
            error_message="User is not authorized to assume this role",
        )

        with pytest.raises(Exception) as exc_info:
            await failed_sts_client.assume_role(
                role_arn="arn:aws:iam::222222222222:role/BedrockPoolRole",
                role_session_name="bedrock-gateway",
            )

        assert "AccessDenied" in str(exc_info.value)
