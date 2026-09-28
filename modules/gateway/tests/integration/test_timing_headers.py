"""
Integration tests for X-Gateway-Timing response headers.

Issue #144: Phase 1 - Verify timing headers appear on proxy responses
and contain all expected segments with reasonable values.
"""

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from src.shared.middleware.logging_middleware import LoggingMiddleware
from src.shared.timing import get_timings


@pytest.fixture
def timing_app():
    """Create a test FastAPI app with LoggingMiddleware for timing tests."""
    app = FastAPI()
    app.add_middleware(LoggingMiddleware)

    @app.get("/test")
    async def test_endpoint(request: Request):
        """Normal endpoint that records some timings."""
        timings = get_timings(request)
        timings.record("auth", 5.0)
        timings.record("model_resolve", 1.0)
        timings.record("budget_check", 12.0)
        timings.record("ratelimit_check", 3.0)
        timings.record("bedrock", 1847.0)
        timings.record("serialize", 2.0)
        return {"status": "ok"}

    @app.get("/health")
    async def health():
        """Health check endpoint (should still get timing header)."""
        return {"status": "healthy"}

    @app.get("/no-timing")
    async def no_timing_endpoint():
        """Endpoint that doesn't record any segment timings."""
        return {"status": "ok"}

    @app.get("/partial-timing")
    async def partial_timing(request: Request):
        """Endpoint that only records some timings."""
        timings = get_timings(request)
        timings.record("auth", 3.0)
        return {"status": "ok"}

    return app


@pytest.fixture
def timing_client(timing_app):
    """Create a test client."""
    return TestClient(timing_app)


class TestTimingHeaderPresence:
    """Tests for X-Gateway-Timing header presence."""

    def test_timing_header_present_on_response(self, timing_client):
        """Test that X-Gateway-Timing header appears on responses."""
        response = timing_client.get("/test")
        assert response.status_code == 200
        assert "X-Gateway-Timing" in response.headers

    def test_timing_header_has_total(self, timing_client):
        """Test that timing header always includes total."""
        response = timing_client.get("/test")
        header = response.headers.get("X-Gateway-Timing", "")
        assert "total=" in header

    def test_timing_header_on_endpoint_without_segments(self, timing_client):
        """Test that timing header appears even without segment recordings."""
        response = timing_client.get("/no-timing")
        assert response.status_code == 200
        # Should still have timing header with at least 'total'
        header = response.headers.get("X-Gateway-Timing", "")
        assert "total=" in header

    def test_timing_header_on_health_endpoint(self, timing_client):
        """Test that health endpoints still get timing header."""
        response = timing_client.get("/health")
        assert response.status_code == 200
        # Health endpoints are skipped for logging but still get timing
        header = response.headers.get("X-Gateway-Timing", "")
        assert "total=" in header


class TestTimingHeaderContent:
    """Tests for X-Gateway-Timing header content."""

    def test_all_segments_present(self, timing_client):
        """Test that all expected segments appear in the header."""
        response = timing_client.get("/test")
        header = response.headers.get("X-Gateway-Timing", "")

        expected_segments = [
            "auth",
            "model_resolve",
            "budget_check",
            "ratelimit_check",
            "bedrock",
            "serialize",
            "total",
        ]

        for segment in expected_segments:
            assert f"{segment}=" in header, f"Missing segment: {segment}"

    def test_header_format_is_correct(self, timing_client):
        """Test that header follows the format: name=Nms;name=Nms."""
        response = timing_client.get("/test")
        header = response.headers.get("X-Gateway-Timing", "")

        parts = header.split(";")
        for part in parts:
            # Each part should be name=Nms
            assert "=" in part, f"Invalid part format: {part}"
            name, value = part.split("=")
            assert name.strip(), f"Empty segment name in: {part}"
            assert value.endswith("ms"), f"Value doesn't end with 'ms': {part}"
            # The numeric part should be parseable
            numeric_str = value[:-2]  # Remove 'ms'
            float(numeric_str)  # Should not raise

    def test_timing_values_are_numeric(self, timing_client):
        """Test that timing values are valid numbers."""
        response = timing_client.get("/test")
        header = response.headers.get("X-Gateway-Timing", "")

        parts = header.split(";")
        for part in parts:
            name, value = part.split("=")
            numeric_str = value[:-2]  # Remove 'ms'
            numeric_val = float(numeric_str)
            assert numeric_val >= 0, f"Negative timing value for {name}: {numeric_val}"

    def test_total_is_reasonable(self, timing_client):
        """Test that total timing is non-negative and reasonable."""
        response = timing_client.get("/test")
        header = response.headers.get("X-Gateway-Timing", "")

        # Parse total
        for part in header.split(";"):
            if part.startswith("total="):
                total_ms = float(part.split("=")[1].replace("ms", ""))
                assert total_ms >= 0
                assert total_ms < 30000  # Should complete in < 30 seconds
                break
        else:
            pytest.fail("No 'total' segment found in timing header")

    def test_partial_timing_header(self, timing_client):
        """Test that partial timing still produces valid header."""
        response = timing_client.get("/partial-timing")
        header = response.headers.get("X-Gateway-Timing", "")

        assert "auth=3ms" in header
        assert "total=" in header

    def test_recorded_values_match(self, timing_client):
        """Test that recorded segment values appear correctly."""
        response = timing_client.get("/test")
        header = response.headers.get("X-Gateway-Timing", "")

        assert "auth=5ms" in header
        assert "model_resolve=1ms" in header
        assert "budget_check=12ms" in header
        assert "ratelimit_check=3ms" in header
        assert "bedrock=1847ms" in header
        assert "serialize=2ms" in header


class TestRequestIdAndTimingCoexistence:
    """Tests that timing headers coexist with X-Request-ID."""

    def test_both_headers_present(self, timing_client):
        """Test that both X-Request-ID and X-Gateway-Timing are set."""
        response = timing_client.get("/test")
        assert "X-Request-ID" in response.headers
        assert "X-Gateway-Timing" in response.headers

    def test_custom_request_id_with_timing(self, timing_client):
        """Test that custom X-Request-ID works alongside timing."""
        response = timing_client.get("/test", headers={"X-Request-ID": "custom-123"})
        assert response.headers["X-Request-ID"] == "custom-123"
        assert "X-Gateway-Timing" in response.headers


class TestTimingInitialization:
    """Tests for timing initialization in middleware."""

    def test_timings_initialized_before_handlers(self):
        """Test that request.state.timings is initialized before handlers run."""
        app = FastAPI()
        app.add_middleware(LoggingMiddleware)

        timings_present = False

        @app.get("/check-init")
        async def check_init(request: Request):
            nonlocal timings_present
            timings_present = hasattr(request.state, "timings")
            return {"ok": True}

        client = TestClient(app)
        response = client.get("/check-init")
        assert response.status_code == 200
        assert timings_present, "request.state.timings was not initialized before handler"
