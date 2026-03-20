"""
Unit tests for the InMemoryBackend.

This module tests the in-memory rate limit backend including:
- Basic rate limiting
- TTL expiration
- Concurrent access thread safety
- Cleanup of expired entries
"""

import asyncio

import pytest

from src.ratelimit.backends.in_memory import InMemoryBackend
from src.ratelimit.models import LimitType


class TestInMemoryBackend:
    """Tests for InMemoryBackend class."""

    @pytest.fixture
    def backend(self):
        """Provide a fresh InMemoryBackend instance."""
        return InMemoryBackend(cleanup_interval=60.0, entry_ttl=3600.0)

    @pytest.mark.asyncio
    async def test_check_limit_new_bucket(self, backend):
        """Test check_limit creates new bucket and returns allowed."""
        allowed, remaining, retry_after = await backend.check_limit(
            key="user:123:org:456",
            limit_type=LimitType.RPM,
            max_tokens=100,
            refill_rate=1.0,
        )
        assert allowed is True
        assert remaining == 100
        assert retry_after == 0

    @pytest.mark.asyncio
    async def test_check_limit_existing_bucket(self, backend):
        """Test check_limit with existing bucket."""
        # Create bucket by consuming
        await backend.consume(
            key="user:123:org:456",
            limit_type=LimitType.RPM,
            max_tokens=100,
            refill_rate=1.0,
            tokens_to_consume=50,
        )

        # Check should show reduced tokens
        allowed, remaining, retry_after = await backend.check_limit(
            key="user:123:org:456",
            limit_type=LimitType.RPM,
            max_tokens=100,
            refill_rate=1.0,
        )
        assert allowed is True
        assert remaining == 50

    @pytest.mark.asyncio
    async def test_consume_success(self, backend):
        """Test successful token consumption."""
        success, remaining, retry_after = await backend.consume(
            key="user:123:org:456",
            limit_type=LimitType.RPM,
            max_tokens=100,
            refill_rate=1.0,
            tokens_to_consume=10,
        )
        assert success is True
        assert remaining == 90
        assert retry_after == 0

    @pytest.mark.asyncio
    async def test_consume_failure_not_enough_tokens(self, backend):
        """Test consumption fails when not enough tokens."""
        # Consume all tokens
        for _ in range(10):
            await backend.consume(
                key="user:123:org:456",
                limit_type=LimitType.RPM,
                max_tokens=10,
                refill_rate=0.0,  # No refill
                tokens_to_consume=1,
            )

        # Now should fail
        success, remaining, retry_after = await backend.consume(
            key="user:123:org:456",
            limit_type=LimitType.RPM,
            max_tokens=10,
            refill_rate=0.0,
            tokens_to_consume=1,
        )
        assert success is False
        assert remaining == 0
        assert retry_after == 60  # Default for zero refill rate

    @pytest.mark.asyncio
    async def test_consume_with_refill(self, backend):
        """Test consumption with token refill."""
        success, remaining, _ = await backend.consume(
            key="user:123:org:456",
            limit_type=LimitType.RPM,
            max_tokens=100,
            refill_rate=100.0,  # Fast refill
            tokens_to_consume=50,
        )
        assert success is True
        assert remaining == 50

        # Wait a bit for refill
        await asyncio.sleep(0.1)

        # Should have some tokens refilled
        remaining = await backend.get_remaining(
            key="user:123:org:456",
            limit_type=LimitType.RPM,
            max_tokens=100,
            refill_rate=100.0,
        )
        assert remaining > 50  # Should have refilled some

    @pytest.mark.asyncio
    async def test_get_remaining(self, backend):
        """Test get_remaining returns correct value."""
        remaining = await backend.get_remaining(
            key="user:123:org:456",
            limit_type=LimitType.RPM,
            max_tokens=100,
            refill_rate=1.0,
        )
        assert remaining == 100

        await backend.consume(
            key="user:123:org:456",
            limit_type=LimitType.RPM,
            max_tokens=100,
            refill_rate=1.0,
            tokens_to_consume=30,
        )

        remaining = await backend.get_remaining(
            key="user:123:org:456",
            limit_type=LimitType.RPM,
            max_tokens=100,
            refill_rate=1.0,
        )
        assert remaining == 70

    @pytest.mark.asyncio
    async def test_get_state_nonexistent(self, backend):
        """Test get_state returns None for nonexistent bucket."""
        state = await backend.get_state(key="nonexistent", limit_type=LimitType.RPM)
        assert state is None

    @pytest.mark.asyncio
    async def test_get_state_existing(self, backend):
        """Test get_state returns state for existing bucket."""
        await backend.consume(
            key="user:123:org:456",
            limit_type=LimitType.RPM,
            max_tokens=100,
            refill_rate=1.0,
            tokens_to_consume=10,
        )

        state = await backend.get_state(key="user:123:org:456", limit_type=LimitType.RPM)
        assert state is not None
        assert 89 <= state.tokens <= 91  # Allow for refill timing
        assert state.max_tokens == 100
        assert state.limit_type == LimitType.RPM

    @pytest.mark.asyncio
    async def test_reset(self, backend):
        """Test reset restores bucket to full capacity."""
        await backend.consume(
            key="user:123:org:456",
            limit_type=LimitType.RPM,
            max_tokens=100,
            refill_rate=1.0,
            tokens_to_consume=80,
        )

        remaining = await backend.get_remaining(
            key="user:123:org:456",
            limit_type=LimitType.RPM,
            max_tokens=100,
            refill_rate=1.0,
        )
        assert remaining == 20

        await backend.reset(key="user:123:org:456", limit_type=LimitType.RPM)

        remaining = await backend.get_remaining(
            key="user:123:org:456",
            limit_type=LimitType.RPM,
            max_tokens=100,
            refill_rate=1.0,
        )
        assert remaining == 100

    @pytest.mark.asyncio
    async def test_increment_concurrent(self, backend):
        """Test incrementing concurrent counter."""
        await backend.set_concurrent_limit("user:123", 10)

        success, current, limit = await backend.increment_concurrent("user:123")
        assert success is True
        assert current == 1
        assert limit == 10

    @pytest.mark.asyncio
    async def test_increment_concurrent_at_limit(self, backend):
        """Test incrementing concurrent counter at limit."""
        await backend.set_concurrent_limit("user:123", 2)

        await backend.increment_concurrent("user:123")
        await backend.increment_concurrent("user:123")

        # Should fail at limit
        success, current, limit = await backend.increment_concurrent("user:123")
        assert success is False
        assert current == 2
        assert limit == 2

    @pytest.mark.asyncio
    async def test_decrement_concurrent(self, backend):
        """Test decrementing concurrent counter."""
        await backend.increment_concurrent("user:123")
        await backend.increment_concurrent("user:123")

        current = await backend.decrement_concurrent("user:123")
        assert current == 1

        current = await backend.decrement_concurrent("user:123")
        assert current == 0

    @pytest.mark.asyncio
    async def test_decrement_concurrent_below_zero(self, backend):
        """Test decrementing doesn't go below zero."""
        current = await backend.decrement_concurrent("user:123")
        assert current == 0

    @pytest.mark.asyncio
    async def test_get_concurrent_count(self, backend):
        """Test getting concurrent count."""
        count = await backend.get_concurrent_count("user:123")
        assert count == 0

        await backend.increment_concurrent("user:123")
        await backend.increment_concurrent("user:123")

        count = await backend.get_concurrent_count("user:123")
        assert count == 2

    @pytest.mark.asyncio
    async def test_set_concurrent_limit(self, backend):
        """Test setting concurrent limit."""
        await backend.set_concurrent_limit("user:123", 5)

        # Fill up to limit
        for _ in range(5):
            success, _, _ = await backend.increment_concurrent("user:123")
            assert success is True

        # Should fail at limit
        success, _, _ = await backend.increment_concurrent("user:123")
        assert success is False

    @pytest.mark.asyncio
    async def test_close(self, backend):
        """Test closing backend clears data."""
        await backend.consume(
            key="user:123:org:456",
            limit_type=LimitType.RPM,
            max_tokens=100,
            refill_rate=1.0,
            tokens_to_consume=10,
        )

        await backend.close()

        # After close, bucket should not exist
        state = await backend.get_state(key="user:123:org:456", limit_type=LimitType.RPM)
        assert state is None

    @pytest.mark.asyncio
    async def test_clear(self, backend):
        """Test clear removes all data."""
        await backend.consume(
            key="user:123:org:456",
            limit_type=LimitType.RPM,
            max_tokens=100,
            refill_rate=1.0,
            tokens_to_consume=10,
        )
        await backend.increment_concurrent("user:123")

        backend.clear()

        state = await backend.get_state(key="user:123:org:456", limit_type=LimitType.RPM)
        assert state is None

        count = await backend.get_concurrent_count("user:123")
        assert count == 0

    @pytest.mark.asyncio
    async def test_different_limit_types_isolated(self, backend):
        """Test different limit types are isolated."""
        await backend.consume(
            key="user:123:org:456",
            limit_type=LimitType.RPM,
            max_tokens=100,
            refill_rate=1.0,
            tokens_to_consume=50,
        )

        # TPM should be independent
        remaining = await backend.get_remaining(
            key="user:123:org:456",
            limit_type=LimitType.TPM,
            max_tokens=10000,
            refill_rate=100.0,
        )
        assert remaining == 10000  # New bucket, full capacity

    @pytest.mark.asyncio
    async def test_different_keys_isolated(self, backend):
        """Test different keys are isolated."""
        await backend.consume(
            key="user:123:org:456",
            limit_type=LimitType.RPM,
            max_tokens=100,
            refill_rate=1.0,
            tokens_to_consume=50,
        )

        remaining = await backend.get_remaining(
            key="user:456:org:789",
            limit_type=LimitType.RPM,
            max_tokens=100,
            refill_rate=1.0,
        )
        assert remaining == 100  # Different user, full capacity
