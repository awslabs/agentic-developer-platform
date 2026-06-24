"""Knowledge Layer metrics -- OTel counters + histograms for pipeline health.

Cardinality discipline: ONLY bounded dimensions as metric attributes.
- OK: tenant_id, stage, asset_type, verb
- NEVER: asset_id, run_id, repo_name, project_id (unbounded -> cost explosion)

Feature-gated by KNOWLEDGE_LAYER_METRICS_ENABLED env var (default true).
Fail-open: metric calls never block ingestion or queries.

Usage:
    from metrics import setup_metrics, record_stage_complete, record_stage_failed

    setup_metrics()
    record_stage_complete(tenant_id="t1", stage="clone", asset_type="repo", latency_ms=1234.5)

References:
    - Design: Issue #1755
    - Tracing pattern: tracing.py (same fail-open discipline)
    - ADOT Collector: modules/agent-factory/webhook-ingress/infra/otel-collector.tf
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
assets_registered: Any = None
assets_queued: Any = None
assets_indexed: Any = None
assets_failed: Any = None

# Histograms
stage_latency: Any = None
ingestion_duration: Any = None


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


def setup_metrics(service_name: str = "knowledge-layer") -> bool:
    """Initialize OTel metrics for the Knowledge Layer.

    Creates a MeterProvider with OTLP gRPC exporter pointing to the ADOT
    Collector. Uses PeriodicExportingMetricReader for efficient batching.

    Args:
        service_name: Service name for metric resource metadata.

    Returns:
        True if metrics were successfully initialized, False otherwise.
        On False, all record_* functions are no-ops (fail-open).
    """
    global _meter, _metrics_initialized, _metrics_enabled
    global assets_registered, assets_queued, assets_indexed, assets_failed
    global stage_latency, ingestion_duration

    if _metrics_initialized:
        return _metrics_enabled

    _metrics_initialized = True

    if not _is_metrics_enabled():
        log.info("Knowledge Layer metrics disabled (KNOWLEDGE_LAYER_METRICS_ENABLED != true)")
        _metrics_enabled = False
        return False

    if not _is_otel_available():
        log.warning(
            "OpenTelemetry metrics packages not installed -- metrics disabled. "
            "Install: opentelemetry-sdk, opentelemetry-exporter-otlp-proto-grpc"
        )
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

        _meter = metrics.get_meter("knowledge-layer", version="1.0.0")

        # Define instruments
        assets_registered = _meter.create_counter(
            "kl.assets_registered",
            unit="1",
            description="Count of assets registered for ingestion",
        )
        assets_queued = _meter.create_counter(
            "kl.assets_queued",
            unit="1",
            description="Count of assets queued for processing",
        )
        assets_indexed = _meter.create_counter(
            "kl.assets_indexed",
            unit="1",
            description="Count of assets successfully indexed (by stage)",
        )
        assets_failed = _meter.create_counter(
            "kl.assets_failed",
            unit="1",
            description="Count of assets that failed indexing (by stage)",
        )
        stage_latency = _meter.create_histogram(
            "kl.stage_latency",
            unit="ms",
            description="Latency of individual pipeline stages",
        )
        ingestion_duration = _meter.create_histogram(
            "kl.ingestion_duration",
            unit="ms",
            description="Total duration of an ingestion run",
        )

        _metrics_enabled = True
        log.info(
            "Knowledge Layer metrics initialized: endpoint=%s, service=%s",
            endpoint,
            service_name,
        )
        return True

    except Exception as e:
        log.warning("Metrics init failed (continuing without metrics): %s", e)
        _metrics_enabled = False
        return False


# ---------------------------------------------------------------------------
# Recording helpers (fail-open)
# ---------------------------------------------------------------------------


def record_stage_complete(tenant_id: str, stage: str, asset_type: str, latency_ms: float) -> None:
    """Record a stage completion. Fail-open -- never raises."""
    try:
        if assets_indexed:
            assets_indexed.add(
                1, {"tenant_id": tenant_id, "stage": stage, "asset_type": asset_type}
            )
        if stage_latency:
            stage_latency.record(latency_ms, {"tenant_id": tenant_id, "stage": stage})
    except Exception:
        pass  # fail-open: metrics never block ingestion


def record_stage_failed(tenant_id: str, stage: str, asset_type: str) -> None:
    """Record a stage failure. Fail-open -- never raises."""
    try:
        if assets_failed:
            assets_failed.add(1, {"tenant_id": tenant_id, "stage": stage, "asset_type": asset_type})
    except Exception:
        pass  # fail-open: metrics never block ingestion


def record_ingestion_duration(tenant_id: str, asset_type: str, duration_ms: float) -> None:
    """Record total ingestion run duration. Fail-open -- never raises."""
    try:
        if ingestion_duration:
            ingestion_duration.record(
                duration_ms, {"tenant_id": tenant_id, "asset_type": asset_type}
            )
    except Exception:
        pass  # fail-open: metrics never block ingestion


# ---------------------------------------------------------------------------
# Shutdown
# ---------------------------------------------------------------------------


def shutdown_metrics() -> None:
    """Flush pending metrics and shut down the meter provider.

    Call at process exit to ensure all metrics are exported.
    """
    global _meter, _metrics_initialized, _metrics_enabled

    if not _metrics_enabled:
        return

    try:
        from opentelemetry import metrics

        provider = metrics.get_meter_provider()
        if hasattr(provider, "shutdown"):
            provider.shutdown()
        log.info("Knowledge Layer metrics shut down")
    except Exception as e:
        log.warning("Error shutting down metrics: %s", e)
    finally:
        _meter = None
        _metrics_initialized = False
        _metrics_enabled = False
