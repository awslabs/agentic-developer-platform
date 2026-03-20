"""
Unit tests for the RedisBackend.

This module tests the Redis rate limit backend using fakeredis.
Note: Some Lua script behavior may differ in fakeredis from real Redis.
"""

import pytest

from src.ratelimit.backends.redis import RedisBackend
from src.ratelimit.models import LimitType


class TestRedisBackend:
    """Tests for RedisBackend class."""

    @pytest.fixture
    async def backend(self, fake_redis):
        """Provide a RedisBackend with fakeredis."""
        backend = RedisBackend(
            redis_url="redis://localhost:6379/0",
            key_prefix="test:ratelimit",
            default_ttl=3600,
        )
        # Replace the client with fakeredis
        backend._client = fake_redis

        # Register the Lua scripts with the fake client
        from src.ratelimit.backends.redis import (
            CONCURRENT_INCREMENT_SCRIPT,
            TOKEN_BUCKET_CHECK_SCRIPT,
            TOKEN_BUCKET_CONSUME_SCRIPT,
        )

        backend._consume_script = backend._client.register_script(TOKEN_BUCKET_CONSUME_SCRIPT)
        backend._check_script = backend._client.register_script(TOKEN_BUCKET_CHECK_SCRIPT)
        backend._concurrent_script = backend._client.register_script(CONCURRENT_INCREMENT_SCRIPT)

        yield backend
        await backend.close()

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
    @pytest.mark.xfail(reason="fakeredis doesn't support evalsha/Lua scripts")
    async def test_consume_success(self, backend):
        """Test successful token consumption."""
        success, remaining, retry_after = await backend.consume(
            key="user:new:org:456",
            limit_type=LimitType.RPM,
            max_tokens=100,
            refill_rate=1.0,
            tokens_to_consume=10,
        )
        assert success is True
        assert remaining == 90
        assert retry_after == 0

    @pytest.mark.asyncio
    async def test_get_state_nonexistent(self, backend):
        """Test get_state returns None for nonexistent bucket."""
        state = await backend.get_state(key="nonexistent", limit_type=LimitType.RPM)
        assert state is None

    @pytest.mark.asyncio
    @pytest.mark.xfail(reason="fakeredis doesn't support evalsha/Lua scripts")
    async def test_get_state_existing(self, backend):
        """Test get_state returns state for existing bucket."""
        await backend.consume(
            key="user:state:org:456",
            limit_type=LimitType.RPM,
            max_tokens=100,
            refill_rate=1.0,
            tokens_to_consume=10,
        )

        state = await backend.get_state(key="user:state:org:456", limit_type=LimitType.RPM)
        assert state is not None
        # Allow some tolerance for timing-related variations
        assert 80 <= state.tokens <= 100
        assert state.max_tokens == 100.0
        assert state.limit_type == LimitType.RPM

    @pytest.mark.asyncio
    async def test_reset(self, backend):
        """Test reset removes bucket."""
        await backend.consume(
            key="user:reset:org:456",
            limit_type=LimitType.RPM,
            max_tokens=100,
            refill_rate=1.0,
            tokens_to_consume=80,
        )

        await backend.reset(key="user:reset:org:456", limit_type=LimitType.RPM)

        # After reset, bucket should be gone (new one will be created)
        state = await backend.get_state(key="user:reset:org:456", limit_type=LimitType.RPM)
        assert state is None

    @pytest.mark.asyncio
    async def test_decrement_concurrent_below_zero(self, backend):
        """Test decrementing doesn't go below zero."""
        # Decrement without any prior increment
        current = await backend.decrement_concurrent("user:123")
        assert current == 0

    @pytest.mark.asyncio
    async def test_get_concurrent_count_initial(self, backend):
        """Test getting concurrent count when not set."""
        count = await backend.get_concurrent_count("user:new:123")
        assert count == 0

    @pytest.mark.asyncio
    async def test_close(self, backend):
        """Test closing backend."""
        await backend.close()
        assert backend._client is None

    @pytest.mark.asyncio
    async def test_different_limit_types_isolated(self, backend):
        """Test different limit types are isolated."""
        await backend.consume(
            key="user:iso:org:456",
            limit_type=LimitType.RPM,
            max_tokens=100,
            refill_rate=1.0,
            tokens_to_consume=50,
        )

        # TPM should be independent - new bucket
        remaining = await backend.get_remaining(
            key="user:iso:org:456",
            limit_type=LimitType.TPM,
            max_tokens=10000,
            refill_rate=100.0,
        )
        assert remaining == 10000  # New bucket, full capacity

    @pytest.mark.asyncio
    async def test_different_keys_isolated(self, backend):
        """Test different keys are isolated."""
        await backend.consume(
            key="user:a:org:456",
            limit_type=LimitType.RPM,
            max_tokens=100,
            refill_rate=1.0,
            tokens_to_consume=50,
        )

        remaining = await backend.get_remaining(
            key="user:b:org:789",
            limit_type=LimitType.RPM,
            max_tokens=100,
            refill_rate=1.0,
        )
        assert remaining == 100  # Different user, full capacity

    @pytest.mark.asyncio
    async def test_key_prefix(self, backend):
        """Test keys use the configured prefix."""
        bucket_key = backend._get_bucket_key("user:123", LimitType.RPM)
        assert bucket_key.startswith("test:ratelimit:")

        concurrent_key = backend._get_concurrent_key("user:123")
        assert concurrent_key.startswith("test:ratelimit:")

    @pytest.mark.asyncio
    @pytest.mark.xfail(reason="fakeredis doesn't support evalsha/Lua scripts")
    async def test_consume_multiple_sequential(self, backend):
        """Test multiple sequential consume operations."""
        total_consumed = 0
        for i in range(5):
            success, remaining, _ = await backend.consume(
                key="user:multi:org:456",
                limit_type=LimitType.RPM,
                max_tokens=100,
                refill_rate=0.0,  # No refill
                tokens_to_consume=10,
            )
            assert success is True
            total_consumed += 10

        # Check final remaining
        remaining = await backend.get_remaining(
            key="user:multi:org:456",
            limit_type=LimitType.RPM,
            max_tokens=100,
            refill_rate=0.0,
        )
        assert remaining == 50  # 100 - 50 consumed

    @pytest.mark.asyncio
    @pytest.mark.xfail(reason="fakeredis doesn't support evalsha/Lua scripts")
    async def test_concurrent_increment_basic(self, backend):
        """Test basic concurrent increment."""
        await backend.set_concurrent_limit("user:conc:123", 10)

        success, current, limit = await backend.increment_concurrent("user:conc:123")
        assert success is True
        assert current == 1
        assert limit == 10

    @pytest.mark.asyncio
    @pytest.mark.xfail(reason="fakeredis doesn't support evalsha/Lua scripts")
    async def test_concurrent_decrement_basic(self, backend):
        """Test basic concurrent decrement after increment."""
        await backend.set_concurrent_limit("user:dec:123", 10)
        await backend.increment_concurrent("user:dec:123")
        await backend.increment_concurrent("user:dec:123")

        current = await backend.decrement_concurrent("user:dec:123")
        assert current == 1

    @pytest.mark.asyncio
    async def test_set_concurrent_limit_basic(self, backend):
        """Test setting concurrent limit."""
        await backend.set_concurrent_limit("user:limit:123", 5)

        # Verify limit is stored
        limit_key = backend._get_concurrent_limit_key("user:limit:123")
        stored_limit = await backend._client.get(limit_key)
        assert stored_limit == "5"
