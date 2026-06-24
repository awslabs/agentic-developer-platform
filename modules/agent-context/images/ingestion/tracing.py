"""Knowledge Layer distributed tracing — span tree per indexing run.

Provides OpenTelemetry tracing for the ingestion pipeline, giving operators
a single X-Ray view per document: which stages ran, their duration, and status.

Pattern: mirrors modules/gateway/src/shared/tracing.py (OTel SDK + X-Ray ID
generator + OTLP gRPC exporter -> ADOT Collector -> X-Ray).

Feature-gated by KNOWLEDGE_LAYER_TRACES_ENABLED (default true).
Fail-open: if OTel SDK fails to init, a NoOpTracer is used and ingestion
continues unaffected.

Usage:
    from tracing import setup_tracing, get_tracer

    setup_tracing()
    tracer = get_tracer("knowledge-layer.ingestion")
    with tracer.start_as_current_span("stage_name", attributes={...}) as span:
        ...

References:
    - Design: docs/agent-context/design-1746-observability.md (section 4.3)
    - Gateway tracing: modules/gateway/src/shared/tracing.py
    - Issue: #1753
"""

from __future__ import annotations

import logging
import os
from typing import Any

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module state
# ---------------------------------------------------------------------------

_tracer_provider: Any = None
_tracing_initialized = False
_tracing_enabled = False


# ---------------------------------------------------------------------------
# Feature flag
# ---------------------------------------------------------------------------


def _is_traces_enabled() -> bool:
    """Check if tracing is enabled via env var."""
    return os.environ.get(
        "KNOWLEDGE_LAYER_TRACES_ENABLED", "true"
    ).lower() in ("true", "1", "yes")


# ---------------------------------------------------------------------------
# OTel availability check
# ---------------------------------------------------------------------------


def _is_otel_available() -> bool:
    """Check if OpenTelemetry packages are installed."""
    try:
        import opentelemetry  # noqa: F401

        return True
    except ImportError:
        return False


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------


def setup_tracing(service_name: str = "knowledge-layer-ingestion") -> bool:
    """Initialize OpenTelemetry tracing for the Knowledge Layer ingestion pipeline.

    Creates a TracerProvider with AWS X-Ray ID generator and OTLP gRPC exporter
    pointing to the ADOT Collector. Uses BatchSpanProcessor for efficient export.

    Args:
        service_name: Service name for trace resource metadata.

    Returns:
        True if tracing was successfully initialized, False otherwise.
        On False, a NoOpTracer is used (fail-open).
    """
    global _tracer_provider, _tracing_initialized, _tracing_enabled

    if _tracing_initialized:
        return _tracing_enabled

    _tracing_initialized = True

    if not _is_traces_enabled():
        log.info("Knowledge Layer tracing disabled (KNOWLEDGE_LAYER_TRACES_ENABLED != true)")
        _tracing_enabled = False
        return False

    if not _is_otel_available():
        log.warning(
            "OpenTelemetry packages not installed — tracing disabled. "
            "Install: opentelemetry-sdk, opentelemetry-exporter-otlp-proto-grpc, "
            "opentelemetry-sdk-extension-aws, opentelemetry-propagator-aws-xray"
        )
        _tracing_enabled = False
        return False

    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        from opentelemetry.propagators.aws import AwsXRayPropagator
        from opentelemetry.sdk.extension.aws.trace import AwsXRayIdGenerator
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        # Read OTLP endpoint from env (default: ADOT Collector cross-namespace FQDN)
        otlp_endpoint = os.environ.get(
            "OTEL_EXPORTER_OTLP_ENDPOINT",
            "http://adot-collector.adp-agents.svc.cluster.local:4317",
        )

        # Resource with service metadata
        resource = Resource.create(
            {
                "service.name": service_name,
                "service.namespace": "knowledge-layer",
                "deployment.environment": os.environ.get("ENVIRONMENT", "dev"),
            }
        )

        # TracerProvider with AWS X-Ray ID generator (for X-Ray compatible trace IDs)
        _tracer_provider = TracerProvider(
            resource=resource,
            id_generator=AwsXRayIdGenerator(),
        )

        # OTLP gRPC exporter -> ADOT Collector (insecure within cluster)
        otlp_exporter = OTLPSpanExporter(endpoint=otlp_endpoint, insecure=True)

        # BatchSpanProcessor: efficient batching, tolerant of transient failures
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

        _tracing_enabled = True
        log.info(
            "Knowledge Layer tracing initialized: endpoint=%s, service=%s",
            otlp_endpoint,
            service_name,
        )
        return True

    except Exception as e:
        log.warning("Failed to initialize tracing (fail-open): %s", e)
        _tracing_enabled = False
        return False


# ---------------------------------------------------------------------------
# Tracer access
# ---------------------------------------------------------------------------


def get_tracer(name: str) -> Any:
    """Get an OpenTelemetry tracer for creating spans.

    If tracing is not initialized or failed, returns a NoOpTracer.

    Args:
        name: Tracer name (e.g. "knowledge-layer.ingestion").

    Returns:
        A Tracer instance (real or no-op).
    """
    if not _tracing_enabled or not _is_otel_available():
        return _NoOpTracer()

    try:
        from opentelemetry import trace

        return trace.get_tracer(name)
    except Exception:
        return _NoOpTracer()


# ---------------------------------------------------------------------------
# Shutdown
# ---------------------------------------------------------------------------


def shutdown_tracing() -> None:
    """Flush pending spans and shut down the tracer provider.

    Call at process exit to ensure all spans are exported.
    """
    global _tracer_provider, _tracing_initialized, _tracing_enabled

    if _tracer_provider is not None:
        try:
            _tracer_provider.shutdown()
            log.info("Knowledge Layer tracing shut down")
        except Exception as e:
            log.warning("Error shutting down tracing: %s", e)
        finally:
            _tracer_provider = None
            _tracing_initialized = False
            _tracing_enabled = False


# ---------------------------------------------------------------------------
# No-op fallback (fail-open: tracing never blocks ingestion)
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

    def start_as_current_span(self, name: str, **kwargs) -> _NoOpSpan:
        return _NoOpSpan()

    def start_span(self, name: str, **kwargs) -> _NoOpSpan:
        return _NoOpSpan()
