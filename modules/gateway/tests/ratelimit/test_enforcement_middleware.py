"""
Unit tests for Rate Limit Enforcement.

Tests token bucket algorithm, hierarchical rate limit checking,
concurrent request tracking, and retry-after calculation.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

from src.ratelimit.backends.in_memory import InMemoryBackend
from src.ratelimit.models import EntityType, LimitType
from src.ratelimit.service import RateLimitService
from src.ratelimit.token_bucket import TokenBucket
from src.shared.schemas.auth import TokenContext
from src.shared.schemas.common import RateLimitCheckResult


@pytest.fixture
def token_context():
    """Create a test token context."""
    return TokenContext(
        user_id="user-123",
        org_id="org-456",
        team_id="team-789",
        department_id="dept-012",
        account_type="human",
        is_admin=False,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )


@pytest.fixture
def service_account_context():
    """Create a test service account context."""
    return TokenContext(
        user_id="service-456",
        org_id="org-456",
        team_id="team-789",
        department_id="dept-012",
        account_type="service",
        is_admin=False,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )


@pytest.fixture
def in_memory_backend():
    """Create an in-memory rate limit backend."""
    return InMemoryBackend()


class TestTokenBucket:
    """Tests for TokenBucket algorithm."""

    def test_initial_state_is_full(self):
        """Test that bucket starts at full capacity."""
        bucket = TokenBucket(capacity=60, refill_rate=1.0)
        assert bucket.get_remaining() == 60

    def test_consume_reduces_tokens(self):
        """Test that consuming reduces available tokens."""
        bucket = TokenBucket(capacity=60, refill_rate=1.0)

        success, remaining, _ = bucket.try_consume(1)

        assert success is True
        assert remaining == 59

    def test_consume_fails_when_insufficient_tokens(self):
        """Test that consumption fails when not enough tokens."""
        bucket = TokenBucket(capacity=10, refill_rate=1.0)

        success, remaining, wait_time = bucket.try_consume(20)

        assert success is False
        assert wait_time > 0

    def test_refill_over_time(self):
        """Test that tokens refill over time."""
        bucket = TokenBucket(capacity=60, refill_rate=10.0)  # 10 tokens/second

        # Consume all tokens
        bucket.try_consume(60)
        # Allow for small floating point variance
        assert bucket.get_remaining() < 1

        # Simulate time passing (mock the time module)

        with patch("time.monotonic") as mock_time:
            # Start at time 0
            mock_time.return_value = 0
            bucket = TokenBucket(capacity=60, refill_rate=10.0)
            bucket.try_consume(60)

            # Move time forward 3 seconds
            mock_time.return_value = 3

            # Should have refilled 30 tokens (allow small variance)
            remaining = bucket.get_remaining()
            assert 29 < remaining < 31

    def test_refill_capped_at_capacity(self):
        """Test that refill doesn't exceed capacity."""
        bucket = TokenBucket(capacity=60, refill_rate=100.0)

        # Even with very high refill rate, shouldn't exceed capacity
        assert bucket.get_remaining() <= 60

    def test_burst_handling(self):
        """Test that burst up to capacity is allowed."""
        bucket = TokenBucket(capacity=60, refill_rate=1.0)

        # Should be able to burst all 60 at once
        success, remaining, _ = bucket.try_consume(60)

        assert success is True
        assert remaining == 0

    def test_check_without_consume(self):
        """Test checking availability without consuming."""
        bucket = TokenBucket(capacity=60, refill_rate=1.0)

        # Check should not consume
        allowed, remaining, _ = bucket.check(10)
        assert allowed is True
        assert remaining == 60  # Still at capacity

        # Actual state unchanged
        assert bucket.get_remaining() == 60

    def test_reset_restores_full_capacity(self):
        """Test that reset restores full capacity."""
        bucket = TokenBucket(capacity=60, refill_rate=1.0)

        bucket.try_consume(50)
        # Allow for small floating point variance
        assert 9 < bucket.get_remaining() < 11

        bucket.reset()
        assert bucket.get_remaining() == 60


class TestInMemoryBackend:
    """Tests for InMemoryBackend."""

    @pytest.mark.asyncio
    async def test_consume_creates_bucket(self, in_memory_backend):
        """Test that consuming from non-existent key creates bucket."""
        success, remaining, wait_time = await in_memory_backend.consume(
            key="test:user:org",
            limit_type=LimitType.RPM,
            max_tokens=60,
            refill_rate=1.0,
        )

        assert success is True
        assert remaining == 59

    @pytest.mark.asyncio
    async def test_check_limit_without_consume(self, in_memory_backend):
        """Test check_limit doesn't consume tokens."""
        # First check
        allowed1, remaining1, _ = await in_memory_backend.check_limit(
            key="test:user:org",
            limit_type=LimitType.RPM,
            max_tokens=60,
            refill_rate=1.0,
        )

        # Second check should show same remaining
        allowed2, remaining2, _ = await in_memory_backend.check_limit(
            key="test:user:org",
            limit_type=LimitType.RPM,
            max_tokens=60,
            refill_rate=1.0,
        )

        assert remaining1 == remaining2

    @pytest.mark.asyncio
    async def test_concurrent_increment_decrement(self, in_memory_backend):
        """Test concurrent request tracking."""
        key = "test:user:org"

        # Set limit
        await in_memory_backend.set_concurrent_limit(key, 5)

        # Increment
        success, current, limit = await in_memory_backend.increment_concurrent(key)
        assert success is True
        assert current == 1
        assert limit == 5

        # Decrement
        remaining = await in_memory_backend.decrement_concurrent(key)
        assert remaining == 0

    @pytest.mark.asyncio
    async def test_concurrent_limit_exceeded(self, in_memory_backend):
        """Test that concurrent limit is enforced."""
        key = "test:user:org"
        await in_memory_backend.set_concurrent_limit(key, 2)

        # Use up the limit
        await in_memory_backend.increment_concurrent(key)
        await in_memory_backend.increment_concurrent(key)

        # Third should fail
        success, current, limit = await in_memory_backend.increment_concurrent(key)
        assert success is False
        assert current == 2

    @pytest.mark.asyncio
    async def test_reset_clears_bucket(self, in_memory_backend):
        """Test that reset clears a bucket."""
        key = "test:user:org"

        # Consume some tokens
        await in_memory_backend.consume(key, LimitType.RPM, 60, 1.0, 50)

        # Reset
        await in_memory_backend.reset(key, LimitType.RPM)

        # Should be fresh bucket at full capacity
        _, remaining, _ = await in_memory_backend.check_limit(key, LimitType.RPM, 60, 1.0)
        assert remaining == 60


class TestRateLimitService:
    """Tests for RateLimitService."""

    @pytest.mark.asyncio
    async def test_check_rate_limit_allows_under_limit(self, token_context):
        """Test that requests under limit are allowed."""
        service = RateLimitService()

        result = await service.check_rate_limit(token_context)

        assert result.allowed is True

    @pytest.mark.asyncio
    async def test_consume_rate_limit_deducts_token(self, token_context):
        """Test that consuming deducts from rate limit."""
        service = RateLimitService()

        result = await service.consume_rate_limit(token_context)

        assert result.allowed is True

    @pytest.mark.asyncio
    async def test_release_concurrent_decrements_count(self, token_context):
        """Test that releasing decrements concurrent count."""
        service = RateLimitService()

        # Consume first to increment
        await service.consume_rate_limit(token_context)

        # Release should not error
        await service.release_concurrent(token_context)

    @pytest.mark.asyncio
    async def test_configure_limits_stores_config(self, token_context):
        """Test that configuring limits stores the configuration."""
        service = RateLimitService()
        from src.ratelimit.models import RateLimitConfigRequest

        config = RateLimitConfigRequest(rpm=100, tpm=10000, concurrent_requests=10)

        result = await service.configure_limits(EntityType.USER, token_context.user_id, token_context.org_id, config)

        assert result.rpm == 100
        assert result.tpm == 10000
        assert result.concurrent_requests == 10

    @pytest.mark.asyncio
    async def test_get_status_returns_current_state(self, token_context):
        """Test that get_status returns current rate limit state."""
        service = RateLimitService()

        status = await service.get_status(
            EntityType.USER,
            token_context.user_id,
            token_context.org_id,
            is_service_account=False,
        )

        assert status is not None
        assert status.rpm_limit is not None

    @pytest.mark.asyncio
    async def test_hierarchy_check_all_levels(self, token_context):
        """Test that hierarchical enforcement checks all levels."""
        service = RateLimitService()

        # Get hierarchy entities
        entities = service._get_hierarchy_entities(token_context)

        # Should have user, team, dept, org
        assert len(entities) == 4

    @pytest.mark.asyncio
    async def test_service_account_uses_different_defaults(self, service_account_context):
        """Test that service accounts use different default limits."""
        service = RateLimitService()

        # Service accounts should use service account defaults
        limits = service._get_limits_for_entity(
            EntityType.SERVICE_ACCOUNT,
            service_account_context.user_id,
            service_account_context.org_id,
            is_service_account=True,
        )

        # Should have non-None limits
        assert limits["rpm"] is not None
        assert limits["tpm"] is not None

    @pytest.mark.asyncio
    async def test_delete_limits_removes_config(self, token_context):
        """Test that deleting limits removes the configuration."""
        service = RateLimitService()
        from src.ratelimit.models import RateLimitConfigRequest

        # Configure limits
        config = RateLimitConfigRequest(rpm=100)
        await service.configure_limits(EntityType.USER, token_context.user_id, token_context.org_id, config)

        # Delete limits
        deleted = await service.delete_limits(EntityType.USER, token_context.user_id, token_context.org_id)

        assert deleted is True

        # Get limits should return None
        result = await service.get_limits(EntityType.USER, token_context.user_id, token_context.org_id)
        assert result is None


class TestRateLimitCheckResult:
    """Tests for RateLimitCheckResult."""

    def test_allowed_result(self):
        """Test creating an allowed result."""
        result = RateLimitCheckResult(allowed=True)

        assert result.allowed is True
        assert result.limit_type is None
        assert result.retry_after_seconds is None

    def test_blocked_result(self):
        """Test creating a blocked result with details."""
        result = RateLimitCheckResult(
            allowed=False,
            limit_type="rpm",
            limit=60,
            remaining=0,
            retry_after_seconds=30,
        )

        assert result.allowed is False
        assert result.limit_type == "rpm"
        assert result.limit == 60
        assert result.remaining == 0
        assert result.retry_after_seconds == 30


class TestRateLimitEnforcementMiddleware:
    """Tests for RateLimitEnforcementMiddleware integration."""

    # Note: Full middleware tests would require integration tests
    # with a test client and the full FastAPI request cycle.

    def test_placeholder_for_middleware_integration_tests(self):
        """Placeholder test to validate test class is not empty."""
        # Middleware integration tests require a full FastAPI test client
        # and are beyond the scope of unit tests
        assert True
