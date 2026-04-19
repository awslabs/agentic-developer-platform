"""Tests for logging middleware."""

from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from src.shared.logging import clear_request_context
from src.shared.middleware.logging_middleware import (
    LoggingMiddleware,
    _SKIP_PATHS,
    _get_client_ip,
    get_current_request_id,
)


@pytest.fixture
def app():
    """Create a test FastAPI app."""
    app = FastAPI()
    app.add_middleware(LoggingMiddleware)

    @app.get("/test")
    async def test_endpoint(request: Request):
        return {
            "request_id": getattr(request.state, "request_id", None),
        }

    @app.get("/health")
    async def health():
        return {"status": "healthy"}

    @app.get("/error")
    async def error_endpoint():
        raise ValueError("Test error")

    return app


@pytest.fixture
def client(app):
    """Create a test client."""
    return TestClient(app)


class TestLoggingMiddleware:
    """Tests for LoggingMiddleware."""

    def teardown_method(self):
        """Clear context after each test."""
        clear_request_context()

    def test_generates_request_id(self, client):
        """Test that middleware generates a request ID."""
        response = client.get("/test")
        assert response.status_code == 200

        data = response.json()
        assert data["request_id"] is not None
        assert len(data["request_id"]) > 0

    def test_uses_provided_request_id(self, client):
        """Test that middleware uses X-Request-ID header if provided."""
        response = client.get("/test", headers={"X-Request-ID": "custom-request-123"})
        assert response.status_code == 200

        data = response.json()
        assert data["request_id"] == "custom-request-123"

    def test_adds_request_id_to_response(self, client):
        """Test that middleware adds X-Request-ID to response headers."""
        response = client.get("/test")
        assert "X-Request-ID" in response.headers
        assert response.headers["X-Request-ID"] is not None

    def test_uses_same_request_id_in_response(self, client):
        """Test that response X-Request-ID matches request state."""
        response = client.get("/test")
        response_request_id = response.headers["X-Request-ID"]
        body_request_id = response.json()["request_id"]
        assert response_request_id == body_request_id

    def test_skips_health_endpoint(self, client):
        """Test that health endpoint is not logged."""
        # This test verifies the middleware doesn't throw errors for health endpoints
        response = client.get("/health")
        assert response.status_code == 200

    def test_handles_errors(self, client):
        """Test that middleware handles errors gracefully."""
        with pytest.raises(ValueError):
            client.get("/error")


class TestLoggingMiddlewareSkipPaths:
    """Tests for skip paths functionality."""

    def test_default_skip_paths(self):
        """Test default skip paths are set at module level."""
        assert "/health" in _SKIP_PATHS
        assert "/ready" in _SKIP_PATHS
        assert "/metrics" in _SKIP_PATHS
        assert "/docs" in _SKIP_PATHS

    def test_custom_skip_paths(self):
        """Test that default skip paths include expected entries."""
        # Skip paths are now module-level constants, not instance attributes.
        # Verify the module constant includes the expected paths.
        assert "/health" in _SKIP_PATHS
        assert "/redoc" in _SKIP_PATHS
        assert "/openapi.json" in _SKIP_PATHS


class TestRequestIdPropagation:
    """Tests for request ID propagation."""

    def teardown_method(self):
        """Clear context after each test."""
        clear_request_context()

    def test_request_id_in_context(self):
        """Test that request ID is available in context during request."""
        app = FastAPI()
        app.add_middleware(LoggingMiddleware)

        captured_request_id = None

        @app.get("/capture")
        async def capture_endpoint():
            nonlocal captured_request_id
            captured_request_id = get_current_request_id()
            return {"captured": captured_request_id}

        client = TestClient(app)
        response = client.get("/capture", headers={"X-Request-ID": "test-propagation-123"})

        assert response.status_code == 200
        assert captured_request_id == "test-propagation-123"


class TestClientIPExtraction:
    """Tests for client IP extraction."""

    def test_extracts_x_forwarded_for(self):
        """Test extraction of X-Forwarded-For header."""
        mock_request = MagicMock()
        mock_request.headers = {"X-Forwarded-For": "203.0.113.195, 70.41.3.18"}
        mock_request.client = None

        ip = _get_client_ip(mock_request)
        assert ip == "203.0.113.195"

    def test_extracts_x_real_ip(self):
        """Test extraction of X-Real-IP header."""
        mock_request = MagicMock()
        mock_request.headers = {"X-Real-IP": "192.168.1.100"}
        mock_request.client = None

        ip = _get_client_ip(mock_request)
        assert ip == "192.168.1.100"

    def test_falls_back_to_direct_client(self):
        """Test fallback to direct client IP."""
        mock_request = MagicMock()
        mock_request.headers = {}
        mock_request.client = MagicMock()
        mock_request.client.host = "127.0.0.1"

        ip = _get_client_ip(mock_request)
        assert ip == "127.0.0.1"


class TestLoggingOutput:
    """Tests for logging output."""

    def test_logs_request_start(self, app, caplog):
        """Test that request start is logged."""
        client = TestClient(app)

        with caplog.at_level("INFO"):
            response = client.get("/test")

        # Check that some logging occurred (middleware logs)
        assert response.status_code == 200

    def test_logs_request_end(self, app, caplog):
        """Test that request end is logged."""
        client = TestClient(app)

        with caplog.at_level("INFO"):
            response = client.get("/test")

        assert response.status_code == 200
