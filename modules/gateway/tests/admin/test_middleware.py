"""Unit tests for RequestLoggingMiddleware."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.admin.middleware import RequestLoggingMiddleware, create_request_logging_middleware


class TestRequestLoggingMiddleware:
    """Tests for RequestLoggingMiddleware."""

    @pytest.fixture
    def app_with_middleware(self):
        """Create a test app with the logging middleware."""
        app = FastAPI()

        # Patch the logging to avoid database calls
        with patch("src.admin.middleware.get_session_factory") as mock_factory:
            mock_session = MagicMock()
            mock_session.__aenter__ = AsyncMock(return_value=mock_session)
            mock_session.__aexit__ = AsyncMock()
            mock_session.add = MagicMock()
            mock_session.commit = AsyncMock()
            mock_factory.return_value = MagicMock(return_value=mock_session)

            # Add middleware
            app.add_middleware(RequestLoggingMiddleware)

            @app.get("/test")
            async def test_endpoint():
                return {"status": "ok"}

            @app.get("/health")
            async def health():
                return {"status": "healthy"}

            @app.post("/api/data")
            async def post_data():
                return {"created": True}

            yield app

    def test_middleware_processes_request(self, app_with_middleware):
        """Test that middleware processes requests."""
        client = TestClient(app_with_middleware)

        response = client.get("/test")

        assert response.status_code == 200
        assert "x-request-id" in response.headers

    def test_middleware_skips_excluded_paths(self, app_with_middleware):
        """Test that excluded paths are not logged."""
        client = TestClient(app_with_middleware)

        response = client.get("/health")

        assert response.status_code == 200

    def test_middleware_adds_request_id(self, app_with_middleware):
        """Test that middleware adds request ID to response."""
        client = TestClient(app_with_middleware)

        response = client.get("/test")

        assert "x-request-id" in response.headers
        assert len(response.headers["x-request-id"]) > 0

    def test_middleware_uses_provided_request_id(self, app_with_middleware):
        """Test that middleware uses provided request ID."""
        client = TestClient(app_with_middleware)

        custom_id = "custom-request-id-12345"
        response = client.get("/test", headers={"x-request-id": custom_id})

        assert response.headers["x-request-id"] == custom_id


class TestMiddlewareConfiguration:
    """Tests for middleware configuration."""

    def test_default_excluded_paths(self):
        """Test default excluded paths."""
        assert "/health" in RequestLoggingMiddleware.DEFAULT_EXCLUDED_PATHS
        assert "/ready" in RequestLoggingMiddleware.DEFAULT_EXCLUDED_PATHS
        assert "/metrics" in RequestLoggingMiddleware.DEFAULT_EXCLUDED_PATHS
        assert "/docs" in RequestLoggingMiddleware.DEFAULT_EXCLUDED_PATHS

    def test_default_excluded_headers(self):
        """Test default excluded headers."""
        assert "authorization" in RequestLoggingMiddleware.DEFAULT_EXCLUDED_HEADERS
        assert "x-api-key" in RequestLoggingMiddleware.DEFAULT_EXCLUDED_HEADERS
        assert "cookie" in RequestLoggingMiddleware.DEFAULT_EXCLUDED_HEADERS

    def test_create_configured_middleware(self):
        """Test creating a configured middleware class."""
        configured_middleware = create_request_logging_middleware(
            excluded_paths={"/custom-path"},
            log_bodies=True,
        )

        assert issubclass(configured_middleware, RequestLoggingMiddleware)


class TestMiddlewareShouldSkip:
    """Tests for path skipping logic."""

    @pytest.fixture
    def middleware(self):
        """Create a middleware instance."""
        app = MagicMock()
        return RequestLoggingMiddleware(app)

    def test_skip_exact_match(self, middleware):
        """Test skipping exact path matches."""
        assert middleware._should_skip("/health") is True
        assert middleware._should_skip("/ready") is True
        assert middleware._should_skip("/metrics") is True

    def test_skip_prefix_match(self, middleware):
        """Test skipping prefix matches."""
        assert middleware._should_skip("/docs/swagger") is True
        assert middleware._should_skip("/health/detailed") is True

    def test_no_skip_other_paths(self, middleware):
        """Test other paths are not skipped."""
        assert middleware._should_skip("/api/users") is False
        assert middleware._should_skip("/v1/chat") is False
        assert middleware._should_skip("/admin/orgs") is False


class TestMiddlewareClientIP:
    """Tests for client IP extraction."""

    @pytest.fixture
    def middleware(self):
        """Create a middleware instance."""
        app = MagicMock()
        return RequestLoggingMiddleware(app)

    def test_get_client_ip_from_forwarded_for(self, middleware):
        """Test extracting IP from X-Forwarded-For header."""
        request = MagicMock()
        request.headers = {"x-forwarded-for": "1.2.3.4, 5.6.7.8"}
        request.client = None

        ip = middleware._get_client_ip(request)

        assert ip == "1.2.3.4"

    def test_get_client_ip_from_real_ip(self, middleware):
        """Test extracting IP from X-Real-IP header."""
        request = MagicMock()
        request.headers = {"x-real-ip": "10.0.0.1"}
        request.client = None

        ip = middleware._get_client_ip(request)

        assert ip == "10.0.0.1"

    def test_get_client_ip_from_client(self, middleware):
        """Test extracting IP from request client."""
        request = MagicMock()
        request.headers = {}
        request.client = MagicMock()
        request.client.host = "192.168.1.1"

        ip = middleware._get_client_ip(request)

        assert ip == "192.168.1.1"

    def test_get_client_ip_none(self, middleware):
        """Test when no IP is available."""
        request = MagicMock()
        request.headers = {}
        request.client = None

        ip = middleware._get_client_ip(request)

        assert ip is None


class TestMiddlewareLogging:
    """Tests for the logging functionality."""

    @pytest.fixture
    def middleware(self):
        """Create a middleware instance."""
        app = MagicMock()
        return RequestLoggingMiddleware(app)

    @pytest.mark.asyncio
    async def test_log_request_creates_entry(self, middleware):
        """Test that _log_request creates a log entry."""
        with patch("src.admin.middleware.get_session_factory") as mock_factory:
            mock_session = MagicMock()
            mock_session.__aenter__ = AsyncMock(return_value=mock_session)
            mock_session.__aexit__ = AsyncMock()
            mock_session.add = MagicMock()
            mock_session.commit = AsyncMock()
            mock_factory.return_value = MagicMock(return_value=mock_session)

            await middleware._log_request(
                request_id="test-123",
                user_id="user-001",
                org_id="org-001",
                department_id="dept-001",
                team_id="team-001",
                method="POST",
                path="/v1/chat",
                query_params={"param": "value"},
                status_code=200,
                response_time_ms=150,
                request_body_size=500,
                response_body_size=1000,
                client_ip="1.2.3.4",
                user_agent="TestAgent/1.0",
                error_message=None,
            )

            # Verify add was called
            mock_session.add.assert_called_once()

    @pytest.mark.asyncio
    async def test_log_request_handles_errors(self, middleware):
        """Test that _log_request handles errors gracefully."""
        with patch("src.admin.middleware.get_session_factory") as mock_factory:
            mock_factory.side_effect = Exception("Database error")

            # Should not raise
            await middleware._log_request(
                request_id="test-123",
                user_id="user-001",
                org_id="org-001",
                department_id=None,
                team_id=None,
                method="GET",
                path="/test",
                query_params=None,
                status_code=200,
                response_time_ms=50,
                request_body_size=None,
                response_body_size=None,
                client_ip=None,
                user_agent=None,
                error_message=None,
            )


class TestMiddlewareIntegration:
    """Integration tests for the middleware."""

    def test_full_request_flow(self):
        """Test the full request flow through the middleware."""
        app = FastAPI()

        with patch("src.admin.middleware.get_session_factory") as mock_factory:
            mock_session = MagicMock()
            mock_session.__aenter__ = AsyncMock(return_value=mock_session)
            mock_session.__aexit__ = AsyncMock()
            mock_session.add = MagicMock()
            mock_session.commit = AsyncMock()
            mock_factory.return_value = MagicMock(return_value=mock_session)

            app.add_middleware(RequestLoggingMiddleware)

            @app.post("/api/test")
            async def test_endpoint():
                return {"result": "success"}

            client = TestClient(app)
            response = client.post(
                "/api/test",
                json={"data": "test"},
                headers={
                    "user-agent": "TestClient/1.0",
                    "x-forwarded-for": "1.2.3.4",
                },
            )

            assert response.status_code == 200
            assert "x-request-id" in response.headers
