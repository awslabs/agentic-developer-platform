"""Unit tests for metrics.py -- Knowledge Layer OTel metrics emission.

Tests cover:
- Metrics init success (OTel SDK available + enabled)
- Metrics disabled via KNOWLEDGE_LAYER_METRICS_ENABLED=false
- Fail-open behavior (missing OTel SDK falls back to no-ops)
- record_stage_complete: counter incremented with correct dimensions
- record_stage_failed: failure counter incremented
- Cardinality discipline: asset_id/run_id NEVER used as metric dimensions
- Door metrics: query counter + latency histogram recorded
- record_ingestion_duration: histogram recorded with correct dimensions
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Add ingestion scripts to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "images" / "ingestion"))
# Add door to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def reset_ingestion_metrics():
    """Reset metrics module state between tests."""
    import metrics

    metrics._meter = None
    metrics._metrics_initialized = False
    metrics._metrics_enabled = False
    metrics.assets_registered = None
    metrics.assets_queued = None
    metrics.assets_indexed = None
    metrics.assets_failed = None
    metrics.stage_latency = None
    metrics.ingestion_duration = None
    yield
    metrics._meter = None
    metrics._metrics_initialized = False
    metrics._metrics_enabled = False
    metrics.assets_registered = None
    metrics.assets_queued = None
    metrics.assets_indexed = None
    metrics.assets_failed = None
    metrics.stage_latency = None
    metrics.ingestion_duration = None


@pytest.fixture(autouse=True)
def reset_door_metrics():
    """Reset door metrics module state between tests."""
    from door import metrics as door_metrics_mod

    door_metrics_mod._meter = None
    door_metrics_mod._metrics_initialized = False
    door_metrics_mod._metrics_enabled = False
    door_metrics_mod.door_query_count = None
    door_metrics_mod.door_query_errors = None
    door_metrics_mod.door_query_latency = None
    yield
    door_metrics_mod._meter = None
    door_metrics_mod._metrics_initialized = False
    door_metrics_mod._metrics_enabled = False
    door_metrics_mod.door_query_count = None
    door_metrics_mod.door_query_errors = None
    door_metrics_mod.door_query_latency = None


# ---------------------------------------------------------------------------
# Tests: Ingestion Metrics Setup
# ---------------------------------------------------------------------------


class TestMetricsInit:
    """Tests for ingestion metrics initialization."""

    def test_metrics_init_disabled(self):
        """Returns False when KNOWLEDGE_LAYER_METRICS_ENABLED=false."""
        import metrics

        with patch.dict("os.environ", {"KNOWLEDGE_LAYER_METRICS_ENABLED": "false"}):
            result = metrics.setup_metrics()

        assert result is False
        assert metrics._metrics_enabled is False

    def test_metrics_init_disabled_various_values(self):
        """Metrics disabled for various false-ish env values."""
        import metrics

        for val in ("false", "0", "no", "False", "NO"):
            metrics._metrics_initialized = False
            metrics._metrics_enabled = False
            with patch.dict("os.environ", {"KNOWLEDGE_LAYER_METRICS_ENABLED": val}):
                result = metrics.setup_metrics()
            assert result is False, f"Expected False for value '{val}'"

    def test_metrics_init_failopen(self):
        """Falls back gracefully when OTel packages are not installed."""
        import metrics

        with patch.dict("os.environ", {"KNOWLEDGE_LAYER_METRICS_ENABLED": "true"}):
            with patch.object(metrics, "_is_otel_available", return_value=False):
                result = metrics.setup_metrics()

        assert result is False
        assert metrics._metrics_enabled is False

    def test_metrics_init_failopen_on_exception(self):
        """Falls back gracefully if OTel SDK init throws."""
        import metrics

        with patch.dict("os.environ", {"KNOWLEDGE_LAYER_METRICS_ENABLED": "true"}):
            with patch.object(metrics, "_is_otel_available", return_value=True):
                with patch(
                    "builtins.__import__",
                    side_effect=ImportError("missing grpc"),
                ):
                    result = metrics.setup_metrics()

        assert result is False
        assert metrics._metrics_enabled is False

    def test_metrics_init_idempotent(self):
        """Second call to setup_metrics is a no-op."""
        import metrics

        with patch.dict("os.environ", {"KNOWLEDGE_LAYER_METRICS_ENABLED": "false"}):
            metrics.setup_metrics()
            assert metrics._metrics_initialized is True

            result = metrics.setup_metrics()
            assert result is False

    def test_metrics_init_success(self):
        """Metrics initialize successfully when OTel is available."""
        import metrics

        mock_meter = MagicMock()
        mock_meter.create_counter = MagicMock(return_value=MagicMock())
        mock_meter.create_histogram = MagicMock(return_value=MagicMock())

        mock_provider = MagicMock()
        mock_reader = MagicMock()
        mock_exporter = MagicMock()
        mock_resource = MagicMock()

        # Create mock OTel modules (may not be installed in test env)
        mock_metrics_mod = MagicMock()
        mock_metrics_mod.set_meter_provider = MagicMock()
        mock_metrics_mod.get_meter = MagicMock(return_value=mock_meter)

        mock_sdk_metrics = MagicMock()
        mock_sdk_metrics.MeterProvider = MagicMock(return_value=mock_provider)

        mock_sdk_export = MagicMock()
        mock_sdk_export.PeriodicExportingMetricReader = MagicMock(return_value=mock_reader)

        mock_otlp_exporter = MagicMock()
        mock_otlp_exporter.OTLPMetricExporter = MagicMock(return_value=mock_exporter)

        mock_sdk_resources = MagicMock()
        mock_sdk_resources.Resource.create = MagicMock(return_value=mock_resource)

        import_map = {
            "opentelemetry": MagicMock(),
            "opentelemetry.metrics": mock_metrics_mod,
            "opentelemetry.exporter.otlp.proto.grpc.metric_exporter": mock_otlp_exporter,
            "opentelemetry.sdk.metrics": mock_sdk_metrics,
            "opentelemetry.sdk.metrics.export": mock_sdk_export,
            "opentelemetry.sdk.resources": mock_sdk_resources,
        }

        with patch.dict("os.environ", {"KNOWLEDGE_LAYER_METRICS_ENABLED": "true"}):
            with patch.object(metrics, "_is_otel_available", return_value=True):
                with patch.dict("sys.modules", import_map):
                    result = metrics.setup_metrics()

        assert result is True
        assert metrics._metrics_enabled is True
        assert metrics.assets_indexed is not None
        assert metrics.assets_failed is not None
        assert metrics.stage_latency is not None
        assert metrics.ingestion_duration is not None


# ---------------------------------------------------------------------------
# Tests: Record Helpers
# ---------------------------------------------------------------------------


class TestRecordStageComplete:
    """Tests for record_stage_complete."""

    def test_record_stage_complete_emits(self):
        """Counter incremented with correct dimensions on stage complete."""
        import metrics

        mock_counter = MagicMock()
        mock_histogram = MagicMock()
        metrics.assets_indexed = mock_counter
        metrics.stage_latency = mock_histogram

        metrics.record_stage_complete(
            tenant_id="tenant-1",
            stage="clone",
            asset_type="repo",
            latency_ms=1234.5,
        )

        mock_counter.add.assert_called_once_with(
            1, {"tenant_id": "tenant-1", "stage": "clone", "asset_type": "repo"}
        )
        mock_histogram.record.assert_called_once_with(
            1234.5, {"tenant_id": "tenant-1", "stage": "clone"}
        )

    def test_record_stage_complete_noop_when_not_initialized(self):
        """No crash when metrics not initialized (instruments are None)."""
        import metrics

        # Should not raise
        metrics.record_stage_complete(
            tenant_id="t1", stage="clone", asset_type="repo", latency_ms=100.0
        )

    def test_record_stage_complete_failopen_on_exception(self):
        """Exception in metric emission is swallowed (fail-open)."""
        import metrics

        mock_counter = MagicMock()
        mock_counter.add.side_effect = RuntimeError("export failed")
        metrics.assets_indexed = mock_counter

        # Should not raise
        metrics.record_stage_complete(
            tenant_id="t1", stage="clone", asset_type="repo", latency_ms=100.0
        )


class TestRecordStageFailed:
    """Tests for record_stage_failed."""

    def test_record_stage_failed_emits(self):
        """Failure counter incremented with correct dimensions."""
        import metrics

        mock_counter = MagicMock()
        metrics.assets_failed = mock_counter

        metrics.record_stage_failed(tenant_id="tenant-2", stage="deepwiki", asset_type="repo")

        mock_counter.add.assert_called_once_with(
            1, {"tenant_id": "tenant-2", "stage": "deepwiki", "asset_type": "repo"}
        )

    def test_record_stage_failed_noop_when_not_initialized(self):
        """No crash when metrics not initialized."""
        import metrics

        metrics.record_stage_failed(tenant_id="t1", stage="clone", asset_type="repo")


class TestRecordIngestionDuration:
    """Tests for record_ingestion_duration."""

    def test_record_ingestion_duration_emits(self):
        """Histogram recorded with correct dimensions."""
        import metrics

        mock_histogram = MagicMock()
        metrics.ingestion_duration = mock_histogram

        metrics.record_ingestion_duration(
            tenant_id="tenant-1", asset_type="repo", duration_ms=45000.0
        )

        mock_histogram.record.assert_called_once_with(
            45000.0, {"tenant_id": "tenant-1", "asset_type": "repo"}
        )


# ---------------------------------------------------------------------------
# Tests: Cardinality Discipline
# ---------------------------------------------------------------------------


class TestCardinalityDiscipline:
    """Verify that unbounded dimensions are NEVER used as metric attributes."""

    def test_no_asset_id_in_stage_complete(self):
        """record_stage_complete does NOT include asset_id."""
        import metrics

        mock_counter = MagicMock()
        mock_histogram = MagicMock()
        metrics.assets_indexed = mock_counter
        metrics.stage_latency = mock_histogram

        metrics.record_stage_complete(
            tenant_id="t1", stage="clone", asset_type="repo", latency_ms=100.0
        )

        # Check the attributes dict passed to counter.add
        call_attrs = mock_counter.add.call_args[0][1]
        assert "asset_id" not in call_attrs
        assert "run_id" not in call_attrs
        assert "repo_name" not in call_attrs
        assert "project_id" not in call_attrs

    def test_no_asset_id_in_stage_failed(self):
        """record_stage_failed does NOT include asset_id."""
        import metrics

        mock_counter = MagicMock()
        metrics.assets_failed = mock_counter

        metrics.record_stage_failed(tenant_id="t1", stage="clone", asset_type="repo")

        call_attrs = mock_counter.add.call_args[0][1]
        assert "asset_id" not in call_attrs
        assert "run_id" not in call_attrs
        assert "repo_name" not in call_attrs
        assert "project_id" not in call_attrs

    def test_no_unbounded_dims_in_ingestion_duration(self):
        """record_ingestion_duration does NOT include unbounded dimensions."""
        import metrics

        mock_histogram = MagicMock()
        metrics.ingestion_duration = mock_histogram

        metrics.record_ingestion_duration(tenant_id="t1", asset_type="repo", duration_ms=5000.0)

        call_attrs = mock_histogram.record.call_args[0][1]
        assert "asset_id" not in call_attrs
        assert "run_id" not in call_attrs
        assert "repo_name" not in call_attrs
        assert "project_id" not in call_attrs


# ---------------------------------------------------------------------------
# Tests: Door Metrics
# ---------------------------------------------------------------------------


class TestDoorMetrics:
    """Tests for Door service metrics."""

    def test_door_metrics_init_disabled(self):
        """Returns False when KNOWLEDGE_LAYER_METRICS_ENABLED=false."""
        from door.metrics import setup_door_metrics

        with patch.dict("os.environ", {"KNOWLEDGE_LAYER_METRICS_ENABLED": "false"}):
            result = setup_door_metrics()

        assert result is False

    def test_door_metrics_init_failopen(self):
        """Falls back gracefully when OTel packages are not installed."""
        from door import metrics as door_metrics_mod
        from door.metrics import setup_door_metrics

        with patch.dict("os.environ", {"KNOWLEDGE_LAYER_METRICS_ENABLED": "true"}):
            with patch.object(door_metrics_mod, "_is_otel_available", return_value=False):
                result = setup_door_metrics()

        assert result is False

    def test_door_record_query_emits(self):
        """Query counter and latency histogram recorded."""
        from door.metrics import record_query
        from door import metrics as door_metrics_mod

        mock_counter = MagicMock()
        mock_histogram = MagicMock()
        door_metrics_mod.door_query_count = mock_counter
        door_metrics_mod.door_query_latency = mock_histogram

        record_query(tenant_id="t1", verb="search", duration_ms=42.5, error=False)

        mock_counter.add.assert_called_once_with(1, {"tenant_id": "t1", "verb": "search"})
        mock_histogram.record.assert_called_once_with(42.5, {"tenant_id": "t1", "verb": "search"})

    def test_door_record_query_error(self):
        """Error counter incremented when error=True."""
        from door.metrics import record_query
        from door import metrics as door_metrics_mod

        mock_counter = MagicMock()
        mock_errors = MagicMock()
        mock_histogram = MagicMock()
        door_metrics_mod.door_query_count = mock_counter
        door_metrics_mod.door_query_errors = mock_errors
        door_metrics_mod.door_query_latency = mock_histogram

        record_query(tenant_id="t1", verb="search", duration_ms=100.0, error=True)

        mock_errors.add.assert_called_once_with(1, {"tenant_id": "t1", "verb": "search"})

    def test_door_record_query_noop_when_not_initialized(self):
        """No crash when Door metrics not initialized."""
        from door.metrics import record_query

        # Should not raise
        record_query(tenant_id="t1", verb="search", duration_ms=100.0, error=False)

    def test_door_record_query_failopen_on_exception(self):
        """Exception in Door metric emission is swallowed (fail-open)."""
        from door.metrics import record_query
        from door import metrics as door_metrics_mod

        mock_counter = MagicMock()
        mock_counter.add.side_effect = RuntimeError("export failed")
        door_metrics_mod.door_query_count = mock_counter

        # Should not raise
        record_query(tenant_id="t1", verb="search", duration_ms=100.0, error=False)

    def test_door_no_unbounded_dims(self):
        """record_query does NOT include unbounded dimensions."""
        from door.metrics import record_query
        from door import metrics as door_metrics_mod

        mock_counter = MagicMock()
        mock_histogram = MagicMock()
        door_metrics_mod.door_query_count = mock_counter
        door_metrics_mod.door_query_latency = mock_histogram

        record_query(tenant_id="t1", verb="search", duration_ms=50.0, error=False)

        call_attrs = mock_counter.add.call_args[0][1]
        assert "asset_id" not in call_attrs
        assert "query" not in call_attrs
        assert "repo_name" not in call_attrs


# ---------------------------------------------------------------------------
# Tests: Shutdown
# ---------------------------------------------------------------------------


class TestShutdown:
    """Tests for metrics shutdown."""

    def test_shutdown_noop_when_disabled(self):
        """shutdown_metrics is a no-op when metrics never initialized."""
        import metrics

        # Should not raise
        metrics.shutdown_metrics()

    def test_door_shutdown_noop_when_disabled(self):
        """shutdown_door_metrics is a no-op when metrics never initialized."""
        from door.metrics import shutdown_door_metrics

        # Should not raise
        shutdown_door_metrics()
