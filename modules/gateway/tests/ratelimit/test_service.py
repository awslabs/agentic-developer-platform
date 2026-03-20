"""
Unit tests for the RateLimitService.

This module tests the rate limit service including:
- IRateLimitService implementation
- Hierarchy enforcement
- Configuration management
- Limit checks and consumption
"""

from datetime import datetime, timedelta

import pytest

from src.ratelimit.backends.in_memory import InMemoryBackend
from src.ratelimit.config import RateLimitConfig
from src.ratelimit.models import EntityType, RateLimitConfigRequest
from src.ratelimit.service import RateLimitService
from src.shared.schemas.auth import TokenContext


class TestRateLimitService:
    """Tests for RateLimitService class."""

    @pytest.fixture
    def config(self):
        """Provide test configuration."""
        return RateLimitConfig(
            default_rpm=60,
            default_tpm=100000,
            default_concurrent=10,
            service_account_default_rpm=120,
            service_account_default_tpm=200000,
            service_account_default_concurrent=20,
            burst_multiplier=1.5,
            refill_buffer_seconds=10,
            backend_type="memory",
            enforce_hierarchy=True,
        )

    @pytest.fixture
    def backend(self):
        """Provide a fresh InMemoryBackend."""
        return InMemoryBackend()

    @pytest.fixture
    def service(self, backend, config):
        """Provide a RateLimitService instance."""
        return RateLimitService(backend=backend, config=config)

    @pytest.fixture
    def human_context(self):
        """Provide a human user context."""
        return TokenContext(
            user_id="user-123",
            org_id="org-456",
            team_id="team-789",
            department_id="dept-012",
            account_type="human",
            is_admin=False,
            expires_at=datetime.utcnow() + timedelta(hours=1),
        )

    @pytest.fixture
    def service_context(self):
        """Provide a service account context."""
        return TokenContext(
            user_id="sa-123",
            org_id="org-456",
            team_id="team-789",
            department_id="dept-012",
            account_type="service",
            is_admin=False,
            expires_at=datetime.utcnow() + timedelta(hours=1),
        )

    @pytest.mark.asyncio
    async def test_check_rate_limit_allowed(self, service, human_context):
        """Test check_rate_limit returns allowed for new user."""
        result = await service.check_rate_limit(human_context)
        assert result.allowed is True

    @pytest.mark.asyncio
    async def test_check_rate_limit_denied_after_exceeding(self, service, human_context):
        """Test check_rate_limit returns denied after exceeding limit."""
        # Configure a very low limit
        await service.configure_limits(
            entity_type=EntityType.USER,
            entity_id=human_context.user_id,
            org_id=human_context.org_id,
            config=RateLimitConfigRequest(rpm=1, concurrent_requests=100),
        )

        # First check should pass
        result = await service.check_rate_limit(human_context)
        assert result.allowed is True

        # Consume the token
        await service.consume_rate_limit(human_context)

        # Now should be denied
        result = await service.check_rate_limit(human_context)
        assert result.allowed is False
        assert result.limit_type == "rpm"

    @pytest.mark.asyncio
    async def test_consume_rate_limit(self, service, human_context):
        """Test consume_rate_limit consumes tokens."""
        result = await service.consume_rate_limit(human_context)
        assert result.allowed is True

    @pytest.mark.asyncio
    async def test_consume_rate_limit_with_tokens(self, service, human_context):
        """Test consume_rate_limit with token count."""
        result = await service.consume_rate_limit(human_context, tokens=100)
        assert result.allowed is True

    @pytest.mark.asyncio
    async def test_release_concurrent(self, service, human_context):
        """Test release_concurrent decrements counter."""
        # First consume to increment
        await service.consume_rate_limit(human_context)

        # Then release
        await service.release_concurrent(human_context)
        # No assertion needed, just verify it doesn't raise

    @pytest.mark.asyncio
    async def test_configure_limits(self, service):
        """Test configure_limits sets rate limits."""
        result = await service.configure_limits(
            entity_type=EntityType.USER,
            entity_id="user-123",
            org_id="org-456",
            config=RateLimitConfigRequest(rpm=100, tpm=50000, concurrent_requests=5),
        )

        assert result.entity_type == "user"
        assert result.entity_id == "user-123"
        assert result.org_id == "org-456"
        assert result.rpm == 100
        assert result.tpm == 50000
        assert result.concurrent_requests == 5

    @pytest.mark.asyncio
    async def test_get_limits(self, service):
        """Test get_limits retrieves configured limits."""
        # First configure
        await service.configure_limits(
            entity_type=EntityType.USER,
            entity_id="user-123",
            org_id="org-456",
            config=RateLimitConfigRequest(rpm=100),
        )

        # Then get
        result = await service.get_limits(
            entity_type=EntityType.USER,
            entity_id="user-123",
            org_id="org-456",
        )

        assert result is not None
        assert result.rpm == 100

    @pytest.mark.asyncio
    async def test_get_limits_nonexistent(self, service):
        """Test get_limits returns None for unconfigured entity."""
        result = await service.get_limits(
            entity_type=EntityType.USER,
            entity_id="nonexistent",
            org_id="org-456",
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_get_status(self, service, human_context):
        """Test get_status returns current status."""
        status = await service.get_status(
            entity_type=EntityType.USER,
            entity_id=human_context.user_id,
            org_id=human_context.org_id,
        )

        assert status.entity_type == "user"
        assert status.entity_id == human_context.user_id
        assert status.rpm_limit == 60  # Default
        assert status.rpm_remaining is not None

    @pytest.mark.asyncio
    async def test_delete_limits(self, service):
        """Test delete_limits removes configured limits."""
        # First configure
        await service.configure_limits(
            entity_type=EntityType.USER,
            entity_id="user-123",
            org_id="org-456",
            config=RateLimitConfigRequest(rpm=100),
        )

        # Then delete
        deleted = await service.delete_limits(
            entity_type=EntityType.USER,
            entity_id="user-123",
            org_id="org-456",
        )
        assert deleted is True

        # Verify deleted
        result = await service.get_limits(
            entity_type=EntityType.USER,
            entity_id="user-123",
            org_id="org-456",
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_delete_limits_nonexistent(self, service):
        """Test delete_limits returns False for unconfigured entity."""
        deleted = await service.delete_limits(
            entity_type=EntityType.USER,
            entity_id="nonexistent",
            org_id="org-456",
        )
        assert deleted is False

    @pytest.mark.asyncio
    async def test_service_account_higher_defaults(self, service, service_context):
        """Test service accounts get higher default limits."""
        status = await service.get_status(
            entity_type=EntityType.SERVICE_ACCOUNT,
            entity_id=service_context.user_id,
            org_id=service_context.org_id,
            is_service_account=True,
        )

        assert status.rpm_limit == 120  # Service account default
        assert status.concurrent_limit == 20  # Service account default

    @pytest.mark.asyncio
    async def test_hierarchy_enforcement(self, service, human_context):
        """Test hierarchical rate limit enforcement."""
        # Set a low limit at team level
        await service.configure_limits(
            entity_type=EntityType.TEAM,
            entity_id=human_context.team_id,
            org_id=human_context.org_id,
            config=RateLimitConfigRequest(rpm=1, concurrent_requests=100),
        )

        # First request should pass
        result = await service.check_rate_limit(human_context)
        assert result.allowed is True

        # Consume team's tokens
        await service.consume_rate_limit(human_context)

        # Second request should fail (team limit hit)
        result = await service.check_rate_limit(human_context)
        assert result.allowed is False

    @pytest.mark.asyncio
    async def test_hierarchy_disabled(self, backend):
        """Test hierarchy can be disabled."""
        config = RateLimitConfig(
            default_rpm=60,
            default_tpm=100000,
            default_concurrent=10,
            enforce_hierarchy=False,  # Disabled
        )
        service = RateLimitService(backend=backend, config=config)

        context = TokenContext(
            user_id="user-123",
            org_id="org-456",
            team_id="team-789",
            department_id="dept-012",
            account_type="human",
            is_admin=False,
            expires_at=datetime.utcnow() + timedelta(hours=1),
        )

        # Set a low limit at team level
        await service.configure_limits(
            entity_type=EntityType.TEAM,
            entity_id=context.team_id,
            org_id=context.org_id,
            config=RateLimitConfigRequest(rpm=1, concurrent_requests=100),
        )

        # First request should pass
        result = await service.check_rate_limit(context)
        assert result.allowed is True

        # Consume user's token
        await service.consume_rate_limit(context)

        # Should still pass because user limit (default) not hit
        # and team limit is ignored when hierarchy disabled
        result = await service.check_rate_limit(context)
        assert result.allowed is True

    @pytest.mark.asyncio
    async def test_concurrent_limit_enforcement(self, service, human_context):
        """Test concurrent request limit enforcement."""
        # Set a low concurrent limit
        await service.configure_limits(
            entity_type=EntityType.USER,
            entity_id=human_context.user_id,
            org_id=human_context.org_id,
            config=RateLimitConfigRequest(rpm=1000, concurrent_requests=2),
        )

        # First two requests should pass
        result1 = await service.consume_rate_limit(human_context)
        assert result1.allowed is True

        result2 = await service.consume_rate_limit(human_context)
        assert result2.allowed is True

        # Third should fail
        result3 = await service.consume_rate_limit(human_context)
        assert result3.allowed is False
        assert result3.limit_type == "concurrent"

    @pytest.mark.asyncio
    async def test_close(self, service):
        """Test close releases resources."""
        await service.close()
        # No assertion needed, just verify it doesn't raise

    @pytest.mark.asyncio
    async def test_entity_key_generation(self, service):
        """Test entity key generation is consistent."""
        key1 = service._get_entity_key(EntityType.USER, "user-123", "org-456")
        key2 = service._get_entity_key(EntityType.USER, "user-123", "org-456")
        assert key1 == key2

        key3 = service._get_entity_key(EntityType.TEAM, "team-123", "org-456")
        assert key1 != key3


class TestRateLimitServiceWithMock:
    """Tests for RateLimitService with mocked backend."""

    @pytest.mark.asyncio
    async def test_check_rate_limit_backend_called(self, mock_backend):
        """Test check_rate_limit calls backend correctly."""
        config = RateLimitConfig(
            default_rpm=60,
            default_tpm=100000,
            default_concurrent=10,
        )
        service = RateLimitService(backend=mock_backend, config=config)

        context = TokenContext(
            user_id="user-123",
            org_id="org-456",
            team_id="team-789",
            department_id="dept-012",
            account_type="human",
            is_admin=False,
            expires_at=datetime.utcnow() + timedelta(hours=1),
        )

        await service.check_rate_limit(context)

        # Backend should have been called
        mock_backend.check_limit.assert_called()
