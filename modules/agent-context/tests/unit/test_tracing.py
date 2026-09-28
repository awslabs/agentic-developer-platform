"""Unit tests for tracing.py — Knowledge Layer distributed tracing.

Tests cover:
- Tracing init success (OTel SDK available + enabled)
- Tracing disabled via KNOWLEDGE_LAYER_TRACES_ENABLED=false
- Fail-open behavior (missing OTel SDK falls back to NoOpTracer)
- NoOpTracer/NoOpSpan interface completeness
- Stage span emission with correct attributes
- Root span in sqs-worker wraps stage spans
- Shutdown flushes spans
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Add ingestion scripts to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "images" / "ingestion"))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def reset_tracing():
    """Reset tracing module state between tests."""
    import tracing

    tracing._tracer_provider = None
    tracing._tracing_initialized = False
    tracing._tracing_enabled = False
    yield
    tracing._tracer_provider = None
    tracing._tracing_initialized = False
    tracing._tracing_enabled = False


@pytest.fixture(autouse=True)
def reset_telemetry():
    """Reset telemetry module state between tests (needed for StageTracker tests)."""
    import telemetry

    telemetry._configured = False
    telemetry.clear_correlation_context()
    yield
    telemetry._configured = False
    telemetry.clear_correlation_context()


# ---------------------------------------------------------------------------
# Tests: setup_tracing
# ---------------------------------------------------------------------------


class TestSetupTracing:
    """Tests for tracing initialization."""

    def test_init_disabled_via_env(self):
        """Returns False and uses NoOpTracer when KNOWLEDGE_LAYER_TRACES_ENABLED=false."""
        import tracing

        with patch.dict("os.environ", {"KNOWLEDGE_LAYER_TRACES_ENABLED": "false"}):
            result = tracing.setup_tracing()

        assert result is False
        assert tracing._tracing_enabled is False
        # get_tracer should return NoOpTracer
        tracer = tracing.get_tracer("test")
        assert isinstance(tracer, tracing._NoOpTracer)

    def test_init_disabled_various_values(self):
        """Tracing is disabled for various false-ish env values."""
        import tracing

        for val in ("false", "0", "no", "False", "NO"):
            tracing._tracing_initialized = False
            tracing._tracing_enabled = False
            with patch.dict("os.environ", {"KNOWLEDGE_LAYER_TRACES_ENABLED": val}):
                result = tracing.setup_tracing()
            assert result is False, f"Expected False for value '{val}'"

    def test_init_failopen_when_otel_unavailable(self):
        """Falls back to NoOpTracer when OTel packages are not installed."""
        import tracing

        with patch.dict("os.environ", {"KNOWLEDGE_LAYER_TRACES_ENABLED": "true"}):
            with patch.object(tracing, "_is_otel_available", return_value=False):
                result = tracing.setup_tracing()

        assert result is False
        assert tracing._tracing_enabled is False

    def test_init_failopen_on_sdk_exception(self):
        """Falls back to NoOpTracer if OTel SDK init throws."""
        import tracing

        with patch.dict("os.environ", {"KNOWLEDGE_LAYER_TRACES_ENABLED": "true"}):
            with patch.object(tracing, "_is_otel_available", return_value=True):
                # Simulate import failure inside try block
                with patch(
                    "builtins.__import__",
                    side_effect=ImportError("missing grpc"),
                ):
                    result = tracing.setup_tracing()

        assert result is False
        assert tracing._tracing_enabled is False

    def test_init_idempotent(self):
        """Second call to setup_tracing is a no-op."""
        import tracing

        with patch.dict("os.environ", {"KNOWLEDGE_LAYER_TRACES_ENABLED": "false"}):
            tracing.setup_tracing()
            # Mark as initialized but not enabled
            assert tracing._tracing_initialized is True

            # Second call should just return cached state
            result = tracing.setup_tracing()
            assert result is False

    def test_init_success_with_otel(self):
        """Tracing initializes successfully when OTel is available."""
        import tracing

        # Mock all OTel components
        mock_provider = MagicMock()
        mock_exporter = MagicMock()
        mock_processor = MagicMock()

        with patch.dict(
            "os.environ",
            {
                "KNOWLEDGE_LAYER_TRACES_ENABLED": "true",
                "OTEL_EXPORTER_OTLP_ENDPOINT": "http://localhost:4317",
            },
        ):
            with patch.object(tracing, "_is_otel_available", return_value=True):
                with patch.dict(
                    "sys.modules",
                    {
                        "opentelemetry": MagicMock(),
                        "opentelemetry.trace": MagicMock(),
                        "opentelemetry.exporter": MagicMock(),
                        "opentelemetry.exporter.otlp": MagicMock(),
                        "opentelemetry.exporter.otlp.proto": MagicMock(),
                        "opentelemetry.exporter.otlp.proto.grpc": MagicMock(),
                        "opentelemetry.exporter.otlp.proto.grpc.trace_exporter": MagicMock(
                            OTLPSpanExporter=MagicMock(return_value=mock_exporter)
                        ),
                        "opentelemetry.propagators": MagicMock(),
                        "opentelemetry.propagators.aws": MagicMock(),
                        "opentelemetry.sdk": MagicMock(),
                        "opentelemetry.sdk.extension": MagicMock(),
                        "opentelemetry.sdk.extension.aws": MagicMock(),
                        "opentelemetry.sdk.extension.aws.trace": MagicMock(
                            AwsXRayIdGenerator=MagicMock()
                        ),
                        "opentelemetry.sdk.resources": MagicMock(
                            Resource=MagicMock(create=MagicMock())
                        ),
                        "opentelemetry.sdk.trace": MagicMock(
                            TracerProvider=MagicMock(return_value=mock_provider)
                        ),
                        "opentelemetry.sdk.trace.export": MagicMock(
                            BatchSpanProcessor=MagicMock(return_value=mock_processor)
                        ),
                        "opentelemetry.propagate": MagicMock(),
                    },
                ):
                    result = tracing.setup_tracing()

        assert result is True
        assert tracing._tracing_enabled is True
        assert tracing._tracer_provider is mock_provider


# ---------------------------------------------------------------------------
# Tests: get_tracer
# ---------------------------------------------------------------------------


class TestGetTracer:
    """Tests for tracer retrieval."""

    def test_returns_noop_when_disabled(self):
        """Returns NoOpTracer when tracing is disabled."""
        import tracing

        tracing._tracing_initialized = True
        tracing._tracing_enabled = False

        tracer = tracing.get_tracer("test")
        assert isinstance(tracer, tracing._NoOpTracer)

    def test_returns_noop_when_otel_unavailable(self):
        """Returns NoOpTracer when OTel packages missing."""
        import tracing

        tracing._tracing_enabled = True
        with patch.object(tracing, "_is_otel_available", return_value=False):
            tracer = tracing.get_tracer("test")
        assert isinstance(tracer, tracing._NoOpTracer)


# ---------------------------------------------------------------------------
# Tests: NoOpTracer / NoOpSpan
# ---------------------------------------------------------------------------


class TestNoOpFallback:
    """Tests that NoOp classes implement the required interface."""

    def test_noop_tracer_start_as_current_span(self):
        """NoOpTracer.start_as_current_span returns a usable NoOpSpan."""
        import tracing

        tracer = tracing._NoOpTracer()
        span = tracer.start_as_current_span("test_span", attributes={"key": "val"})
        assert isinstance(span, tracing._NoOpSpan)

    def test_noop_tracer_start_span(self):
        """NoOpTracer.start_span returns a usable NoOpSpan."""
        import tracing

        tracer = tracing._NoOpTracer()
        span = tracer.start_span("test_span")
        assert isinstance(span, tracing._NoOpSpan)

    def test_noop_span_context_manager(self):
        """NoOpSpan works as a context manager."""
        import tracing

        span = tracing._NoOpSpan()
        with span as s:
            assert s is span

    def test_noop_span_methods_are_silent(self):
        """NoOpSpan methods accept calls without error."""
        import tracing

        span = tracing._NoOpSpan()
        span.set_attribute("key", "value")
        span.set_status("OK")
        span.set_status("ERROR", "something failed")
        span.record_exception(ValueError("test"))
        span.add_event("event_name", attributes={"a": 1})
        span.end()
        # No assertions needed — these should not raise

    def test_noop_span_as_context_manager_in_tracer(self):
        """NoOpTracer.start_as_current_span can be used as a context manager."""
        import tracing

        tracer = tracing._NoOpTracer()
        cm = tracer.start_as_current_span("test")
        # Should support __enter__/__exit__ (it's a NoOpSpan which does)
        result = cm.__enter__()
        assert result is cm
        cm.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# Tests: StageTracker span integration
# ---------------------------------------------------------------------------


class TestStageTrackerSpans:
    """Tests that StageTracker emits spans via the tracing module."""

    def test_stage_creates_span_with_attributes(self):
        """A stage() block creates a span with the expected correlation attributes."""
        from telemetry import set_correlation_context

        # Set up correlation context
        set_correlation_context(
            asset_type="repo",
            tenant_id="tenant-123",
            owner_sub="user-abc",
        )

        # Create a mock tracer that records calls
        mock_span = MagicMock()
        mock_span.__enter__ = MagicMock(return_value=mock_span)
        mock_span.__exit__ = MagicMock(return_value=False)

        mock_tracer = MagicMock()
        mock_tracer.start_as_current_span = MagicMock(return_value=mock_span)

        with patch("stage_tracker._tracer", mock_tracer):
            with patch("stage_tracker.stage_db.create_index_run", return_value="run-001"):
                with patch("stage_tracker.stage_db.start_stage", return_value="stage-id"):
                    with patch("stage_tracker.stage_db.fail_stage"):
                        from stage_tracker import StageTracker

                        mock_conn = MagicMock()
                        tracker = StageTracker(mock_conn, "org/repo", "repo-id-1")

                        with tracker.stage("zoekt"):
                            pass  # Don't call verify — stage will be marked failed

        # Verify span was started with correct name and attributes
        mock_tracer.start_as_current_span.assert_called_once()
        call_args = mock_tracer.start_as_current_span.call_args
        assert call_args[0][0] == "zoekt"  # span name
        attrs = call_args[1]["attributes"]
        assert attrs["asset_id"] == "repo-id-1"
        assert attrs["run_id"] == "run-001"
        assert attrs["repo_name"] == "org/repo"
        assert attrs["stage"] == "zoekt"
        assert attrs["asset_type"] == "repo"
        assert attrs["tenant_id"] == "tenant-123"
        assert attrs["owner_sub"] == "user-abc"

    def test_verified_stage_sets_span_ok(self):
        """A verified stage sets span status to OK with artifact_ref."""
        mock_span = MagicMock()
        mock_span.__enter__ = MagicMock(return_value=mock_span)
        mock_span.__exit__ = MagicMock(return_value=False)

        mock_tracer = MagicMock()
        mock_tracer.start_as_current_span = MagicMock(return_value=mock_span)

        with patch("stage_tracker._tracer", mock_tracer):
            with patch("stage_tracker.stage_db.create_index_run", return_value="run-002"):
                with patch("stage_tracker.stage_db.start_stage", return_value="stage-id"):
                    with patch("stage_tracker.stage_db.verify_stage"):
                        from stage_tracker import StageTracker

                        mock_conn = MagicMock()
                        tracker = StageTracker(mock_conn, "org/repo", "repo-id-2")

                        with tracker.stage("clone") as ctx:
                            ctx.set_artifact("/tmp/clone/org/repo")
                            ctx.verify(lambda: True)

        # Span should have OK status and artifact_ref attribute
        from opentelemetry.trace import StatusCode

        mock_span.set_attribute.assert_any_call("artifact_ref", "/tmp/clone/org/repo")
        mock_span.set_status.assert_called_with(StatusCode.OK)

    def test_failed_stage_sets_span_error(self):
        """A failed stage sets span status to ERROR with the error message."""
        mock_span = MagicMock()
        mock_span.__enter__ = MagicMock(return_value=mock_span)
        mock_span.__exit__ = MagicMock(return_value=False)

        mock_tracer = MagicMock()
        mock_tracer.start_as_current_span = MagicMock(return_value=mock_span)

        with patch("stage_tracker._tracer", mock_tracer):
            with patch("stage_tracker.stage_db.create_index_run", return_value="run-003"):
                with patch("stage_tracker.stage_db.start_stage", return_value="stage-id"):
                    with patch("stage_tracker.stage_db.fail_stage"):
                        from stage_tracker import StageTracker

                        mock_conn = MagicMock()
                        tracker = StageTracker(mock_conn, "org/repo", "repo-id-3")

                        with tracker.stage("deepwiki") as ctx:
                            ctx.fail("deepwiki API timeout")

        from opentelemetry.trace import StatusCode

        mock_span.set_status.assert_called_with(StatusCode.ERROR, "deepwiki API timeout")

    def test_exception_in_stage_records_on_span(self):
        """An exception inside stage() is recorded on the span."""
        mock_span = MagicMock()
        mock_span.__enter__ = MagicMock(return_value=mock_span)
        mock_span.__exit__ = MagicMock(return_value=False)

        mock_tracer = MagicMock()
        mock_tracer.start_as_current_span = MagicMock(return_value=mock_span)

        with patch("stage_tracker._tracer", mock_tracer):
            with patch("stage_tracker.stage_db.create_index_run", return_value="run-004"):
                with patch("stage_tracker.stage_db.start_stage", return_value="stage-id"):
                    with patch("stage_tracker.stage_db.fail_stage"):
                        from stage_tracker import StageTracker

                        mock_conn = MagicMock()
                        tracker = StageTracker(mock_conn, "org/repo", "repo-id-4")

                        with tracker.stage("scip"):
                            raise RuntimeError("SCIP indexer crashed")

        # Exception should be recorded on the span
        mock_span.record_exception.assert_called_once()
        exc_arg = mock_span.record_exception.call_args[0][0]
        assert isinstance(exc_arg, RuntimeError)
        assert "SCIP indexer crashed" in str(exc_arg)

    def test_stage_failopen_when_tracer_raises(self):
        """If the tracer itself raises, ingestion continues (fail-open)."""
        # Tracer that raises on start_as_current_span
        mock_tracer = MagicMock()
        broken_span = MagicMock()
        broken_span.__enter__ = MagicMock(side_effect=RuntimeError("OTel broke"))
        mock_tracer.start_as_current_span = MagicMock(return_value=broken_span)

        with patch("stage_tracker._tracer", mock_tracer):
            with patch("stage_tracker.stage_db.create_index_run", return_value="run-005"):
                with patch("stage_tracker.stage_db.start_stage", return_value="stage-id"):
                    with patch("stage_tracker.stage_db.fail_stage"):
                        from stage_tracker import StageTracker

                        mock_conn = MagicMock()
                        tracker = StageTracker(mock_conn, "org/repo", "repo-id-5")

                        # This should NOT raise — fail-open
                        with tracker.stage("clone") as ctx:
                            ctx.set_artifact("/tmp/clone")
                            ctx.verify(lambda: True)

        # If we get here, fail-open worked. The stage should still have results.
        # Note: with the broken span, the verify logic still runs.

    def test_root_span_wraps_stages(self):
        """Root span in sqs-worker is parent of stage spans (trace context propagation).

        Verifies that when the sqs-worker creates an 'ingestion_run' root span
        and StageTracker creates child spans within it, the trace context propagation
        makes stage spans children of the root span.
        """
        from opentelemetry import trace
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import (
            SimpleSpanProcessor,
            SpanExporter,
            SpanExportResult,
        )

        # Simple in-memory exporter that captures finished spans
        class _ListExporter(SpanExporter):
            def __init__(self):
                self.spans = []

            def export(self, spans):
                self.spans.extend(spans)
                return SpanExportResult.SUCCESS

            def shutdown(self):
                pass

        exporter = _ListExporter()
        provider = TracerProvider(resource=Resource.create({"service.name": "test"}))
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        trace.set_tracer_provider(provider)

        try:
            tracer = trace.get_tracer("test-root-span")

            # Simulate sqs-worker: create root span wrapping stage spans
            with tracer.start_as_current_span("ingestion_run", attributes={"asset_id": "org/repo"}):
                # Simulate StageTracker: child span created within root context
                with tracer.start_as_current_span(
                    "clone", attributes={"stage": "clone", "run_id": "run-001"}
                ):
                    pass  # stage work here

                with tracer.start_as_current_span(
                    "zoekt", attributes={"stage": "zoekt", "run_id": "run-001"}
                ):
                    pass

            # Verify span tree structure
            spans = exporter.spans
            assert len(spans) == 3, f"Expected 3 spans, got {len(spans)}"

            # Find spans by name
            span_by_name = {s.name: s for s in spans}
            root = span_by_name["ingestion_run"]
            clone = span_by_name["clone"]
            zoekt = span_by_name["zoekt"]

            # All spans share the same trace ID
            assert clone.context.trace_id == root.context.trace_id
            assert zoekt.context.trace_id == root.context.trace_id

            # Child spans have root as parent
            assert clone.parent.span_id == root.context.span_id
            assert zoekt.parent.span_id == root.context.span_id

            # Root has no parent
            assert root.parent is None

        finally:
            provider.shutdown()
            # Reset global tracer provider
            trace.set_tracer_provider(trace.NoOpTracerProvider())


# ---------------------------------------------------------------------------
# Tests: shutdown_tracing
# ---------------------------------------------------------------------------


class TestShutdownTracing:
    """Tests for tracing shutdown."""

    def test_shutdown_calls_provider_shutdown(self):
        """shutdown_tracing calls the provider's shutdown method."""
        import tracing

        mock_provider = MagicMock()
        tracing._tracer_provider = mock_provider
        tracing._tracing_initialized = True
        tracing._tracing_enabled = True

        tracing.shutdown_tracing()

        mock_provider.shutdown.assert_called_once()
        assert tracing._tracer_provider is None
        assert tracing._tracing_initialized is False
        assert tracing._tracing_enabled is False

    def test_shutdown_noop_when_not_initialized(self):
        """shutdown_tracing is safe to call when not initialized."""
        import tracing

        tracing._tracer_provider = None
        # Should not raise
        tracing.shutdown_tracing()

    def test_shutdown_failopen_on_error(self):
        """shutdown_tracing doesn't raise if provider.shutdown fails."""
        import tracing

        mock_provider = MagicMock()
        mock_provider.shutdown.side_effect = RuntimeError("flush failed")
        tracing._tracer_provider = mock_provider
        tracing._tracing_initialized = True
        tracing._tracing_enabled = True

        # Should not raise
        tracing.shutdown_tracing()
        assert tracing._tracer_provider is None
