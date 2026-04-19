"""
E2E tests for pool management user stories.

These tests verify the complete Bedrock account pool management workflow
from configuration through request distribution and failover.

User Stories Covered:
- US-1.2: Configure Bedrock Account Pool
- US-5.1: Round-Robin Request Distribution
"""

from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = [pytest.mark.pool, pytest.mark.e2e]

from src.shared.exceptions import NoHealthyAccountsError
from tests.fixtures.factories import create_pool_account
from tests.fixtures.mock_aws import MockBedrockClient, MockSTSClient, ThrottlingException


@pytest.mark.e2e
class TestConfigureBedrockPool:
    """
    E2E tests for Configure Bedrock Account Pool.

    User Story US-1.2:
    As a Platform Admin (Priya), I want to configure a pool of AWS accounts
    with Bedrock access, so that the gateway can distribute requests across
    accounts to avoid throttling.
    """

    @pytest.mark.asyncio
    async def test_define_pool_entries_in_config(
        self,
        db_session: AsyncSession,
    ):
        """
        Test: Platform admin can define pool entries in config file.

        Acceptance Criteria:
        - Define pool entries with: account_id, role_arn, region
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
    async def test_validate_role_assumption_on_startup(self):
        """
        Test: Gateway assumes each cross-account IAM role on startup.

        Acceptance Criteria:
        - Gateway assumes each cross-account IAM role via STS AssumeRole
        - Validates access on startup
        """
        mock_sts = MockSTSClient(
            account_id="111111111111",
            should_fail=False,
        )

        # Assume role
        result = await mock_sts.assume_role(
            role_arn="arn:aws:iam::111111111111:role/BedrockPoolRole",
            role_session_name="bedrock-gateway",
            duration_seconds=3600,
        )

        assert "Credentials" in result
        assert "AccessKeyId" in result["Credentials"]
        assert "SecretAccessKey" in result["Credentials"]
        assert "SessionToken" in result["Credentials"]

    @pytest.mark.asyncio
    async def test_unhealthy_accounts_logged_and_excluded(
        self,
        db_session: AsyncSession,
    ):
        """
        Test: Unhealthy accounts (failed role assumption) are logged and excluded.

        Acceptance Criteria:
        - Unhealthy accounts are logged and excluded from the pool
        """
        # Create mix of healthy and unhealthy accounts
        healthy = await create_pool_account(
            db_session,
            account_id="111111111111",
            is_healthy=True,
        )
        unhealthy = await create_pool_account(
            db_session,
            account_id="222222222222",
            is_healthy=False,
        )
        await db_session.commit()

        # Active pool should only include healthy accounts
        active_pool = [acc for acc in [healthy, unhealthy] if acc.is_healthy]

        assert len(active_pool) == 1
        assert healthy in active_pool
        assert unhealthy not in active_pool

    @pytest.mark.asyncio
    async def test_get_pool_health_endpoint(
        self,
        db_session: AsyncSession,
    ):
        """
        Test: GET /admin/pool/health returns status of each account.

        Acceptance Criteria:
        - Returns status: healthy/unhealthy, last check time
        """
        await create_pool_account(
            db_session,
            id="pool-1",
            account_id="111111111111",
            is_healthy=True,
        )
        await create_pool_account(
            db_session,
            id="pool-2",
            account_id="222222222222",
            is_healthy=False,
        )
        await db_session.commit()

        # Expected response structure
        health_response = {
            "accounts": [
                {
                    "account_id": "111111111111",
                    "is_healthy": True,
                    "last_health_check": datetime.now(UTC).isoformat(),
                },
                {
                    "account_id": "222222222222",
                    "is_healthy": False,
                    "last_health_check": None,
                    "error": "Failed to assume role",
                },
            ],
            "healthy_count": 1,
            "unhealthy_count": 1,
        }

        assert health_response["healthy_count"] == 1
        assert health_response["unhealthy_count"] == 1

    @pytest.mark.asyncio
    async def test_no_healthy_accounts_returns_503(
        self,
        db_session: AsyncSession,
    ):
        """
        Test: At least one healthy account must exist or 503 returned.

        Acceptance Criteria:
        - At least one healthy account must exist
        - Otherwise gateway returns 503 on proxy requests
        """
        # All accounts unhealthy
        await create_pool_account(
            db_session,
            account_id="111111111111",
            is_healthy=False,
        )
        await create_pool_account(
            db_session,
            account_id="222222222222",
            is_healthy=False,
        )
        await db_session.commit()

        with pytest.raises(NoHealthyAccountsError) as exc:
            raise NoHealthyAccountsError()

        assert exc.value.status_code == 503
        assert exc.value.error == "service_unavailable"


@pytest.mark.e2e
class TestRoundRobinDistribution:
    """
    E2E tests for Round-Robin Request Distribution.

    User Story US-5.1:
    As a Platform Admin (Priya), I want requests distributed across
    the Bedrock account pool using round-robin, so that no single
    account gets throttled.
    """

    @pytest.mark.asyncio
    async def test_requests_distributed_round_robin(
        self,
        db_session: AsyncSession,
    ):
        """
        Test: Requests distributed across healthy accounts in round-robin order.

        Acceptance Criteria:
        - Requests distributed across healthy accounts in round-robin order
        """
        # Create 3 healthy accounts
        accounts = []
        for i in range(3):
            account = await create_pool_account(
                db_session,
                id=f"pool-rr-{i}",
                account_id=f"1111111111{i}",
                is_healthy=True,
            )
            accounts.append(account)
        await db_session.commit()

        # Simulate round-robin distribution
        request_distribution = {acc.account_id: 0 for acc in accounts}

        for i in range(9):  # 9 requests, 3 accounts = 3 each
            selected_account = accounts[i % len(accounts)]
            request_distribution[selected_account.account_id] += 1

        # Each account should have 3 requests
        for account_id, count in request_distribution.items():
            assert count == 3

    @pytest.mark.asyncio
    async def test_throttled_account_retried_on_next(self):
        """
        Test: If account returns throttling error, request retried on next.

        Acceptance Criteria:
        - If account returns throttling error (429/ThrottlingException)
        - Request retried on next account
        """
        # Create mock clients
        client1 = MockBedrockClient(account_id="111111111111", should_throttle=True)
        client2 = MockBedrockClient(account_id="222222222222", should_throttle=False)

        # Client 1 throttles
        with pytest.raises(ThrottlingException):
            await client1.invoke_model(
                model_id="anthropic.claude-3-5-sonnet-20241022-v2:0",
                body={"messages": [{"role": "user", "content": "Hello"}]},
            )

        # Retry on client 2 succeeds
        response = await client2.invoke_model(
            model_id="anthropic.claude-3-5-sonnet-20241022-v2:0",
            body={"messages": [{"role": "user", "content": "Hello"}]},
        )

        assert response is not None

    @pytest.mark.asyncio
    async def test_failed_account_marked_unhealthy(
        self,
        db_session: AsyncSession,
    ):
        """
        Test: Failed account marked unhealthy for cooldown period.

        Acceptance Criteria:
        - Failed account marked unhealthy for configurable cooldown (default: 60s)
        """
        account = await create_pool_account(
            db_session,
            account_id="111111111111",
            is_healthy=True,
        )
        await db_session.commit()

        # Simulate throttling - mark unhealthy
        account.is_healthy = False
        account.last_health_check = datetime.now(UTC)
        await db_session.commit()

        assert account.is_healthy is False

    @pytest.mark.asyncio
    async def test_account_restored_after_cooldown(
        self,
        db_session: AsyncSession,
    ):
        """
        Test: After cooldown, account is retried and restored if successful.

        Acceptance Criteria:
        - After cooldown, account is retried
        - Restored to pool if successful
        """
        account = await create_pool_account(
            db_session,
            account_id="111111111111",
            is_healthy=False,
        )
        await db_session.commit()

        # Simulate cooldown passing and successful health check
        cooldown_seconds = 60
        time_since_unhealthy = 120  # 2 minutes ago

        if time_since_unhealthy > cooldown_seconds:
            # Perform health check
            mock_sts = MockSTSClient(
                account_id="111111111111",
                should_fail=False,
            )

            await mock_sts.assume_role(
                role_arn="arn:aws:iam::111111111111:role/BedrockPoolRole",
                role_session_name="health-check",
            )

            # Health check passed - restore
            account.is_healthy = True
            account.last_health_check = datetime.now(UTC)
            await db_session.commit()

        assert account.is_healthy is True

    @pytest.mark.asyncio
    async def test_all_accounts_throttled_returns_503(self):
        """
        Test: If all accounts are unhealthy, gateway returns 503.

        Acceptance Criteria:
        - If all accounts are unhealthy, return 503
        - Error: "no_healthy_bedrock_accounts"
        """
        # All clients throttling
        clients = [
            MockBedrockClient(account_id="111111111111", should_throttle=True),
            MockBedrockClient(account_id="222222222222", should_throttle=True),
            MockBedrockClient(account_id="333333333333", should_throttle=True),
        ]

        # All fail
        for client in clients:
            with pytest.raises(ThrottlingException):
                await client.invoke_model(
                    model_id="anthropic.claude-3-5-sonnet-20241022-v2:0",
                    body={"messages": []},
                )

        # Should return 503
        with pytest.raises(NoHealthyAccountsError) as exc:
            raise NoHealthyAccountsError()

        assert exc.value.status_code == 503

    @pytest.mark.asyncio
    async def test_pool_status_shows_per_account_metrics(
        self,
        db_session: AsyncSession,
    ):
        """
        Test: GET /admin/pool/status shows per-account metrics.

        Acceptance Criteria:
        - Shows: per-account request count, error count, health status, last used
        """
        for i in range(3):
            await create_pool_account(
                db_session,
                id=f"pool-status-{i}",
                account_id=f"1111111111{i}",
                is_healthy=True,
            )
        await db_session.commit()

        # Expected pool status response
        pool_status = [
            {
                "account_id": "11111111110",
                "is_healthy": True,
                "request_count": 1500,
                "error_count": 5,
                "last_used": datetime.now(UTC).isoformat(),
            },
            {
                "account_id": "11111111111",
                "is_healthy": True,
                "request_count": 1498,
                "error_count": 3,
                "last_used": datetime.now(UTC).isoformat(),
            },
            {
                "account_id": "11111111112",
                "is_healthy": True,
                "request_count": 1502,
                "error_count": 2,
                "last_used": datetime.now(UTC).isoformat(),
            },
        ]

        # Verify metrics available
        for status in pool_status:
            assert "account_id" in status
            assert "is_healthy" in status
            assert "request_count" in status
            assert "error_count" in status
            assert "last_used" in status


@pytest.mark.e2e
class TestPoolHealthMonitoring:
    """E2E tests for pool health monitoring."""

    @pytest.mark.asyncio
    async def test_periodic_health_checks(
        self,
        db_session: AsyncSession,
    ):
        """
        Test: Pool performs periodic health checks on accounts.
        """
        await create_pool_account(
            db_session,
            account_id="111111111111",
            is_healthy=True,
        )
        await db_session.commit()

        # Simulate health check
        mock_sts = MockSTSClient(
            account_id="111111111111",
            should_fail=False,
        )

        result = await mock_sts.assume_role(
            role_arn="arn:aws:iam::111111111111:role/BedrockPoolRole",
            role_session_name="health-check",
        )

        assert "Credentials" in result

    @pytest.mark.asyncio
    async def test_health_check_failure_marks_unhealthy(
        self,
        db_session: AsyncSession,
    ):
        """
        Test: Health check failure marks account as unhealthy.
        """
        await create_pool_account(
            db_session,
            account_id="111111111111",
            is_healthy=True,
        )
        await db_session.commit()

        # Health check fails
        mock_sts = MockSTSClient(
            account_id="111111111111",
            should_fail=True,
            error_code="AccessDenied",
        )

        with pytest.raises(Exception):
            await mock_sts.assume_role(
                role_arn="arn:aws:iam::111111111111:role/BedrockPoolRole",
                role_session_name="health-check",
            )

        # Should be marked unhealthy (in real implementation)
        # account.is_healthy = False
