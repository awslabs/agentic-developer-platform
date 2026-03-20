"""
OpenTelemetry/X-Ray distributed tracing configuration.

Issue #144: Phase 2 - AWS X-Ray Distributed Tracing

Provides:
- OpenTelemetry TracerProvider with X-Ray ID generator and propagator
- OTLP exporter pointing to OTel Collector sidecar
- BatchSpanProcessor for efficient trace export
- setup_tracing(app) function to instrument FastAPI
- get_tracer(name) function for creating custom spans
- Feature flag (OTEL_ENABLED) so tracing can be disabled without affecting Phase 1

Configuration via environment variables:
- OTEL_ENABLED: Enable/disable tracing (default: false)
- OTEL_EXPORTER_OTLP_ENDPOINT: OTel Collector endpoint (default: http://localhost:4317)
- OTEL_SERVICE_NAME: Service name for traces (default: bedrock-gateway)
"""

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

# Lazy-initialized tracer provider
_tracer_provider = None
_tracing_initialized = False


def _is_otel_available() -> bool:
    """Check if OpenTelemetry packages are available."""
    try:
        import opentelemetry  # noqa: F401

        return True
    except ImportError:
        return False


def setup_tracing(app: Any) -> bool:
    """
    Initialize OpenTelemetry tracing and instrument FastAPI.

    This must be called early in app startup, before middleware registration.
    Tracing is opt-in via the OTEL_ENABLED environment variable.

    Args:
        app: FastAPI application instance

    Returns:
        True if tracing was successfully initialized, False otherwise
    """
    global _tracer_provider, _tracing_initialized

    if _tracing_initialized:
        logger.debug("Tracing already initialized, skipping")
        return True

    otel_enabled = os.environ.get("OTEL_ENABLED", "false").lower() in ("true", "1", "yes")
    if not otel_enabled:
        logger.info("OpenTelemetry tracing disabled (OTEL_ENABLED != true)")
        return False

    if not _is_otel_available():
        logger.warning("OpenTelemetry packages not installed. Install with: pip install '.[tracing]' to enable distributed tracing.")
        return False

    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        from opentelemetry.propagators.aws import AwsXRayPropagator
        from opentelemetry.sdk.extension.aws.trace import AwsXRayIdGenerator
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        # Read configuration from environment
        service_name = os.environ.get("OTEL_SERVICE_NAME", "bedrock-gateway")
        otlp_endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")

        # Create resource with service metadata
        resource = Resource.create(
            {
                "service.name": service_name,
                "service.namespace": "bedrock-gateway",
                "deployment.environment": os.environ.get("BG_ENVIRONMENT", "dev"),
            }
        )

        # Create TracerProvider with X-Ray ID generator
        _tracer_provider = TracerProvider(
            resource=resource,
            id_generator=AwsXRayIdGenerator(),
        )

        # Configure OTLP exporter (pointing to OTel Collector sidecar)
        otlp_exporter = OTLPSpanExporter(endpoint=otlp_endpoint, insecure=True)

        # Add BatchSpanProcessor for efficient export
        _tracer_provider.add_span_processor(
            BatchSpanProcessor(
                otlp_exporter,
                max_queue_size=2048,
                max_export_batch_size=512,
                schedule_delay_millis=5000,
            )
        )

        # Set as global tracer provider
        trace.set_tracer_provider(_tracer_provider)

        # Set X-Ray propagator for trace context propagation
        from opentelemetry import propagate

        propagate.set_global_textmap(AwsXRayPropagator())

        # Instrument FastAPI
        FastAPIInstrumentor.instrument_app(app)

        _tracing_initialized = True
        logger.info(
            "OpenTelemetry tracing initialized",
            extra={
                "service_name": service_name,
                "otlp_endpoint": otlp_endpoint,
                "id_generator": "AwsXRayIdGenerator",
            },
        )
        return True

    except Exception as e:
        logger.error(f"Failed to initialize OpenTelemetry tracing: {e}")
        return False


def get_tracer(name: str) -> Any:
    """
    Get an OpenTelemetry tracer for creating custom spans.

    If tracing is not enabled/available, returns a no-op tracer.

    Args:
        name: Tracer name (typically __name__ of the module)

    Returns:
        OpenTelemetry Tracer instance (or no-op if unavailable)
    """
    if not _is_otel_available():
        return _NoOpTracer()

    try:
        from opentelemetry import trace

        return trace.get_tracer(name)
    except Exception:
        return _NoOpTracer()


def shutdown_tracing() -> None:
    """Shutdown the tracer provider, flushing any pending spans."""
    global _tracer_provider, _tracing_initialized

    if _tracer_provider is not None:
        try:
            _tracer_provider.shutdown()
            logger.info("OpenTelemetry tracing shut down")
        except Exception as e:
            logger.error(f"Error shutting down tracing: {e}")
        finally:
            _tracer_provider = None
            _tracing_initialized = False


class _NoOpSpan:
    """No-op span for when tracing is disabled."""

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def set_attribute(self, key, value):
        pass

    def set_status(self, status):
        pass

    def record_exception(self, exception):
        pass

    def add_event(self, name, attributes=None):
        pass


class _NoOpTracer:
    """No-op tracer for when tracing is disabled."""

    def start_as_current_span(self, name, **kwargs):
        return _NoOpSpan()

    def start_span(self, name, **kwargs):
        return _NoOpSpan()
