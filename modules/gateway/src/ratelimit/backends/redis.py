"""
Redis rate limit backend.

This module provides a Redis-based implementation of the rate limit backend
using Lua scripts for atomic token bucket operations.
"""

import logging
import time
from datetime import datetime

import redis.asyncio as redis

from ..backend import RateLimitBackend
from ..models import EntityType, LimitType, RateLimitState

logger = logging.getLogger(__name__)

# Lua script for atomic token bucket consume operation
TOKEN_BUCKET_CONSUME_SCRIPT = """
local key = KEYS[1]
local now = tonumber(ARGV[1])
local max_tokens = tonumber(ARGV[2])
local refill_rate = tonumber(ARGV[3])
local tokens_to_consume = tonumber(ARGV[4])
local ttl = tonumber(ARGV[5])

-- Get current state or initialize
local state = redis.call('HMGET', key, 'tokens', 'last_refill')
local tokens = tonumber(state[1]) or max_tokens
local last_refill = tonumber(state[2]) or now

-- Calculate tokens to add based on elapsed time
local elapsed = now - last_refill
if elapsed > 0 and refill_rate > 0 then
    tokens = math.min(max_tokens, tokens + elapsed * refill_rate)
end

-- Try to consume tokens
local success = 0
local remaining = tokens
local wait_time = 0

if tokens >= tokens_to_consume then
    tokens = tokens - tokens_to_consume
    remaining = tokens
    success = 1
else
    -- Calculate wait time
    local tokens_needed = tokens_to_consume - tokens
    if refill_rate > 0 then
        wait_time = math.ceil(tokens_needed / refill_rate)
    else
        wait_time = 60
    end
end

-- Save state
redis.call('HMSET', key, 'tokens', tokens, 'last_refill', now, 'max_tokens', max_tokens, 'refill_rate', refill_rate)
redis.call('EXPIRE', key, ttl)

return {success, math.floor(remaining), wait_time}
"""

# Lua script for checking without consuming
TOKEN_BUCKET_CHECK_SCRIPT = """
local key = KEYS[1]
local now = tonumber(ARGV[1])
local max_tokens = tonumber(ARGV[2])
local refill_rate = tonumber(ARGV[3])
local tokens_to_check = tonumber(ARGV[4])

-- Get current state or initialize
local state = redis.call('HMGET', key, 'tokens', 'last_refill')
local tokens = tonumber(state[1]) or max_tokens
local last_refill = tonumber(state[2]) or now

-- Calculate tokens to add based on elapsed time
local elapsed = now - last_refill
if elapsed > 0 and refill_rate > 0 then
    tokens = math.min(max_tokens, tokens + elapsed * refill_rate)
end

-- Check if tokens available
local allowed = 0
local wait_time = 0

if tokens >= tokens_to_check then
    allowed = 1
else
    -- Calculate wait time
    local tokens_needed = tokens_to_check - tokens
    if refill_rate > 0 then
        wait_time = math.ceil(tokens_needed / refill_rate)
    else
        wait_time = 60
    end
end

return {allowed, math.floor(tokens), wait_time}
"""

# Lua script for atomic concurrent increment
CONCURRENT_INCREMENT_SCRIPT = """
local key = KEYS[1]
local limit_key = KEYS[2]
local ttl = tonumber(ARGV[1])

local limit = tonumber(redis.call('GET', limit_key)) or 100
local current = tonumber(redis.call('GET', key)) or 0

if current >= limit then
    return {0, current, limit}
end

current = redis.call('INCR', key)
redis.call('EXPIRE', key, ttl)

return {1, current, limit}
"""


class RedisBackend(RateLimitBackend):
    """
    Redis-based rate limit backend.

    This implementation uses Redis for distributed rate limiting,
    suitable for multi-instance deployments. It uses Lua scripts
    for atomic operations to ensure consistency.

    Features:
    - Atomic token bucket operations via Lua scripts
    - Connection pooling
    - Graceful connection failure handling
    - Configurable key TTL
    """

    def __init__(
        self,
        redis_url: str = "redis://localhost:6379/0",
        key_prefix: str = "ratelimit",
        default_ttl: int = 3600,
    ) -> None:
        """
        Initialize the Redis backend.

        Args:
            redis_url: Redis connection URL
            key_prefix: Prefix for all Redis keys
            default_ttl: Default TTL for keys in seconds
        """
        self._redis_url = redis_url
        self._key_prefix = key_prefix
        self._default_ttl = default_ttl
        self._client: redis.Redis | None = None
        self._consume_script: redis.client.Script | None = None
        self._check_script: redis.client.Script | None = None
        self._concurrent_script: redis.client.Script | None = None

    async def _get_client(self) -> redis.Redis:
        """Get or create Redis client."""
        if self._client is None:
            self._client = redis.from_url(
                self._redis_url,
                encoding="utf-8",
                decode_responses=True,
            )
            # Register Lua scripts
            self._consume_script = self._client.register_script(TOKEN_BUCKET_CONSUME_SCRIPT)
            self._check_script = self._client.register_script(TOKEN_BUCKET_CHECK_SCRIPT)
            self._concurrent_script = self._client.register_script(CONCURRENT_INCREMENT_SCRIPT)
        return self._client

    def _get_bucket_key(self, key: str, limit_type: LimitType) -> str:
        """Generate a Redis key for a bucket."""
        return f"{self._key_prefix}:bucket:{key}:{limit_type.value}"

    def _get_concurrent_key(self, key: str) -> str:
        """Generate a Redis key for concurrent counter."""
        return f"{self._key_prefix}:concurrent:{key}"

    def _get_concurrent_limit_key(self, key: str) -> str:
        """Generate a Redis key for concurrent limit."""
        return f"{self._key_prefix}:concurrent_limit:{key}"

    async def check_limit(
        self,
        key: str,
        limit_type: LimitType,
        max_tokens: int,
        refill_rate: float,
    ) -> tuple[bool, int, int]:
        """Check if a rate limit would be exceeded without consuming."""
        try:
            client = await self._get_client()
            bucket_key = self._get_bucket_key(key, limit_type)
            now = time.time()

            result = await self._check_script(  # type: ignore
                keys=[bucket_key],
                args=[now, max_tokens, refill_rate, 1],
                client=client,
            )

            allowed = bool(result[0])
            remaining = int(result[1])
            wait_time = int(result[2])

            return allowed, remaining, wait_time
        except redis.RedisError as e:
            logger.error(f"Redis error in check_limit: {e}")
            # Fail open - allow the request
            return True, max_tokens, 0

    async def consume(
        self,
        key: str,
        limit_type: LimitType,
        max_tokens: int,
        refill_rate: float,
        tokens_to_consume: int = 1,
    ) -> tuple[bool, int, int]:
        """Attempt to consume tokens from a rate limit bucket."""
        try:
            client = await self._get_client()
            bucket_key = self._get_bucket_key(key, limit_type)
            now = time.time()

            result = await self._consume_script(  # type: ignore
                keys=[bucket_key],
                args=[now, max_tokens, refill_rate, tokens_to_consume, self._default_ttl],
                client=client,
            )

            success = bool(result[0])
            remaining = int(result[1])
            wait_time = int(result[2])

            return success, remaining, wait_time
        except redis.RedisError as e:
            logger.error(f"Redis error in consume: {e}")
            # Fail open - allow the request
            return True, max_tokens, 0

    async def get_remaining(
        self,
        key: str,
        limit_type: LimitType,
        max_tokens: int,
        refill_rate: float,
    ) -> int:
        """Get the number of remaining tokens."""
        _, remaining, _ = await self.check_limit(key, limit_type, max_tokens, refill_rate)
        return remaining

    async def get_state(
        self,
        key: str,
        limit_type: LimitType,
    ) -> RateLimitState | None:
        """Get the current state of a rate limit bucket."""
        try:
            client = await self._get_client()
            bucket_key = self._get_bucket_key(key, limit_type)

            state = await client.hgetall(bucket_key)
            if not state:
                return None

            # Parse entity info from key
            parts = key.split(":")
            entity_type_str = parts[0] if len(parts) > 0 else "user"
            entity_id = parts[1] if len(parts) > 1 else key

            try:
                entity_type = EntityType(entity_type_str)
            except ValueError:
                entity_type = EntityType.USER

            return RateLimitState(
                entity_type=entity_type,
                entity_id=entity_id,
                limit_type=limit_type,
                tokens=float(state.get("tokens", 0)),
                max_tokens=float(state.get("max_tokens", 1)),
                refill_rate=float(state.get("refill_rate", 0)),
                last_refill=datetime.utcnow(),
            )
        except redis.RedisError as e:
            logger.error(f"Redis error in get_state: {e}")
            return None

    async def reset(self, key: str, limit_type: LimitType) -> None:
        """Reset a rate limit bucket to full capacity."""
        try:
            client = await self._get_client()
            bucket_key = self._get_bucket_key(key, limit_type)
            await client.delete(bucket_key)
        except redis.RedisError as e:
            logger.error(f"Redis error in reset: {e}")

    async def increment_concurrent(self, key: str) -> tuple[bool, int, int]:
        """Increment concurrent request counter."""
        try:
            client = await self._get_client()
            concurrent_key = self._get_concurrent_key(key)
            limit_key = self._get_concurrent_limit_key(key)

            result = await self._concurrent_script(  # type: ignore
                keys=[concurrent_key, limit_key],
                args=[self._default_ttl],
                client=client,
            )

            success = bool(result[0])
            current = int(result[1])
            limit = int(result[2])

            return success, current, limit
        except redis.RedisError as e:
            logger.error(f"Redis error in increment_concurrent: {e}")
            # Fail open
            return True, 0, 100

    async def decrement_concurrent(self, key: str) -> int:
        """Decrement concurrent request counter."""
        try:
            client = await self._get_client()
            concurrent_key = self._get_concurrent_key(key)

            current = await client.decr(concurrent_key)
            # Ensure non-negative
            if current < 0:
                await client.set(concurrent_key, 0)
                return 0
            return current
        except redis.RedisError as e:
            logger.error(f"Redis error in decrement_concurrent: {e}")
            return 0

    async def get_concurrent_count(self, key: str) -> int:
        """Get current concurrent request count."""
        try:
            client = await self._get_client()
            concurrent_key = self._get_concurrent_key(key)

            value = await client.get(concurrent_key)
            return int(value) if value else 0
        except redis.RedisError as e:
            logger.error(f"Redis error in get_concurrent_count: {e}")
            return 0

    async def set_concurrent_limit(self, key: str, limit: int) -> None:
        """Set the concurrent request limit for a key."""
        try:
            client = await self._get_client()
            limit_key = self._get_concurrent_limit_key(key)
            await client.set(limit_key, limit, ex=self._default_ttl)
        except redis.RedisError as e:
            logger.error(f"Redis error in set_concurrent_limit: {e}")

    async def close(self) -> None:
        """Close and cleanup backend resources."""
        if self._client:
            await self._client.aclose()
            self._client = None
