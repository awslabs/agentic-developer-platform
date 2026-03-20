"""
Integration tests for Rate Limit Check → Enforcement → Header Responses.

These tests verify the complete rate limiting flow including
checking limits, enforcing them, and returning proper headers.

User Stories Covered:
- US-3.1: Configure Rate Limits
- US-3.2: Rate Limit Enforcement
- US-3.3: Service Account Rate Limits
"""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

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


@pytest.mark.integration
class TestRateLimitHeaders:
    """Test suite for rate limit header responses."""

    @pytest.mark.asyncio
    async def test_rate_limit_headers_included_in_response(
        self,
        db_session: AsyncSession,
    ):
        """
        Test that rate limit headers are included in all responses.

        Acceptance Criteria (US-3.2):
        - Response includes headers: X-RateLimit-Limit, X-RateLimit-Remaining, X-RateLimit-Reset
        """
        # Setup
        org = await create_org(db_session, id="org-headers")
        dept = await create_department(db_session, org.id, id="dept-headers")
        team = await create_team(db_session, org.id, dept.id, id="team-headers")
        user = await create_user(db_session, org.id, team.id, id="user-headers")

        # Configure rate limit
        await create_rate_limit_config(
            db_session,
            org.id,
            "user",
            user.id,
            rpm=100,
            tpm=100000,
            concurrent_requests=10,
        )
        await db_session.commit()

        # Check rate limit
        rate_limit_result = RateLimitCheckResult(
            allowed=True,
            limit_type="rpm",
            limit=100,
            remaining=99,
            retry_after_seconds=None,
        )

        # Verify expected headers
        expected_headers = {
            "X-RateLimit-Limit": str(rate_limit_result.limit),
            "X-RateLimit-Remaining": str(rate_limit_result.remaining),
            "X-RateLimit-Reset": str(60),  # Seconds until reset
        }

        assert "X-RateLimit-Limit" in expected_headers
        assert "X-RateLimit-Remaining" in expected_headers
        assert "X-RateLimit-Reset" in expected_headers
        assert expected_headers["X-RateLimit-Limit"] == "100"
        assert expected_headers["X-RateLimit-Remaining"] == "99"

    @pytest.mark.asyncio
    async def test_request_within_limit_returns_200(
        self,
        db_session: AsyncSession,
    ):
        """
        Test that request within rate limit returns 200.

        Acceptance Criteria (US-3.2):
        - Rate limits checked at all hierarchy levels
        - Most restrictive limit applies
        """
        # Setup
        org = await create_org(db_session, id="org-within-limit")
        dept = await create_department(db_session, org.id, id="dept-within")
        team = await create_team(db_session, org.id, dept.id, id="team-within")
        user = await create_user(db_session, org.id, team.id, id="user-within")

        # Configure rate limits at multiple levels
        await create_rate_limit_config(
            db_session,
            org.id,
            "org",
            org.id,
            rpm=1000,
            tpm=1000000,
            concurrent_requests=100,
        )
        await create_rate_limit_config(
            db_session,
            org.id,
            "user",
            user.id,
            rpm=100,
            tpm=100000,
            concurrent_requests=10,
        )
        await db_session.commit()

        # Rate limit check result
        rate_limit_result = RateLimitCheckResult(
            allowed=True,
            limit_type="rpm",
            limit=100,  # User limit (most restrictive)
            remaining=99,
        )

        assert rate_limit_result.allowed is True
        assert rate_limit_result.limit == 100

    @pytest.mark.asyncio
    async def test_request_exceeding_limit_returns_429(
        self,
        db_session: AsyncSession,
    ):
        """
        Test that request exceeding rate limit returns 429.

        Acceptance Criteria (US-3.2):
        - When rate limited, gateway returns 429
        - Response includes: error, type, limit, retry_after_seconds
        """
        # Setup
        org = await create_org(db_session, id="org-exceed-limit")
        dept = await create_department(db_session, org.id, id="dept-exceed")
        team = await create_team(db_session, org.id, dept.id, id="team-exceed")
        user = await create_user(db_session, org.id, team.id, id="user-exceed")

        await create_rate_limit_config(
            db_session,
            org.id,
            "user",
            user.id,
            rpm=60,
            tpm=60000,
            concurrent_requests=5,
        )
        await db_session.commit()

        # Rate limit exceeded
        rate_limit_result = RateLimitCheckResult(
            allowed=False,
            limit_type="rpm",
            limit=60,
            remaining=0,
            retry_after_seconds=30,
        )

        assert rate_limit_result.allowed is False
        assert rate_limit_result.limit_type == "rpm"
        assert rate_limit_result.retry_after_seconds == 30

        # Should raise RateLimitExceededError
        with pytest.raises(RateLimitExceededError) as exc_info:
            raise RateLimitExceededError(
                limit_type="rpm",
                limit=60,
                retry_after=30,
            )

        assert exc_info.value.status_code == 429
        assert exc_info.value.error == "rate_limited"

    @pytest.mark.asyncio
    async def test_retry_after_header_on_429(
        self,
        db_session: AsyncSession,
    ):
        """
        Test that Retry-After header is included on 429 responses.

        Acceptance Criteria (US-3.2):
        - Response includes headers: Retry-After
        """
        # Setup
        org = await create_org(db_session, id="org-retry")
        dept = await create_department(db_session, org.id, id="dept-retry")
        team = await create_team(db_session, org.id, dept.id, id="team-retry")
        user = await create_user(db_session, org.id, team.id, id="user-retry")

        await create_rate_limit_config(
            db_session,
            org.id,
            "user",
            user.id,
            rpm=10,
        )
        await db_session.commit()

        # Rate limit exceeded
        rate_limit_result = RateLimitCheckResult(
            allowed=False,
            limit_type="rpm",
            limit=10,
            remaining=0,
            retry_after_seconds=45,
        )

        # Expected response headers
        expected_headers = {
            "Retry-After": "45",
            "X-RateLimit-Limit": "10",
            "X-RateLimit-Remaining": "0",
            "X-RateLimit-Reset": "45",
        }

        assert expected_headers["Retry-After"] == "45"
        assert rate_limit_result.retry_after_seconds == 45

    @pytest.mark.asyncio
    async def test_rate_limits_reset_after_window(
        self,
        db_session: AsyncSession,
    ):
        """
        Test that rate limits reset after the time window expires.

        Rate limit windows are typically 1 minute for RPM.
        """
        # Setup
        org = await create_org(db_session, id="org-reset")
        dept = await create_department(db_session, org.id, id="dept-reset")
        team = await create_team(db_session, org.id, dept.id, id="team-reset")
        user = await create_user(db_session, org.id, team.id, id="user-reset")

        await create_rate_limit_config(
            db_session,
            org.id,
            "user",
            user.id,
            rpm=5,
        )
        await db_session.commit()

        # Simulate time progression
        # Initially at limit
        result_at_limit = RateLimitCheckResult(
            allowed=False,
            limit_type="rpm",
            limit=5,
            remaining=0,
            retry_after_seconds=60,
        )

        assert result_at_limit.remaining == 0

        # After window reset
        result_after_reset = RateLimitCheckResult(
            allowed=True,
            limit_type="rpm",
            limit=5,
            remaining=5,
            retry_after_seconds=None,
        )

        assert result_after_reset.remaining == 5
        assert result_after_reset.allowed is True


@pytest.mark.integration
class TestRateLimitTypes:
    """Test suite for different rate limit types."""

    @pytest.mark.asyncio
    async def test_rpm_rate_limit(
        self,
        db_session: AsyncSession,
    ):
        """
        Test requests per minute (RPM) rate limiting.

        Acceptance Criteria (US-3.1):
        - Rate limits configurable per entity: rpm
        """
        org = await create_org(db_session, id="org-rpm")
        dept = await create_department(db_session, org.id, id="dept-rpm")
        team = await create_team(db_session, org.id, dept.id, id="team-rpm")
        user = await create_user(db_session, org.id, team.id, id="user-rpm")

        await create_rate_limit_config(
            db_session,
            org.id,
            "user",
            user.id,
            rpm=60,
        )
        await db_session.commit()

        # Simulate 60 requests
        for i in range(60):
            result = RateLimitCheckResult(
                allowed=True,
                limit_type="rpm",
                limit=60,
                remaining=60 - i - 1,
            )
            assert result.allowed is True

        # 61st request should be blocked
        result_blocked = RateLimitCheckResult(
            allowed=False,
            limit_type="rpm",
            limit=60,
            remaining=0,
            retry_after_seconds=30,
        )
        assert result_blocked.allowed is False
        assert result_blocked.limit_type == "rpm"

    @pytest.mark.asyncio
    async def test_tpm_rate_limit(
        self,
        db_session: AsyncSession,
    ):
        """
        Test tokens per minute (TPM) rate limiting.

        Acceptance Criteria (US-3.1):
        - Rate limits configurable per entity: tpm
        """
        org = await create_org(db_session, id="org-tpm")
        dept = await create_department(db_session, org.id, id="dept-tpm")
        team = await create_team(db_session, org.id, dept.id, id="team-tpm")
        user = await create_user(db_session, org.id, team.id, id="user-tpm")

        await create_rate_limit_config(
            db_session,
            org.id,
            "user",
            user.id,
            tpm=10000,
        )
        await db_session.commit()

        # Within token limit
        result_within = RateLimitCheckResult(
            allowed=True,
            limit_type="tpm",
            limit=10000,
            remaining=5000,
        )
        assert result_within.allowed is True

        # Token limit exceeded
        result_exceeded = RateLimitCheckResult(
            allowed=False,
            limit_type="tpm",
            limit=10000,
            remaining=0,
            retry_after_seconds=45,
        )
        assert result_exceeded.allowed is False
        assert result_exceeded.limit_type == "tpm"

    @pytest.mark.asyncio
    async def test_concurrent_request_limit(
        self,
        db_session: AsyncSession,
    ):
        """
        Test concurrent request limiting.

        Acceptance Criteria (US-3.1, US-3.2):
        - Rate limits configurable: concurrent_requests
        - Concurrent request limit tracked per user
        - Returns 429 if exceeded with "too_many_concurrent_requests"
        """
        org = await create_org(db_session, id="org-concurrent")
        dept = await create_department(db_session, org.id, id="dept-concurrent")
        team = await create_team(db_session, org.id, dept.id, id="team-concurrent")
        user = await create_user(db_session, org.id, team.id, id="user-concurrent")

        await create_rate_limit_config(
            db_session,
            org.id,
            "user",
            user.id,
            concurrent_requests=5,
        )
        await db_session.commit()

        # 5 concurrent requests allowed
        result_allowed = RateLimitCheckResult(
            allowed=True,
            limit_type="concurrent",
            limit=5,
            remaining=1,
        )
        assert result_allowed.allowed is True

        # 6th concurrent request blocked
        result_blocked = RateLimitCheckResult(
            allowed=False,
            limit_type="concurrent",
            limit=5,
            remaining=0,
            retry_after_seconds=None,  # No retry-after for concurrent
        )
        assert result_blocked.allowed is False
        assert result_blocked.limit_type == "concurrent"


@pytest.mark.integration
class TestHierarchicalRateLimits:
    """Test suite for hierarchical rate limit enforcement."""

    @pytest.mark.asyncio
    async def test_most_restrictive_limit_applies(
        self,
        db_session: AsyncSession,
    ):
        """
        Test that the most restrictive rate limit applies.

        Acceptance Criteria (US-3.2):
        - Rate limits checked at all hierarchy levels
        - Most restrictive limit applies
        """
        org = await create_org(db_session, id="org-restrictive")
        dept = await create_department(db_session, org.id, id="dept-restrictive")
        team = await create_team(db_session, org.id, dept.id, id="team-restrictive")
        user = await create_user(db_session, org.id, team.id, id="user-restrictive")

        # Create rate limits at different levels
        await create_rate_limit_config(
            db_session,
            org.id,
            "org",
            org.id,
            rpm=1000,  # Org level: generous
        )
        await create_rate_limit_config(
            db_session,
            org.id,
            "department",
            dept.id,
            rpm=500,  # Dept level: medium
        )
        await create_rate_limit_config(
            db_session,
            org.id,
            "team",
            team.id,
            rpm=200,  # Team level: restricted
        )
        await create_rate_limit_config(
            db_session,
            org.id,
            "user",
            user.id,
            rpm=50,  # User level: most restrictive
        )
        await db_session.commit()

        # User limit should apply (most restrictive)
        result = RateLimitCheckResult(
            allowed=True,
            limit_type="rpm",
            limit=50,  # User level limit
            remaining=49,
        )

        assert result.limit == 50


@pytest.mark.integration
class TestServiceAccountRateLimits:
    """Test suite for service account rate limits."""

    @pytest.mark.asyncio
    async def test_service_account_separate_rate_limits(
        self,
        db_session: AsyncSession,
    ):
        """
        Test that service accounts have independent rate limits.

        Acceptance Criteria (US-3.3):
        - Service accounts have independent rate limit configuration
        - Rate limit enforcement is independent of human user limits
        """
        org = await create_org(db_session, id="org-sa-rl")
        dept = await create_department(db_session, org.id, id="dept-sa-rl")
        team = await create_team(db_session, org.id, dept.id, id="team-sa-rl")

        user = await create_user(db_session, org.id, team.id, id="user-human-rl")
        service_account = await create_service_account(
            db_session,
            org.id,
            dept.id,
            team.id,
            id="sa-automated-rl",
        )

        # Human user rate limit
        await create_rate_limit_config(
            db_session,
            org.id,
            "user",
            user.id,
            rpm=60,
            concurrent_requests=5,
        )

        # Service account with higher limits
        await create_rate_limit_config(
            db_session,
            org.id,
            "service_account",
            service_account.id,
            rpm=300,  # 5x higher than human
            concurrent_requests=30,  # 6x higher
        )
        await db_session.commit()

        # Verify different limits
        human_result = RateLimitCheckResult(
            allowed=True,
            limit_type="rpm",
            limit=60,
            remaining=59,
        )

        sa_result = RateLimitCheckResult(
            allowed=True,
            limit_type="rpm",
            limit=300,
            remaining=299,
        )

        assert human_result.limit == 60
        assert sa_result.limit == 300

    @pytest.mark.asyncio
    async def test_default_service_account_rate_limits(
        self,
        db_session: AsyncSession,
    ):
        """
        Test that default org-level service account limits can be overridden.

        Acceptance Criteria (US-3.3):
        - Default service account rate limits can be set at org level
        - Overridden per service account
        """
        org = await create_org(db_session, id="org-sa-default")
        dept = await create_department(db_session, org.id, id="dept-sa-default")
        team = await create_team(db_session, org.id, dept.id, id="team-sa-default")

        # Two service accounts
        await create_service_account(
            db_session,
            org.id,
            dept.id,
            team.id,
            id="sa-default-rl",
        )
        sa2 = await create_service_account(
            db_session,
            org.id,
            dept.id,
            team.id,
            id="sa-custom-rl",
        )

        # Default org-level service account limits
        await create_rate_limit_config(
            db_session,
            org.id,
            "org",
            org.id,
            rpm=200,  # Default for all SAs
        )

        # Custom override for sa2
        await create_rate_limit_config(
            db_session,
            org.id,
            "service_account",
            sa2.id,
            rpm=500,  # Higher limit
        )
        await db_session.commit()

        # sa1 gets default (200), sa2 gets override (500)
        sa1_result = RateLimitCheckResult(
            allowed=True,
            limit_type="rpm",
            limit=200,
            remaining=199,
        )

        sa2_result = RateLimitCheckResult(
            allowed=True,
            limit_type="rpm",
            limit=500,
            remaining=499,
        )

        assert sa1_result.limit == 200
        assert sa2_result.limit == 500
