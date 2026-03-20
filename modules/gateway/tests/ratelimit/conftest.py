"""
Pytest fixtures for rate limit testing.

This module provides shared fixtures for testing the rate limiting module.
"""

from datetime import datetime, timedelta
from unittest.mock import AsyncMock

import fakeredis.aioredis
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.testclient import TestClient as StarletteTestClient

from src.ratelimit.backends.in_memory import InMemoryBackend
from src.ratelimit.backends.redis import RedisBackend
from src.ratelimit.config import RateLimitConfig
from src.ratelimit.middleware import RateLimitMiddleware
from src.ratelimit.models import RateLimitConfigRequest
from src.ratelimit.routes import router, set_rate_limit_service
from src.ratelimit.service import RateLimitService
from src.shared.schemas.auth import TokenContext


@pytest.fixture
def rate_limit_config() -> RateLimitConfig:
    """Provide a test rate limit configuration."""
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
        cleanup_interval_seconds=60,
        entry_ttl_seconds=3600,
        enforce_hierarchy=True,
    )


@pytest.fixture
def in_memory_backend() -> InMemoryBackend:
    """Provide a fresh in-memory backend instance."""
    backend = InMemoryBackend(cleanup_interval=60.0, entry_ttl=3600.0)
    yield backend
    # Cleanup
    backend.clear()


@pytest.fixture
async def redis_backend(fake_redis) -> RedisBackend:
    """Provide a Redis backend with fakeredis."""
    backend = RedisBackend(
        redis_url="redis://localhost:6379/0",
        key_prefix="test:ratelimit",
        default_ttl=3600,
    )
    # Replace the client with fakeredis
    backend._client = fake_redis
    # Register Lua scripts
    backend._consume_script = backend._client.register_script(
        """
        local key = KEYS[1]
        local now = tonumber(ARGV[1])
        local max_tokens = tonumber(ARGV[2])
        local refill_rate = tonumber(ARGV[3])
        local tokens_to_consume = tonumber(ARGV[4])
        local ttl = tonumber(ARGV[5])

        local state = redis.call('HMGET', key, 'tokens', 'last_refill')
        local tokens = tonumber(state[1]) or max_tokens
        local last_refill = tonumber(state[2]) or now

        local elapsed = now - last_refill
        if elapsed > 0 and refill_rate > 0 then
            tokens = math.min(max_tokens, tokens + elapsed * refill_rate)
        end

        local success = 0
        local remaining = tokens
        local wait_time = 0

        if tokens >= tokens_to_consume then
            tokens = tokens - tokens_to_consume
            remaining = tokens
            success = 1
        else
            local tokens_needed = tokens_to_consume - tokens
            if refill_rate > 0 then
                wait_time = math.ceil(tokens_needed / refill_rate)
            else
                wait_time = 60
            end
        end

        redis.call('HMSET', key, 'tokens', tokens, 'last_refill', now, 'max_tokens', max_tokens, 'refill_rate', refill_rate)
        redis.call('EXPIRE', key, ttl)

        return {success, math.floor(remaining), wait_time}
        """
    )
    backend._check_script = backend._client.register_script(
        """
        local key = KEYS[1]
        local now = tonumber(ARGV[1])
        local max_tokens = tonumber(ARGV[2])
        local refill_rate = tonumber(ARGV[3])
        local tokens_to_check = tonumber(ARGV[4])

        local state = redis.call('HMGET', key, 'tokens', 'last_refill')
        local tokens = tonumber(state[1]) or max_tokens
        local last_refill = tonumber(state[2]) or now

        local elapsed = now - last_refill
        if elapsed > 0 and refill_rate > 0 then
            tokens = math.min(max_tokens, tokens + elapsed * refill_rate)
        end

        local allowed = 0
        local wait_time = 0

        if tokens >= tokens_to_check then
            allowed = 1
        else
            local tokens_needed = tokens_to_check - tokens
            if refill_rate > 0 then
                wait_time = math.ceil(tokens_needed / refill_rate)
            else
                wait_time = 60
            end
        end

        return {allowed, math.floor(tokens), wait_time}
        """
    )
    backend._concurrent_script = backend._client.register_script(
        """
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
    )
    yield backend
    await backend.close()


@pytest.fixture
def fake_redis():
    """Provide a fakeredis instance for testing."""
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


@pytest.fixture
def rate_limit_service(in_memory_backend, rate_limit_config) -> RateLimitService:
    """Provide a rate limit service with in-memory backend."""
    return RateLimitService(backend=in_memory_backend, config=rate_limit_config)


@pytest.fixture
def human_user_context() -> TokenContext:
    """Provide a token context for a human user."""
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
def service_account_context() -> TokenContext:
    """Provide a token context for a service account."""
    return TokenContext(
        user_id="sa-123",
        org_id="org-456",
        team_id="team-789",
        department_id="dept-012",
        account_type="service",
        is_admin=False,
        expires_at=datetime.utcnow() + timedelta(hours=1),
    )


@pytest.fixture
def admin_context() -> TokenContext:
    """Provide a token context for an admin user."""
    return TokenContext(
        user_id="admin-123",
        org_id="org-456",
        team_id="team-789",
        department_id="dept-012",
        account_type="human",
        is_admin=True,
        expires_at=datetime.utcnow() + timedelta(hours=1),
    )


@pytest.fixture
def sample_rate_limit_config() -> RateLimitConfigRequest:
    """Provide a sample rate limit configuration request."""
    return RateLimitConfigRequest(
        rpm=100,
        tpm=50000,
        concurrent_requests=5,
        burst_size=150,
    )


@pytest.fixture
def test_app(rate_limit_service, admin_context) -> FastAPI:
    """Provide a FastAPI test application with rate limit routes.

    Issue #133: Updated to mock the new get_current_user dependency instead
    of relying on middleware to set request.state.token_context.
    """
    from src.auth.dependencies import get_current_user

    app = FastAPI()
    app.include_router(router)

    # Set the rate limit service
    set_rate_limit_service(rate_limit_service)

    # Issue #133: Override get_current_user dependency to return admin context
    # This replaces the old middleware approach that set request.state.token_context
    async def override_get_current_user():
        return admin_context

    app.dependency_overrides[get_current_user] = override_get_current_user

    return app


@pytest.fixture
def test_client(test_app) -> StarletteTestClient:
    """Provide a test client for the FastAPI application."""
    return TestClient(test_app)


@pytest.fixture
def app_with_middleware(rate_limit_service, human_user_context) -> FastAPI:
    """Provide a FastAPI app with rate limit middleware."""
    app = FastAPI()

    @app.get("/test")
    async def test_endpoint():
        return {"status": "ok"}

    @app.get("/health")
    async def health_endpoint():
        return {"status": "healthy"}

    # Add token context middleware (simulating auth)
    @app.middleware("http")
    async def add_token_context(request, call_next):
        if request.url.path != "/health":  # Skip for health endpoint
            request.state.token_context = human_user_context
        return await call_next(request)

    # Add rate limit middleware
    app.add_middleware(RateLimitMiddleware, rate_limit_service=rate_limit_service)

    return app


@pytest.fixture
def middleware_test_client(app_with_middleware) -> StarletteTestClient:
    """Provide a test client for middleware testing."""
    return TestClient(app_with_middleware)


@pytest.fixture
def mock_backend() -> AsyncMock:
    """Provide a mock rate limit backend."""
    backend = AsyncMock()
    backend.check_limit.return_value = (True, 100, 0)
    backend.consume.return_value = (True, 99, 0)
    backend.get_remaining.return_value = 100
    backend.get_state.return_value = None
    backend.reset.return_value = None
    backend.increment_concurrent.return_value = (True, 1, 10)
    backend.decrement_concurrent.return_value = 0
    backend.get_concurrent_count.return_value = 0
    backend.set_concurrent_limit.return_value = None
    backend.close.return_value = None
    return backend
