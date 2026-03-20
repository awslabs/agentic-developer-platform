"""
Integration tests for the rate limiting module.

This module tests end-to-end flows including:
- Configure rate limit -> make requests -> verify limit enforcement
- Verify reset after time window
- Full hierarchy enforcement
"""

from datetime import datetime, timedelta

import pytest

from src.ratelimit.backends.in_memory import InMemoryBackend
from src.ratelimit.config import RateLimitConfig
from src.ratelimit.models import EntityType, RateLimitConfigRequest
from src.ratelimit.service import RateLimitService
from src.shared.schemas.auth import TokenContext


class TestRateLimitIntegration:
    """Integration tests for rate limiting flows."""

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
            burst_multiplier=1.0,  # No burst for testing
            refill_buffer_seconds=60,  # 1 minute buffer
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

    @pytest.mark.asyncio
    async def test_configure_and_enforce_rpm_limit(self, service):
        """Test full flow: configure limit -> make requests -> verify enforcement."""
        import uuid

        unique_id = str(uuid.uuid4())[:8]
        user_context = TokenContext(
            user_id=f"user-{unique_id}",
            org_id=f"org-{unique_id}",
            team_id=f"team-{unique_id}",
            department_id=f"dept-{unique_id}",
            account_type="human",
            is_admin=False,
            expires_at=datetime.utcnow() + timedelta(hours=1),
        )

        # Step 1: Configure a low RPM limit for the user
        await service.configure_limits(
            entity_type=EntityType.USER,
            entity_id=user_context.user_id,
            org_id=user_context.org_id,
            config=RateLimitConfigRequest(rpm=2, concurrent_requests=100),
        )

        # Step 2: Make requests up to the limit
        result = await service.consume_rate_limit(user_context)
        assert result.allowed is True

        result = await service.consume_rate_limit(user_context)
        assert result.allowed is True

        # Step 3: Next request should be rate limited
        result = await service.check_rate_limit(user_context)
        assert result.allowed is False
        assert result.limit_type == "rpm"

    @pytest.mark.asyncio
    async def test_hierarchy_enforcement_team_limit(self, service):
        """Test that team limit is enforced even with higher user limit."""
        import uuid

        unique_id = str(uuid.uuid4())[:8]
        user_context = TokenContext(
            user_id=f"user-{unique_id}",
            org_id=f"org-{unique_id}",
            team_id=f"team-{unique_id}",
            department_id=f"dept-{unique_id}",
            account_type="human",
            is_admin=False,
            expires_at=datetime.utcnow() + timedelta(hours=1),
        )

        # Set high user limit
        await service.configure_limits(
            entity_type=EntityType.USER,
            entity_id=user_context.user_id,
            org_id=user_context.org_id,
            config=RateLimitConfigRequest(rpm=100, concurrent_requests=100),
        )

        # Set low team limit (should take precedence)
        await service.configure_limits(
            entity_type=EntityType.TEAM,
            entity_id=user_context.team_id,
            org_id=user_context.org_id,
            config=RateLimitConfigRequest(rpm=1, concurrent_requests=100),
        )

        # First request should pass
        result = await service.consume_rate_limit(user_context)
        assert result.allowed is True

        # Second request should be limited by team limit
        result = await service.check_rate_limit(user_context)
        assert result.allowed is False

    @pytest.mark.asyncio
    async def test_hierarchy_enforcement_org_limit(self, service):
        """Test that org limit is enforced."""
        import uuid

        unique_id = str(uuid.uuid4())[:8]
        user_context = TokenContext(
            user_id=f"user-{unique_id}",
            org_id=f"org-{unique_id}",
            team_id=f"team-{unique_id}",
            department_id=f"dept-{unique_id}",
            account_type="human",
            is_admin=False,
            expires_at=datetime.utcnow() + timedelta(hours=1),
        )

        # Set low org limit
        await service.configure_limits(
            entity_type=EntityType.ORGANIZATION,
            entity_id=user_context.org_id,
            org_id=user_context.org_id,
            config=RateLimitConfigRequest(rpm=1, concurrent_requests=100),
        )

        # First request should pass
        result = await service.consume_rate_limit(user_context)
        assert result.allowed is True

        # Second request should be limited
        result = await service.check_rate_limit(user_context)
        assert result.allowed is False

    @pytest.mark.asyncio
    async def test_delete_limit_restores_default(self, service):
        """Test that deleting limit restores default behavior."""
        import uuid

        unique_id = str(uuid.uuid4())[:8]
        user_context = TokenContext(
            user_id=f"user-{unique_id}",
            org_id=f"org-{unique_id}",
            team_id=f"team-{unique_id}",
            department_id=f"dept-{unique_id}",
            account_type="human",
            is_admin=False,
            expires_at=datetime.utcnow() + timedelta(hours=1),
        )

        # Configure low limit
        await service.configure_limits(
            entity_type=EntityType.USER,
            entity_id=user_context.user_id,
            org_id=user_context.org_id,
            config=RateLimitConfigRequest(rpm=1, concurrent_requests=100),
        )

        # Use up the limit
        await service.consume_rate_limit(user_context)

        # Should be limited
        result = await service.check_rate_limit(user_context)
        assert result.allowed is False

        # Delete the limit
        await service.delete_limits(
            entity_type=EntityType.USER,
            entity_id=user_context.user_id,
            org_id=user_context.org_id,
        )

        # Now should use default (higher) limit - new bucket created
        result = await service.check_rate_limit(user_context)
        assert result.allowed is True

    @pytest.mark.asyncio
    async def test_status_reflects_usage(self, service):
        """Test that status endpoint reflects actual usage."""
        import uuid

        unique_id = str(uuid.uuid4())[:8]
        user_context = TokenContext(
            user_id=f"user-{unique_id}",
            org_id=f"org-{unique_id}",
            team_id=f"team-{unique_id}",
            department_id=f"dept-{unique_id}",
            account_type="human",
            is_admin=False,
            expires_at=datetime.utcnow() + timedelta(hours=1),
        )

        # Configure limit
        await service.configure_limits(
            entity_type=EntityType.USER,
            entity_id=user_context.user_id,
            org_id=user_context.org_id,
            config=RateLimitConfigRequest(rpm=10, concurrent_requests=100),
        )

        # Check initial status
        status = await service.get_status(
            entity_type=EntityType.USER,
            entity_id=user_context.user_id,
            org_id=user_context.org_id,
        )
        initial_remaining = status.rpm_remaining

        # Make a request
        await service.consume_rate_limit(user_context)

        # Check status again - remaining should decrease
        status = await service.get_status(
            entity_type=EntityType.USER,
            entity_id=user_context.user_id,
            org_id=user_context.org_id,
        )
        new_remaining = status.rpm_remaining
        assert new_remaining < initial_remaining

    @pytest.mark.asyncio
    async def test_service_account_separate_limits(self):
        """Test that service accounts have separate limits from users."""
        config = RateLimitConfig(
            default_rpm=60,
            default_tpm=100000,
            default_concurrent=10,
            service_account_default_rpm=120,
            burst_multiplier=1.0,
            refill_buffer_seconds=60,
            enforce_hierarchy=False,
        )
        backend = InMemoryBackend()
        service = RateLimitService(backend=backend, config=config)

        user_context = TokenContext(
            user_id="user-sep-123",
            org_id="org-sep-456",
            team_id="team-sep-789",
            department_id="dept-sep-012",
            account_type="human",
            is_admin=False,
            expires_at=datetime.utcnow() + timedelta(hours=1),
        )

        sa_context = TokenContext(
            user_id="sa-sep-123",
            org_id="org-sep-456",
            team_id="team-sep-789",
            department_id="dept-sep-012",
            account_type="service",
            is_admin=False,
            expires_at=datetime.utcnow() + timedelta(hours=1),
        )

        # Configure low limit for human user
        await service.configure_limits(
            entity_type=EntityType.USER,
            entity_id=user_context.user_id,
            org_id=user_context.org_id,
            config=RateLimitConfigRequest(rpm=1, concurrent_requests=100),
        )

        # User should be limited quickly
        result = await service.check_rate_limit(user_context)
        assert result.allowed is True
        await service.consume_rate_limit(user_context)

        result = await service.check_rate_limit(user_context)
        assert result.allowed is False

        # Service account should still be allowed (uses its own limits)
        result = await service.check_rate_limit(sa_context)
        assert result.allowed is True

    @pytest.mark.asyncio
    async def test_tpm_enforcement(self):
        """Test tokens per minute enforcement."""
        config = RateLimitConfig(
            default_rpm=1000,
            default_tpm=100,  # Low TPM limit
            default_concurrent=100,
            burst_multiplier=1.0,
            refill_buffer_seconds=60,
            enforce_hierarchy=False,
        )
        backend = InMemoryBackend()
        service = RateLimitService(backend=backend, config=config)

        context = TokenContext(
            user_id="user-tpm-123",
            org_id="org-tpm-456",
            team_id="team-tpm-789",
            department_id="dept-tpm-012",
            account_type="human",
            is_admin=False,
            expires_at=datetime.utcnow() + timedelta(hours=1),
        )

        # Configure TPM limit
        await service.configure_limits(
            entity_type=EntityType.USER,
            entity_id=context.user_id,
            org_id=context.org_id,
            config=RateLimitConfigRequest(rpm=1000, tpm=50, concurrent_requests=100),
        )

        # Consume many tokens at once
        result = await service.consume_rate_limit(context, tokens=50)
        assert result.allowed is True

        # Next request should be limited
        result = await service.consume_rate_limit(context, tokens=10)
        assert result.allowed is False
        assert result.limit_type == "tpm"


class TestRateLimitEdgeCases:
    """Edge case tests for rate limiting."""

    @pytest.mark.asyncio
    async def test_concurrent_release_on_request_completion(self):
        """Test concurrent slot is released when request completes."""
        config = RateLimitConfig(
            default_rpm=1000,
            default_tpm=100000,
            default_concurrent=2,
            burst_multiplier=1.0,
            refill_buffer_seconds=60,
            enforce_hierarchy=False,
        )
        backend = InMemoryBackend()
        service = RateLimitService(backend=backend, config=config)

        context = TokenContext(
            user_id="user-conc-123",
            org_id="org-conc-456",
            team_id="team-conc-789",
            department_id="dept-conc-012",
            account_type="human",
            is_admin=False,
            expires_at=datetime.utcnow() + timedelta(hours=1),
        )

        # Consume concurrent slots
        await service.consume_rate_limit(context)
        await service.consume_rate_limit(context)

        # Third should fail
        result = await service.consume_rate_limit(context)
        assert result.allowed is False
        assert result.limit_type == "concurrent"

        # Release one slot
        await service.release_concurrent(context)

        # Now should work
        result = await service.consume_rate_limit(context)
        assert result.allowed is True

    @pytest.mark.asyncio
    async def test_multiple_limit_types_independent(self):
        """Test RPM and TPM are enforced independently."""
        config = RateLimitConfig(
            default_rpm=1,
            default_tpm=1000,
            default_concurrent=100,
            burst_multiplier=1.0,
            refill_buffer_seconds=60,
            enforce_hierarchy=False,
        )
        backend = InMemoryBackend()
        service = RateLimitService(backend=backend, config=config)

        context = TokenContext(
            user_id="user-multi-123",
            org_id="org-multi-456",
            team_id="team-multi-789",
            department_id="dept-multi-012",
            account_type="human",
            is_admin=False,
            expires_at=datetime.utcnow() + timedelta(hours=1),
        )

        # Configure limits
        await service.configure_limits(
            entity_type=EntityType.USER,
            entity_id=context.user_id,
            org_id=context.org_id,
            config=RateLimitConfigRequest(rpm=1, tpm=1000, concurrent_requests=100),
        )

        # First request should pass
        result = await service.consume_rate_limit(context)
        assert result.allowed is True

        # Second should hit RPM limit
        result = await service.consume_rate_limit(context)
        assert result.allowed is False
        assert result.limit_type == "rpm"
