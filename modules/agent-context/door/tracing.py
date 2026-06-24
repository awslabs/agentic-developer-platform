"""Door service tracing — OTel + FastAPI auto-instrumentation + identity enrichment.

Pattern: modules/gateway/src/shared/tracing.py (OTel + X-Ray)
Feature-gated by KNOWLEDGE_LAYER_TRACES_ENABLED (default true).

Provides:
- setup_tracing(app): Instruments FastAPI with OTel, X-Ray ID generator, OTLP exporter
- get_tracer(name): Returns a tracer (or NoOp if tracing unavailable)
- shutdown_tracing(): Flush pending spans and tear down the provider
"""

from __future__ import annotations

import logging
import os
from typing import Any

log = logging.getLogger(__name__)

# Module-level state
_tracer_provider: Any = None
_tracing_initialized = False
_tracing_enabled = False


def _is_otel_available() -> bool:
    """Check if OpenTelemetry packages are installed."""
    try:
        import opentelemetry  # noqa: F401

        return True
    except ImportError:
        return False


def setup_tracing(app: Any) -> bool:
    """Initialize OTel tracing for the Door FastAPI app.

    - Creates TracerProvider with AwsXRayIdGenerator
    - OTLP gRPC exporter -> ADOT Collector
    - FastAPIInstrumentor.instrument_app(app)
    - Returns False (fail-open) if anything fails

    Args:
        app: FastAPI application instance

    Returns:
        True if tracing was successfully initialized, False otherwise
    """
    global _tracer_provider, _tracing_initialized, _tracing_enabled

    if _tracing_initialized:
        return _tracing_enabled

    enabled = os.environ.get("KNOWLEDGE_LAYER_TRACES_ENABLED", "true").lower() in ("true", "1")
    if not enabled:
        log.info("Door tracing disabled (KNOWLEDGE_LAYER_TRACES_ENABLED != true)")
        _tracing_initialized = True
        _tracing_enabled = False
        return False

    if not _is_otel_available():
        log.warning("OTel packages not installed — Door tracing disabled (fail-open)")
        _tracing_initialized = True
        _tracing_enabled = False
        return False

    try:
        from opentelemetry import propagate, trace
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        from opentelemetry.propagators.aws import AwsXRayPropagator
        from opentelemetry.sdk.extension.aws.trace import AwsXRayIdGenerator
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        endpoint = os.environ.get(
            "OTEL_EXPORTER_OTLP_ENDPOINT",
            "http://adot-collector.adp-agents.svc.cluster.local:4317",
        )

        resource = Resource.create(
            {
                "service.name": os.environ.get("OTEL_SERVICE_NAME", "knowledge-layer-door"),
                "service.namespace": "agent-context",
                "deployment.environment": os.environ.get("ENVIRONMENT", "dev"),
            }
        )

        _tracer_provider = TracerProvider(resource=resource, id_generator=AwsXRayIdGenerator())
        _tracer_provider.add_span_processor(
            BatchSpanProcessor(
                OTLPSpanExporter(endpoint=endpoint, insecure=True),
                max_queue_size=2048,
                schedule_delay_millis=5000,
            )
        )
        trace.set_tracer_provider(_tracer_provider)
        propagate.set_global_textmap(AwsXRayPropagator())
        FastAPIInstrumentor.instrument_app(app)

        _tracing_initialized = True
        _tracing_enabled = True
        log.info("Door tracing initialized (endpoint=%s)", endpoint)
        return True
    except Exception as e:
        log.warning("Door tracing init failed (continuing without traces): %s", e)
        _tracing_initialized = True
        _tracing_enabled = False
        return False


def get_tracer(name: str) -> Any:
    """Get a tracer instance.

    Returns a real OTel tracer if tracing is enabled and available,
    otherwise returns a _NoOpTracer that silently discards all span calls.
    """
    if not _tracing_enabled or not _is_otel_available():
        return _NoOpTracer()

    try:
        from opentelemetry import trace

        return trace.get_tracer(name)
    except Exception:
        return _NoOpTracer()


def shutdown_tracing() -> None:
    """Shutdown the tracer provider, flushing any pending spans."""
    global _tracer_provider, _tracing_initialized, _tracing_enabled

    if _tracer_provider is not None:
        try:
            _tracer_provider.shutdown()
        except Exception as e:
            log.warning("Door tracing shutdown error: %s", e)
        finally:
            _tracer_provider = None
            _tracing_initialized = False
            _tracing_enabled = False


# ---------------------------------------------------------------------------
# No-op fallback classes
# ---------------------------------------------------------------------------


class _NoOpSpan:
    """No-op span for when tracing is disabled or unavailable."""

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def set_attribute(self, key: str, value: Any) -> None:
        pass

    def set_status(self, status: Any, description: str | None = None) -> None:
        pass

    def record_exception(self, exception: BaseException) -> None:
        pass

    def add_event(self, name: str, attributes: dict | None = None) -> None:
        pass

    def end(self) -> None:
        pass


class _NoOpTracer:
    """No-op tracer for when tracing is disabled or unavailable."""

    def start_as_current_span(self, name: str, **kwargs: Any) -> _NoOpSpan:
        return _NoOpSpan()

    def start_span(self, name: str, **kwargs: Any) -> _NoOpSpan:
        return _NoOpSpan()
