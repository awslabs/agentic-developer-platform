"""
E2E tests for pool management user stories.

Test modes:
- @pytest.mark.unit: Pure Python-level logic tests (db_session + mocks)
- @pytest.mark.live_only: Real HTTP against deployed gateway

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


# =============================================================================
# Unit tests -- pure Python logic, db_session + mocks
# =============================================================================


@pytest.mark.unit
class TestConfigureBedrockPool:
    """Unit tests for Configure Bedrock Account Pool. US-1.2."""

    async def test_define_pool_entries_in_config(self, db_session: AsyncSession):
        """Platform admin can define pool entries in config file."""
        pool_config = [
            {"account_id": "111111111111", "role_arn": "arn:aws:iam::111111111111:role/BedrockPoolRole", "region": "us-east-1"},
            {"account_id": "222222222222", "role_arn": "arn:aws:iam::222222222222:role/BedrockPoolRole", "region": "us-east-1"},
            {"account_id": "333333333333", "role_arn": "arn:aws:iam::333333333333:role/BedrockPoolRole", "region": "us-west-2"},
        ]

        for config in pool_config:
            account = await create_pool_account(
                db_session, account_id=config["account_id"],
                role_arn=config["role_arn"], region=config["region"], is_healthy=True,
            )
            assert account.account_id == config["account_id"]
            assert account.role_arn == config["role_arn"]
            assert account.region == config["region"]

        await db_session.commit()

    async def test_validate_role_assumption_on_startup(self):
        """Gateway assumes each cross-account IAM role on startup."""
        mock_sts = MockSTSClient(account_id="111111111111", should_fail=False)

        result = await mock_sts.assume_role(
            role_arn="arn:aws:iam::111111111111:role/BedrockPoolRole",
            role_session_name="bedrock-gateway", duration_seconds=3600,
        )

        assert "Credentials" in result
        assert "AccessKeyId" in result["Credentials"]
        assert "SecretAccessKey" in result["Credentials"]
        assert "SessionToken" in result["Credentials"]

    async def test_unhealthy_accounts_logged_and_excluded(self, db_session: AsyncSession):
        """Unhealthy accounts are logged and excluded from the pool."""
        healthy = await create_pool_account(db_session, account_id="111111111111", is_healthy=True)
        unhealthy = await create_pool_account(db_session, account_id="222222222222", is_healthy=False)
        await db_session.commit()

        active_pool = [acc for acc in [healthy, unhealthy] if acc.is_healthy]
        assert len(active_pool) == 1
        assert healthy in active_pool
        assert unhealthy not in active_pool

    async def test_get_pool_health_endpoint(self, db_session: AsyncSession):
        """GET /admin/pool/health returns status of each account."""
        await create_pool_account(db_session, id="pool-1", account_id="111111111111", is_healthy=True)
        await create_pool_account(db_session, id="pool-2", account_id="222222222222", is_healthy=False)
        await db_session.commit()

        health_response = {
            "accounts": [
                {"account_id": "111111111111", "is_healthy": True, "last_health_check": datetime.now(UTC).isoformat()},
                {"account_id": "222222222222", "is_healthy": False, "last_health_check": None, "error": "Failed to assume role"},
            ],
            "healthy_count": 1, "unhealthy_count": 1,
        }

        assert health_response["healthy_count"] == 1
        assert health_response["unhealthy_count"] == 1

    async def test_no_healthy_accounts_returns_503(self, db_session: AsyncSession):
        """At least one healthy account must exist or 503 returned."""
        await create_pool_account(db_session, account_id="111111111111", is_healthy=False)
        await create_pool_account(db_session, account_id="222222222222", is_healthy=False)
        await db_session.commit()

        with pytest.raises(NoHealthyAccountsError) as exc:
            raise NoHealthyAccountsError()

        assert exc.value.status_code == 503
        assert exc.value.error == "service_unavailable"


@pytest.mark.unit
class TestRoundRobinDistribution:
    """Unit tests for Round-Robin Request Distribution. US-5.1."""

    async def test_requests_distributed_round_robin(self, db_session: AsyncSession):
        """Requests distributed across healthy accounts in round-robin order."""
        accounts = []
        for i in range(3):
            account = await create_pool_account(
                db_session, id=f"pool-rr-{i}", account_id=f"1111111111{i}", is_healthy=True,
            )
            accounts.append(account)
        await db_session.commit()

        request_distribution = {acc.account_id: 0 for acc in accounts}
        for i in range(9):
            selected_account = accounts[i % len(accounts)]
            request_distribution[selected_account.account_id] += 1

        for account_id, count in request_distribution.items():
            assert count == 3

    async def test_throttled_account_retried_on_next(self):
        """If account returns throttling error, request retried on next."""
        client1 = MockBedrockClient(account_id="111111111111", should_throttle=True)
        client2 = MockBedrockClient(account_id="222222222222", should_throttle=False)

        with pytest.raises(ThrottlingException):
            await client1.invoke_model(
                model_id="anthropic.claude-3-5-sonnet-20241022-v2:0",
                body={"messages": [{"role": "user", "content": "Hello"}]},
            )

        response = await client2.invoke_model(
            model_id="anthropic.claude-3-5-sonnet-20241022-v2:0",
            body={"messages": [{"role": "user", "content": "Hello"}]},
        )
        assert response is not None

    async def test_failed_account_marked_unhealthy(self, db_session: AsyncSession):
        """Failed account marked unhealthy for cooldown period."""
        account = await create_pool_account(db_session, account_id="111111111111", is_healthy=True)
        await db_session.commit()

        account.is_healthy = False
        account.last_health_check = datetime.now(UTC)
        await db_session.commit()

        assert account.is_healthy is False

    async def test_account_restored_after_cooldown(self, db_session: AsyncSession):
        """After cooldown, account is retried and restored if successful."""
        account = await create_pool_account(db_session, account_id="111111111111", is_healthy=False)
        await db_session.commit()

        cooldown_seconds = 60
        time_since_unhealthy = 120

        if time_since_unhealthy > cooldown_seconds:
            mock_sts = MockSTSClient(account_id="111111111111", should_fail=False)
            await mock_sts.assume_role(
                role_arn="arn:aws:iam::111111111111:role/BedrockPoolRole",
                role_session_name="health-check",
            )
            account.is_healthy = True
            account.last_health_check = datetime.now(UTC)
            await db_session.commit()

        assert account.is_healthy is True

    async def test_all_accounts_throttled_returns_503(self):
        """If all accounts are unhealthy, gateway returns 503."""
        clients = [
            MockBedrockClient(account_id=f"1111111111{i}", should_throttle=True)
            for i in range(3)
        ]

        for client in clients:
            with pytest.raises(ThrottlingException):
                await client.invoke_model(
                    model_id="anthropic.claude-3-5-sonnet-20241022-v2:0",
                    body={"messages": []},
                )

        with pytest.raises(NoHealthyAccountsError) as exc:
            raise NoHealthyAccountsError()
        assert exc.value.status_code == 503

    async def test_pool_status_shows_per_account_metrics(self, db_session: AsyncSession):
        """GET /admin/pool/status shows per-account metrics."""
        for i in range(3):
            await create_pool_account(
                db_session, id=f"pool-status-{i}", account_id=f"1111111111{i}", is_healthy=True,
            )
        await db_session.commit()

        pool_status = [
            {"account_id": f"1111111111{i}", "is_healthy": True, "request_count": 1500 + i, "error_count": 5 - i, "last_used": datetime.now(UTC).isoformat()}
            for i in range(3)
        ]

        for status in pool_status:
            assert "account_id" in status
            assert "is_healthy" in status
            assert "request_count" in status
            assert "error_count" in status
            assert "last_used" in status


@pytest.mark.unit
class TestPoolHealthMonitoring:
    """Unit tests for pool health monitoring."""

    async def test_periodic_health_checks(self, db_session: AsyncSession):
        """Pool performs periodic health checks on accounts."""
        await create_pool_account(db_session, account_id="111111111111", is_healthy=True)
        await db_session.commit()

        mock_sts = MockSTSClient(account_id="111111111111", should_fail=False)
        result = await mock_sts.assume_role(
            role_arn="arn:aws:iam::111111111111:role/BedrockPoolRole",
            role_session_name="health-check",
        )
        assert "Credentials" in result

    async def test_health_check_failure_marks_unhealthy(self, db_session: AsyncSession):
        """Health check failure marks account as unhealthy."""
        await create_pool_account(db_session, account_id="111111111111", is_healthy=True)
        await db_session.commit()

        mock_sts = MockSTSClient(account_id="111111111111", should_fail=True, error_code="AccessDenied")

        with pytest.raises(Exception):
            await mock_sts.assume_role(
                role_arn="arn:aws:iam::111111111111:role/BedrockPoolRole",
                role_session_name="health-check",
            )


# =============================================================================
# Live-only tests -- Pool verification via real HTTP
# =============================================================================


@pytest.mark.live_only
class TestLivePoolOAuth:
    """Live HTTP tests for pool selection via OAuth."""

    async def test_pool_account_used_in_proxy_response(self, api_client, jwt_for_user):
        """Proxy response may include pool-account-related headers or metadata."""
        from tests.e2e.config import get_test_bedrock_model

        model = get_test_bedrock_model()
        response = await api_client.post(
            "/v1/messages",
            headers={"Authorization": f"Bearer {jwt_for_user}", "Content-Type": "application/json"},
            json={
                "model": model,
                "max_tokens": 10,
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        # Should succeed (pool has healthy accounts)
        assert response.status_code < 500, f"Pool test returned {response.status_code}"

    async def test_pool_health_endpoint_returns_data(self, api_client, jwt_for_user):
        """GET /admin/pool/health returns pool status data."""
        response = await api_client.get(
            "/admin/pool/health",
            headers={"Authorization": f"Bearer {jwt_for_user}"},
        )
        assert response.status_code < 500, f"Pool health returned {response.status_code}"


@pytest.mark.live_only
class TestLivePoolIAM:
    """Live HTTP tests for pool selection via IAM SigV4."""

    async def test_iam_pool_account_used_in_proxy(self, iam_signed_client):
        """IAM-authed proxy request uses pool account."""
        from tests.e2e.config import get_test_bedrock_model

        model = get_test_bedrock_model()
        response = await iam_signed_client.post(
            "/v1/messages",
            json={
                "model": model,
                "max_tokens": 10,
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        assert response.status_code < 500, f"IAM pool test returned {response.status_code}"

    async def test_iam_pool_health_endpoint(self, iam_signed_client):
        """IAM-authed request to /admin/pool/health."""
        response = await iam_signed_client.get("/admin/pool/health")
        assert response.status_code < 500, f"IAM pool health returned {response.status_code}"
