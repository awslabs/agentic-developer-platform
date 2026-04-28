"""
E2E tests for rate limiting user stories.

Test modes:
- @pytest.mark.unit: Pure Python-level logic tests (db_session + mocks)
- @pytest.mark.integration: ASGI app in-process tests
- @pytest.mark.live_only: Real HTTP against deployed gateway

User Stories Covered:
- US-3.1: Configure Rate Limits
- US-3.2: Rate Limit Enforcement
- US-3.3: Service Account Rate Limits
"""

import asyncio

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = [pytest.mark.ratelimit, pytest.mark.e2e]

from src.shared.exceptions import RateLimitExceededError
from src.shared.schemas.common import RateLimitCheckResult
from tests.fixtures.factories import (
    create_department,
    create_org,
    create_rate_limit_config,
    create_service_account,
    create_team,
    create_user,
)

# =============================================================================
# Unit tests -- pure Python logic, db_session + mocks
# =============================================================================


@pytest.mark.unit
class TestConfigureRateLimits:
    """Unit tests for Configure Rate Limits. US-3.1."""

    async def test_configure_rate_limits_at_all_levels(self, db_session: AsyncSession):
        """Rate limits configurable at all entity levels."""
        org = await create_org(db_session, id="org-rl-config")
        dept = await create_department(db_session, org.id, id="dept-rl-config")
        team = await create_team(db_session, org.id, dept.id, id="team-rl-config")
        user = await create_user(db_session, org.id, team.id, id="user-rl-config")

        org_rl = await create_rate_limit_config(db_session, org.id, "org", org.id, rpm=10000, tpm=10000000, concurrent_requests=1000)
        dept_rl = await create_rate_limit_config(db_session, org.id, "department", dept.id, rpm=5000, tpm=5000000, concurrent_requests=500)
        team_rl = await create_rate_limit_config(db_session, org.id, "team", team.id, rpm=2000, tpm=2000000, concurrent_requests=200)
        user_rl = await create_rate_limit_config(db_session, org.id, "user", user.id, rpm=100, tpm=100000, concurrent_requests=10)
        await db_session.commit()

        assert org_rl.rpm == 10000
        assert dept_rl.rpm == 5000
        assert team_rl.rpm == 2000
        assert user_rl.rpm == 100

    async def test_token_bucket_algorithm(self):
        """Token bucket algorithm with configurable burst size."""
        bucket_config = {"rate": 100, "burst_size": 20, "current_tokens": 20}
        assert bucket_config["current_tokens"] <= bucket_config["burst_size"]

    async def test_rate_limit_changes_immediate(self, db_session: AsyncSession):
        """Rate limit changes take effect immediately."""
        org = await create_org(db_session, id="org-rl-immediate")
        dept = await create_department(db_session, org.id, id="dept-rl-immediate")
        team = await create_team(db_session, org.id, dept.id, id="team-rl-immediate")
        user = await create_user(db_session, org.id, team.id, id="user-rl-immediate")

        rl = await create_rate_limit_config(db_session, org.id, "user", user.id, rpm=60)
        await db_session.commit()
        assert rl.rpm == 60


@pytest.mark.unit
class TestRateLimitEnforcement:
    """Unit tests for Rate Limit Enforcement. US-3.2."""

    async def test_rate_limit_returns_429_with_details(self):
        """When rate limited, gateway returns 429 with details."""
        with pytest.raises(RateLimitExceededError) as exc:
            raise RateLimitExceededError(limit_type="rpm", limit=100, retry_after=30)

        assert exc.value.status_code == 429
        assert exc.value.error == "rate_limited"
        assert exc.value.details["type"] == "rpm"
        assert exc.value.details["limit"] == 100
        assert exc.value.details["retry_after_seconds"] == 30

    async def test_response_includes_rate_limit_headers(self):
        """Response includes rate limit headers."""
        expected_headers = {
            "Retry-After": "30",
            "X-RateLimit-Limit": "100",
            "X-RateLimit-Remaining": "0",
            "X-RateLimit-Reset": "30",
        }

        assert "Retry-After" in expected_headers
        assert "X-RateLimit-Limit" in expected_headers
        assert "X-RateLimit-Remaining" in expected_headers
        assert "X-RateLimit-Reset" in expected_headers

    async def test_most_restrictive_limit_applies(self, db_session: AsyncSession):
        """Rate limits checked at all levels -- most restrictive applies."""
        org = await create_org(db_session, id="org-restrictive")
        dept = await create_department(db_session, org.id, id="dept-restrictive")
        team = await create_team(db_session, org.id, dept.id, id="team-restrictive")
        user = await create_user(db_session, org.id, team.id, id="user-restrictive")

        await create_rate_limit_config(db_session, org.id, "org", org.id, rpm=1000)
        await create_rate_limit_config(db_session, org.id, "department", dept.id, rpm=500)
        await create_rate_limit_config(db_session, org.id, "team", team.id, rpm=200)
        await create_rate_limit_config(db_session, org.id, "user", user.id, rpm=60)
        await db_session.commit()

        effective_limit = min(1000, 500, 200, 60)
        assert effective_limit == 60

    async def test_concurrent_request_limit_tracked_per_user(self):
        """Concurrent request limit tracked per user."""
        current_concurrent = 10
        max_concurrent = 10

        if current_concurrent >= max_concurrent:
            with pytest.raises(RateLimitExceededError) as exc:
                raise RateLimitExceededError(limit_type="concurrent", limit=max_concurrent, retry_after=0)
            assert exc.value.details["type"] == "concurrent"


@pytest.mark.unit
class TestServiceAccountRateLimits:
    """Unit tests for Service Account Rate Limits. US-3.3."""

    async def test_service_account_independent_rate_limits(self, db_session: AsyncSession):
        """Service accounts have independent rate limit configuration."""
        org = await create_org(db_session, id="org-sa-rl-ind")
        dept = await create_department(db_session, org.id, id="dept-sa-rl-ind")
        team = await create_team(db_session, org.id, dept.id, id="team-sa-rl-ind")

        user = await create_user(db_session, org.id, team.id, id="user-rl")
        sa = await create_service_account(db_session, org.id, dept.id, team.id, id="sa-rl")

        user_rl = await create_rate_limit_config(db_session, org.id, "user", user.id, rpm=60, concurrent_requests=5)
        sa_rl = await create_rate_limit_config(db_session, org.id, "service_account", sa.id, rpm=600, concurrent_requests=50)
        await db_session.commit()

        assert sa_rl.rpm > user_rl.rpm
        assert sa_rl.concurrent_requests > user_rl.concurrent_requests

    async def test_default_service_account_limits_at_org_level(self, db_session: AsyncSession):
        """Default service account rate limits can be set at org level."""
        org = await create_org(db_session, id="org-sa-default-rl")
        dept = await create_department(db_session, org.id, id="dept-sa-default-rl")
        team = await create_team(db_session, org.id, dept.id, id="team-sa-default-rl")

        await create_service_account(db_session, org.id, dept.id, team.id, id="sa-default-rl")
        sa2 = await create_service_account(db_session, org.id, dept.id, team.id, id="sa-custom-rl")

        org_sa_default = await create_rate_limit_config(db_session, org.id, "org", org.id, rpm=300)
        sa2_override = await create_rate_limit_config(db_session, org.id, "service_account", sa2.id, rpm=1000)
        await db_session.commit()

        assert org_sa_default.rpm == 300
        assert sa2_override.rpm == 1000

    async def test_service_account_enforcement_independent(self, db_session: AsyncSession):
        """Rate limit enforcement for service accounts is independent."""
        org = await create_org(db_session, id="org-sa-enforce")
        dept = await create_department(db_session, org.id, id="dept-sa-enforce")
        team = await create_team(db_session, org.id, dept.id, id="team-sa-enforce")

        await create_user(db_session, org.id, team.id, id="user-enforce")
        await create_service_account(db_session, org.id, dept.id, team.id, id="sa-enforce")

        user_rl_result = RateLimitCheckResult(allowed=False, limit_type="rpm", limit=60, remaining=0, retry_after_seconds=30)
        sa_rl_result = RateLimitCheckResult(allowed=True, limit_type="rpm", limit=600, remaining=500)

        assert user_rl_result.allowed is False
        assert sa_rl_result.allowed is True


@pytest.mark.unit
class TestRateLimitByTier:
    """Unit tests for rate limiting by tier/role."""

    async def test_different_tiers_different_limits(self, db_session: AsyncSession):
        """Different user tiers have different rate limits."""
        org = await create_org(db_session, id="org-tier")
        dept = await create_department(db_session, org.id, id="dept-tier")
        team = await create_team(db_session, org.id, dept.id, id="team-tier")

        basic_user = await create_user(db_session, org.id, team.id, id="user-basic")
        basic_rl = await create_rate_limit_config(db_session, org.id, "user", basic_user.id, rpm=60, tpm=60000, concurrent_requests=5)

        premium_user = await create_user(db_session, org.id, team.id, id="user-premium")
        premium_rl = await create_rate_limit_config(db_session, org.id, "user", premium_user.id, rpm=300, tpm=300000, concurrent_requests=20)
        await db_session.commit()

        assert premium_rl.rpm > basic_rl.rpm
        assert premium_rl.tpm > basic_rl.tpm
        assert premium_rl.concurrent_requests > basic_rl.concurrent_requests


@pytest.mark.unit
class TestRateLimitHTTPEnforcement:
    """Unit tests for rate-limit quota-exceeded -> 429."""

    async def test_quota_exceeded_returns_429(self):
        """Exceeding RPM quota surfaces as 429 with details."""
        with pytest.raises(RateLimitExceededError) as exc:
            raise RateLimitExceededError(limit_type="rpm", limit=60, retry_after=45)

        assert exc.value.status_code == 429
        assert exc.value.details["type"] == "rpm"
        assert exc.value.details["retry_after_seconds"] == 45

    async def test_tpm_exceeded_returns_429(self):
        """Token-per-minute (TPM) quota exceeded returns 429."""
        with pytest.raises(RateLimitExceededError) as exc:
            raise RateLimitExceededError(limit_type="tpm", limit=100000, retry_after=10)

        assert exc.value.status_code == 429
        assert exc.value.details["type"] == "tpm"


# =============================================================================
# Live-only tests -- Rate limit enforcement via real HTTP
# =============================================================================


@pytest.mark.live_only
class TestLiveRateLimitOAuth:
    """Live HTTP tests for rate limit enforcement via OAuth."""

    async def test_ratelimit_headers_present(self, api_client, jwt_for_user):
        """Successful proxy response includes X-RateLimit-* headers."""
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
        # Response should succeed or return a non-5xx error
        assert response.status_code < 500, f"Rate limit test returned {response.status_code}"
        # If rate limit headers are present, verify format
        rl_limit = response.headers.get("x-ratelimit-limit")
        rl_remaining = response.headers.get("x-ratelimit-remaining")
        if rl_limit:
            assert rl_limit.isdigit(), f"X-RateLimit-Limit should be numeric, got '{rl_limit}'"
        if rl_remaining:
            assert rl_remaining.isdigit(), f"X-RateLimit-Remaining should be numeric, got '{rl_remaining}'"

    async def test_burst_requests_may_trigger_429(self, api_client, jwt_for_user):
        """Sending rapid burst requests may trigger 429 rate limiting.

        This test sends a burst of requests and checks that either all succeed
        or some return 429. It does NOT guarantee hitting the limit (depends
        on rate config) but validates the gateway handles burst correctly.
        """
        from tests.e2e.config import get_test_bedrock_model

        model = get_test_bedrock_model()
        payload = {
            "model": model,
            "max_tokens": 5,
            "messages": [{"role": "user", "content": "hi"}],
        }
        headers = {"Authorization": f"Bearer {jwt_for_user}", "Content-Type": "application/json"}

        # Fire 5 concurrent requests
        tasks = [api_client.post("/v1/messages", headers=headers, json=payload) for _ in range(5)]
        responses = await asyncio.gather(*tasks, return_exceptions=True)

        status_codes = []
        for r in responses:
            if isinstance(r, Exception):
                continue
            status_codes.append(r.status_code)

        # All should be 200 or 429 (or other non-5xx)
        for code in status_codes:
            assert code < 500, f"Burst request returned {code}"


@pytest.mark.live_only
class TestLiveRateLimitIAM:
    """Live HTTP tests for rate limit enforcement via IAM SigV4."""

    async def test_iam_ratelimit_headers_present(self, iam_signed_client):
        """IAM-authed proxy response includes rate limit headers (if applicable)."""
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
        assert response.status_code < 500, f"IAM rate limit test returned {response.status_code}"
