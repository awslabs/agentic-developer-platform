"""
Integration tests for OpenTelemetry/X-Ray distributed tracing.

Issue #144: Phase 2 - Verify tracing initialization, custom spans,
trace context propagation, and minimal overhead.
"""

import os
from unittest.mock import MagicMock, patch

import pytest

from src.shared.tracing import (
    _NoOpSpan,
    _NoOpTracer,
    get_tracer,
    setup_tracing,
    shutdown_tracing,
)


class TestTracingDisabled:
    """Tests for tracing when disabled (default behavior)."""

    def test_setup_tracing_returns_false_when_disabled(self):
        """Test that setup_tracing returns False when OTEL_ENABLED is not set."""
        app = MagicMock()
        with patch.dict(os.environ, {"OTEL_ENABLED": "false"}, clear=False):
            result = setup_tracing(app)
        assert result is False

    def test_get_tracer_returns_noop_when_disabled(self):
        """Test that get_tracer returns a no-op tracer when OTel is not available."""
        tracer = get_tracer("test_module")
        assert isinstance(tracer, _NoOpTracer)

    def test_noop_tracer_start_as_current_span(self):
        """Test that no-op tracer's context manager works without errors."""
        tracer = _NoOpTracer()
        with tracer.start_as_current_span("test_span") as span:
            span.set_attribute("key", "value")
            span.add_event("test_event")
        # Should not raise any exceptions

    def test_shutdown_tracing_when_not_initialized(self):
        """Test that shutdown_tracing is safe when tracing was never initialized."""
        shutdown_tracing()  # Should not raise


class TestTracingConfiguration:
    """Tests for tracing configuration."""

    def test_otel_enabled_requires_packages(self):
        """Test that enabling OTEL without packages returns False."""
        app = MagicMock()

        with patch.dict(os.environ, {"OTEL_ENABLED": "true"}, clear=False):
            with patch("src.shared.tracing._is_otel_available", return_value=False):
                result = setup_tracing(app)
        assert result is False

    def test_otel_enabled_values(self):
        """Test various OTEL_ENABLED values."""
        app = MagicMock()

        # These should return False (disabled)
        for value in ["false", "0", "no", "", "FALSE"]:
            with patch.dict(os.environ, {"OTEL_ENABLED": value}, clear=False):
                # Reset initialization state
                import src.shared.tracing as tracing_mod

                tracing_mod._tracing_initialized = False
                result = setup_tracing(app)
                assert result is False, f"Expected False for OTEL_ENABLED={value!r}"

    def test_tracing_env_vars_read_correctly(self):
        """Test that configuration is read from environment variables."""
        # Verify the config module picks up tracing settings
        with patch.dict(
            os.environ,
            {
                "BG_OTEL_ENABLED": "false",
                "BG_OTEL_SERVICE_NAME": "test-service",
                "BG_OTEL_EXPORTER_ENDPOINT": "http://test:4317",
            },
            clear=False,
        ):
            from src.shared.config import Settings

            settings = Settings()
            assert settings.otel_enabled is False
            assert settings.otel_service_name == "test-service"
            assert settings.otel_exporter_endpoint == "http://test:4317"


class TestTracingOverhead:
    """Tests for tracing overhead."""

    def test_noop_tracer_cpu_overhead(self):
        """The disabled tracer stays a thin wrapper around its no-op span."""
        from statistics import median
        from time import thread_time

        tracer = _NoOpTracer()
        iterations = 10000

        def cpu_cost(start_span):
            start = thread_time()
            for _ in range(iterations):
                with start_span("test") as span:
                    span.set_attribute("key", "value")
            return thread_time() - start

        # Compare the same span lifecycle on the same interpreter. A fixed wall
        # deadline measures shared-runner contention and coverage instrumentation
        # as well as tracing. Thread CPU time excludes descheduling; alternating
        # samples and their medians reduce warmup noise without removing the
        # regression check for excess work in the disabled tracer.
        direct, wrapped = [], []
        for _ in range(5):
            direct.append(cpu_cost(lambda name: _NoOpSpan()))
            wrapped.append(cpu_cost(tracer.start_as_current_span))
        ratio = median(wrapped) / median(direct)
        assert ratio < 3, f"Disabled tracer adds excessive CPU work: {ratio:.2f}x the direct no-op span lifecycle"

    @pytest.mark.xfail(
        reason="Flaky under CI load — timing-dependent assertion (saw 214ms, 226ms). "
        "Issue #2260: keep as xfail until a non-timing-based overhead check is designed.",
        strict=False,
    )
    def test_get_timings_overhead(self):
        """Test that get_timings adds minimal overhead."""
        import time
        from unittest.mock import MagicMock

        from src.shared.timing import get_timings

        request = MagicMock()
        request.state.timings = {}

        iterations = 10000
        start = time.monotonic()
        for _ in range(iterations):
            timings = get_timings(request)
            timings.record("test", 1.0)
        elapsed_ms = (time.monotonic() - start) * 1000

        # 10000 iterations should complete in < 500ms even under CI load.
        # Previous threshold (200ms) was too tight for shared runners.
        assert elapsed_ms < 500, f"get_timings overhead too high: {elapsed_ms:.1f}ms for {iterations} iterations"


class TestTimingHeaderConfig:
    """Tests for timing header configuration."""

    def test_timing_header_enabled_by_default(self):
        """Test that timing headers are enabled by default."""
        from src.shared.config import Settings

        with patch.dict(os.environ, {}, clear=False):
            settings = Settings()
            assert settings.timing_header_enabled is True
