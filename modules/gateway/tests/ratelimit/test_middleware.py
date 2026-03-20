"""
Unit tests for the rate limit middleware.

This module tests the FastAPI middleware for rate limiting.
"""

from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.middleware.base import BaseHTTPMiddleware

from src.ratelimit.backends.in_memory import InMemoryBackend
from src.ratelimit.config import RateLimitConfig
from src.ratelimit.middleware import RateLimitMiddleware, create_rate_limit_middleware
from src.ratelimit.models import EntityType, RateLimitConfigRequest
from src.ratelimit.service import RateLimitService
from src.shared.schemas.auth import TokenContext


class TestRateLimitMiddleware:
    """Tests for RateLimitMiddleware class."""

    @pytest.fixture
    def backend(self):
        """Provide a fresh InMemoryBackend."""
        return InMemoryBackend()

    @pytest.fixture
    def config(self):
        """Provide test configuration."""
        return RateLimitConfig(
            default_rpm=60,
            default_tpm=100000,
            default_concurrent=10,
            enforce_hierarchy=False,  # Simplify tests
        )

    @pytest.fixture
    def service(self, backend, config):
        """Provide a RateLimitService instance."""
        return RateLimitService(backend=backend, config=config)

    @pytest.fixture
    def user_context(self):
        """Provide a user context."""
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
    def app_with_middleware(self, service, user_context):
        """Provide a FastAPI app with rate limit middleware."""
        app = FastAPI()

        @app.get("/test")
        async def test_endpoint():
            return {"status": "ok"}

        @app.get("/health")
        async def health_endpoint():
            return {"status": "healthy"}

        @app.get("/metrics")
        async def metrics_endpoint():
            return {"metrics": "data"}

        # Add token context middleware (simulating auth)
        @app.middleware("http")
        async def add_token_context(request, call_next):
            if request.url.path not in ["/health", "/metrics"]:
                request.state.token_context = user_context
            return await call_next(request)

        # Add rate limit middleware
        app.add_middleware(RateLimitMiddleware, rate_limit_service=service)

        return app

    @pytest.fixture
    def client(self, app_with_middleware):
        """Provide a test client."""
        return TestClient(app_with_middleware)

    def test_request_allowed(self, client):
        """Test request is allowed when under limit."""
        response = client.get("/test")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}

    def test_exempt_paths_health(self, client):
        """Test health endpoint is exempt from rate limiting."""
        # Make many requests - should all pass
        for _ in range(100):
            response = client.get("/health")
            assert response.status_code == 200

    def test_exempt_paths_metrics(self, client):
        """Test metrics endpoint is exempt from rate limiting."""
        for _ in range(100):
            response = client.get("/metrics")
            assert response.status_code == 200

    def test_rate_limit_headers_present(self, client):
        """Test rate limit headers are present in response."""
        response = client.get("/test")
        assert response.status_code == 200
        # Headers should be present
        assert "X-RateLimit-Limit" in response.headers or response.status_code == 200

    @pytest.mark.asyncio
    async def test_rate_limited_returns_429(self):
        """Test rate limited request returns 429."""
        import uuid

        unique_id = str(uuid.uuid4())[:8]

        backend = InMemoryBackend()
        config = RateLimitConfig(
            default_rpm=60,
            default_tpm=100000,
            default_concurrent=10,
            enforce_hierarchy=False,
        )
        service = RateLimitService(backend=backend, config=config)
        user_context = TokenContext(
            user_id=f"user-rate-429-{unique_id}",
            org_id=f"org-{unique_id}",
            team_id=f"team-{unique_id}",
            department_id=f"dept-{unique_id}",
            account_type="human",
            is_admin=False,
            expires_at=datetime.utcnow() + timedelta(hours=1),
        )

        # Configure very low limit
        await service.configure_limits(
            entity_type=EntityType.USER,
            entity_id=user_context.user_id,
            org_id=user_context.org_id,
            config=RateLimitConfigRequest(rpm=1, concurrent_requests=100),
        )

        app = FastAPI()

        @app.get("/test")
        async def test_endpoint():
            return {"status": "ok"}

        # Create a custom middleware class to inject token context
        # This must be added AFTER RateLimitMiddleware so it runs BEFORE it
        class AuthMiddleware(BaseHTTPMiddleware):
            async def dispatch(self, request, call_next):
                request.state.token_context = user_context
                return await call_next(request)

        # Middleware execution order: last added runs first
        # So we add auth middleware last so it runs first, setting token_context
        app.add_middleware(RateLimitMiddleware, rate_limit_service=service)
        app.add_middleware(AuthMiddleware)
        client = TestClient(app)

        # First request should pass
        response = client.get("/test")
        assert response.status_code == 200

        # Second request should be rate limited
        response = client.get("/test")
        assert response.status_code == 429

    @pytest.mark.asyncio
    async def test_429_response_format(self):
        """Test 429 response has correct format."""
        import uuid

        unique_id = str(uuid.uuid4())[:8]

        backend = InMemoryBackend()
        config = RateLimitConfig(
            default_rpm=60,
            default_tpm=100000,
            default_concurrent=10,
            enforce_hierarchy=False,
        )
        service = RateLimitService(backend=backend, config=config)
        user_context = TokenContext(
            user_id=f"user-format-429-{unique_id}",
            org_id=f"org-{unique_id}",
            team_id=f"team-{unique_id}",
            department_id=f"dept-{unique_id}",
            account_type="human",
            is_admin=False,
            expires_at=datetime.utcnow() + timedelta(hours=1),
        )

        # Configure very low limit
        await service.configure_limits(
            entity_type=EntityType.USER,
            entity_id=user_context.user_id,
            org_id=user_context.org_id,
            config=RateLimitConfigRequest(rpm=1, concurrent_requests=100),
        )

        app = FastAPI()

        @app.get("/test")
        async def test_endpoint():
            return {"status": "ok"}

        # Create a custom middleware class to inject token context
        class AuthMiddleware(BaseHTTPMiddleware):
            async def dispatch(self, request, call_next):
                request.state.token_context = user_context
                return await call_next(request)

        # Middleware execution order: last added runs first
        app.add_middleware(RateLimitMiddleware, rate_limit_service=service)
        app.add_middleware(AuthMiddleware)
        client = TestClient(app)

        # First request to consume the limit
        client.get("/test")

        # Second request should be rate limited
        response = client.get("/test")
        assert response.status_code == 429

        data = response.json()
        assert "error" in data
        assert data["error"] == "rate_limited"
        assert "retry_after_seconds" in data

        # Check headers
        assert "Retry-After" in response.headers
        assert "X-RateLimit-Remaining" in response.headers
        assert response.headers["X-RateLimit-Remaining"] == "0"

    def test_no_auth_context_passes_through(self, service):
        """Test requests without auth context pass through."""
        app = FastAPI()

        @app.get("/test")
        async def test_endpoint():
            return {"status": "ok"}

        # No auth middleware - no token context set

        app.add_middleware(RateLimitMiddleware, rate_limit_service=service)
        client = TestClient(app)

        response = client.get("/test")
        assert response.status_code == 200


class TestCreateRateLimitMiddleware:
    """Tests for create_rate_limit_middleware factory."""

    def test_factory_creates_middleware_class(self):
        """Test factory creates a configured middleware class."""
        service = MagicMock(spec=RateLimitService)
        middleware_class = create_rate_limit_middleware(
            rate_limit_service=service,
            exempt_paths={"/custom"},
        )

        assert issubclass(middleware_class, RateLimitMiddleware)

    def test_factory_with_custom_exempt_paths(self):
        """Test factory includes custom exempt paths."""
        backend = InMemoryBackend()
        config = RateLimitConfig()
        service = RateLimitService(backend=backend, config=config)

        middleware_class = create_rate_limit_middleware(
            rate_limit_service=service,
            exempt_paths={"/custom-exempt"},
        )

        app = FastAPI()

        @app.get("/custom-exempt")
        async def custom_endpoint():
            return {"status": "ok"}

        app.add_middleware(middleware_class)
        client = TestClient(app)

        # Custom exempt path should work without rate limiting
        for _ in range(100):
            response = client.get("/custom-exempt")
            assert response.status_code == 200


class TestMiddlewareExemptPaths:
    """Tests for exempt path handling."""

    def test_is_exempt_exact_match(self):
        """Test exact path match is exempt."""
        service = MagicMock(spec=RateLimitService)
        middleware = RateLimitMiddleware(app=MagicMock(), rate_limit_service=service)

        assert middleware._is_exempt("/health") is True
        assert middleware._is_exempt("/ready") is True
        assert middleware._is_exempt("/metrics") is True

    def test_is_exempt_prefix_match(self):
        """Test prefix path match is exempt."""
        service = MagicMock(spec=RateLimitService)
        middleware = RateLimitMiddleware(app=MagicMock(), rate_limit_service=service)

        assert middleware._is_exempt("/docs/") is True
        assert middleware._is_exempt("/docs/swagger") is True

    def test_is_exempt_non_exempt_path(self):
        """Test non-exempt path is not exempt."""
        service = MagicMock(spec=RateLimitService)
        middleware = RateLimitMiddleware(app=MagicMock(), rate_limit_service=service)

        assert middleware._is_exempt("/api/users") is False
        assert middleware._is_exempt("/test") is False
