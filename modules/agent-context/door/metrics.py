"""Door service metrics -- OTel counters + histograms for query health.

Cardinality discipline: ONLY bounded dimensions as metric attributes.
- OK: tenant_id, verb
- NEVER: asset_id, run_id, repo_name, project_id, query (unbounded -> cost explosion)

Feature-gated by KNOWLEDGE_LAYER_METRICS_ENABLED env var (default true).
Fail-open: metric calls never block query handling.

Usage:
    from .metrics import setup_door_metrics, record_query

    setup_door_metrics()
    record_query(tenant_id="t1", verb="search", duration_ms=42.5, error=False)

References:
    - Design: Issue #1755
    - Ingestion metrics: images/ingestion/metrics.py (same pattern)
"""

from __future__ import annotations

import logging
import os
from typing import Any

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module state
# ---------------------------------------------------------------------------

_meter: Any = None
_metrics_initialized = False
_metrics_enabled = False

# Counters
door_query_count: Any = None
door_query_errors: Any = None

# Histograms
door_query_latency: Any = None


# ---------------------------------------------------------------------------
# Feature flag
# ---------------------------------------------------------------------------


def _is_metrics_enabled() -> bool:
    """Check if metrics emission is enabled via env var."""
    return os.environ.get("KNOWLEDGE_LAYER_METRICS_ENABLED", "true").lower() in ("true", "1", "yes")


# ---------------------------------------------------------------------------
# OTel availability check
# ---------------------------------------------------------------------------


def _is_otel_available() -> bool:
    """Check if OpenTelemetry metrics packages are installed."""
    try:
        import opentelemetry.metrics  # noqa: F401

        return True
    except ImportError:
        return False


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------


def setup_door_metrics(service_name: str = "knowledge-layer-door") -> bool:
    """Initialize OTel metrics for the Door service.

    Creates a MeterProvider with OTLP gRPC exporter pointing to the ADOT
    Collector. Uses PeriodicExportingMetricReader for efficient batching.

    Args:
        service_name: Service name for metric resource metadata.

    Returns:
        True if metrics were successfully initialized, False otherwise.
        On False, record_query is a no-op (fail-open).
    """
    global _meter, _metrics_initialized, _metrics_enabled
    global door_query_count, door_query_errors, door_query_latency

    if _metrics_initialized:
        return _metrics_enabled

    _metrics_initialized = True

    if not _is_metrics_enabled():
        log.info("Door metrics disabled (KNOWLEDGE_LAYER_METRICS_ENABLED != true)")
        _metrics_enabled = False
        return False

    if not _is_otel_available():
        log.warning("OpenTelemetry metrics packages not installed -- Door metrics disabled.")
        _metrics_enabled = False
        return False

    try:
        from opentelemetry import metrics
        from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import (
            OTLPMetricExporter,
        )
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
        from opentelemetry.sdk.resources import Resource

        endpoint = os.environ.get(
            "OTEL_EXPORTER_OTLP_ENDPOINT",
            "http://adot-collector.adp-agents.svc.cluster.local:4317",
        )

        resource = Resource.create(
            {
                "service.name": service_name,
                "service.namespace": "knowledge-layer",
                "deployment.environment": os.environ.get("ENVIRONMENT", "dev"),
            }
        )

        reader = PeriodicExportingMetricReader(
            OTLPMetricExporter(endpoint=endpoint, insecure=True),
            export_interval_millis=5000,
        )
        provider = MeterProvider(resource=resource, metric_readers=[reader])
        metrics.set_meter_provider(provider)

        _meter = metrics.get_meter("knowledge-layer-door", version="1.0.0")

        # Define instruments
        door_query_count = _meter.create_counter(
            "kl.door_query_count",
            unit="1",
            description="Count of Door tool calls",
        )
        door_query_errors = _meter.create_counter(
            "kl.door_query_errors",
            unit="1",
            description="Count of Door tool call errors",
        )
        door_query_latency = _meter.create_histogram(
            "kl.door_query_latency",
            unit="ms",
            description="Latency of Door tool calls",
        )

        _metrics_enabled = True
        log.info(
            "Door metrics initialized: endpoint=%s, service=%s",
            endpoint,
            service_name,
        )
        return True

    except Exception as e:
        log.warning("Door metrics init failed (continuing without metrics): %s", e)
        _metrics_enabled = False
        return False


# ---------------------------------------------------------------------------
# Recording helpers (fail-open)
# ---------------------------------------------------------------------------


def record_query(tenant_id: str, verb: str, duration_ms: float, error: bool = False) -> None:
    """Record a Door query metric. Fail-open -- never raises."""
    try:
        if door_query_count:
            door_query_count.add(1, {"tenant_id": tenant_id, "verb": verb})
        if door_query_latency:
            door_query_latency.record(duration_ms, {"tenant_id": tenant_id, "verb": verb})
        if error and door_query_errors:
            door_query_errors.add(1, {"tenant_id": tenant_id, "verb": verb})
    except Exception:
        pass  # fail-open: metrics never block query handling


# ---------------------------------------------------------------------------
# Shutdown
# ---------------------------------------------------------------------------


def shutdown_door_metrics() -> None:
    """Flush pending metrics and shut down the meter provider."""
    global _meter, _metrics_initialized, _metrics_enabled

    if not _metrics_enabled:
        return

    try:
        from opentelemetry import metrics

        provider = metrics.get_meter_provider()
        if hasattr(provider, "shutdown"):
            provider.shutdown()
        log.info("Door metrics shut down")
    except Exception as e:
        log.warning("Error shutting down Door metrics: %s", e)
    finally:
        _meter = None
        _metrics_initialized = False
        _metrics_enabled = False
