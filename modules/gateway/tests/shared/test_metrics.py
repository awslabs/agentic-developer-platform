"""Tests for CloudWatch EMF metrics module."""

import json
from io import StringIO
from unittest.mock import patch

from src.admin.metrics import metrics_endpoint
from src.shared import metrics as metric_module
from src.shared.logging import clear_request_context, set_request_context
from src.shared.metrics import (
    emit_auth_exchange_count,
    emit_budget_utilization,
    emit_caller_provenance_rejected,
    emit_cost,
    emit_error_count,
    emit_pool_health,
    emit_rate_limit_remaining,
    emit_request_count,
    emit_request_latency,
    emit_request_metrics,
    emit_stream_outcome,
    emit_tokens,
)


class TestEMFFormat:
    """Tests for EMF format compliance."""

    def test_emf_contains_aws_namespace(self):
        """Test that EMF output contains _aws namespace."""
        captured = StringIO()
        with patch("sys.stdout", captured):
            emit_request_count(org_id="test-org", model="test-model")

        output = captured.getvalue().strip()
        data = json.loads(output)

        assert "_aws" in data
        assert "CloudWatchMetrics" in data["_aws"]
        assert "Timestamp" in data["_aws"]

    def test_emf_contains_namespace(self):
        """Test that EMF output contains BedrockGateway namespace."""
        captured = StringIO()
        with patch("sys.stdout", captured):
            emit_request_count(org_id="test-org", model="test-model")

        output = captured.getvalue().strip()
        data = json.loads(output)

        assert data["_aws"]["CloudWatchMetrics"][0]["Namespace"] == "BedrockGateway"

    def test_emf_contains_dimensions(self):
        """Test that EMF output contains dimensions."""
        captured = StringIO()
        with patch("sys.stdout", captured):
            emit_request_count(org_id="test-org", model="test-model")

        output = captured.getvalue().strip()
        data = json.loads(output)

        dimensions = data["_aws"]["CloudWatchMetrics"][0]["Dimensions"]
        assert len(dimensions) > 0
        assert "org_id" in dimensions[0]
        assert "model" in dimensions[0]

    def test_emf_contains_metrics_definition(self):
        """Test that EMF output contains metrics definition."""
        captured = StringIO()
        with patch("sys.stdout", captured):
            emit_request_count(org_id="test-org", model="test-model")

        output = captured.getvalue().strip()
        data = json.loads(output)

        metrics = data["_aws"]["CloudWatchMetrics"][0]["Metrics"]
        assert len(metrics) > 0
        assert metrics[0]["Name"] == "RequestCount"
        assert metrics[0]["Unit"] == "Count"

    def test_export_probe_uses_no_request_dimensions(self):
        captured = StringIO()
        with patch("sys.stdout", captured):
            emit_request_count(org_id="test-org", model="test-model")

        data = json.loads(captured.getvalue())
        probe = data["_aws"]["CloudWatchMetrics"][1]
        assert probe["Namespace"] == "BedrockGateway"
        assert probe["Dimensions"] == [[]]
        assert probe["Metrics"] == [{"Name": "ExporterEmitAttempt", "Unit": "Count"}]
        assert data["ExporterEmitAttempt"] == 1

    def test_failed_emf_write_does_not_interrupt_request_or_disclose_context(self):
        class BrokenOutput:
            def write(self, value):
                raise OSError("secret from output channel")

            def flush(self):
                raise OSError("secret from output channel")

        warning = StringIO()
        with patch("sys.stdout", BrokenOutput()), patch("sys.stderr", warning):
            emit_request_count(org_id="sensitive-tenant", model="test-model")

        assert warning.getvalue() == "gateway metric EMF write failed; export unverified\n"

    def test_stream_outcome_rejects_untrusted_labels_and_context(self):
        captured = StringIO()
        set_request_context(request_id="private-request", org_id="private-tenant")
        try:
            with patch("sys.stdout", captured):
                emit_stream_outcome("private-prompt-and-secret")
        finally:
            clear_request_context()

        data = json.loads(captured.getvalue())
        assert data["stream_outcome"] == "other"
        assert data["StreamOutcome"] == 1
        assert data["_aws"]["CloudWatchMetrics"][0]["Dimensions"] == [["stream_outcome"]]
        assert "private-request" not in captured.getvalue()
        assert "private-tenant" not in captured.getvalue()
        assert "private-prompt-and-secret" not in captured.getvalue()

    def test_local_exporter_failures_remain_queryable_without_emf(self, monkeypatch):
        class BrokenOutput:
            def write(self, value):
                raise OSError("private-secret")

            def flush(self):
                raise OSError("private-secret")

        monkeypatch.setattr(metric_module, "_last_export_warning_at", None)
        attempts = metric_module.EMF_WRITE_ATTEMPTS._value.get()
        failures = metric_module.EMF_WRITE_FAILURES._value.get()
        last_success = metric_module.EMF_LAST_SUCCESS._value.get()
        warning = StringIO()
        with patch("sys.stdout", BrokenOutput()), patch("sys.stderr", warning):
            emit_stream_outcome("transport_error")
            emit_stream_outcome("transport_error")

        assert metric_module.EMF_WRITE_ATTEMPTS._value.get() == attempts + 2
        assert metric_module.EMF_WRITE_FAILURES._value.get() == failures + 2
        assert metric_module.EMF_LAST_SUCCESS._value.get() == last_success
        assert warning.getvalue() == "gateway metric EMF write failed; export unverified\n"
        scrape = metrics_endpoint().body
        assert b"bedrock_gateway_emf_write_attempts_total" in scrape
        assert b"bedrock_gateway_emf_write_failures_total" in scrape
        assert b"bedrock_gateway_emf_last_success_timestamp_seconds" in scrape
        assert b"private-secret" not in scrape

    def test_success_updates_local_export_liveness(self):
        before = metric_module.EMF_LAST_SUCCESS._value.get()
        with patch("sys.stdout", StringIO()):
            emit_stream_outcome("completed")
        assert metric_module.EMF_LAST_SUCCESS._value.get() >= before


class TestRequestMetrics:
    """Tests for request-related metrics."""

    def test_emit_request_count(self):
        """Test emit_request_count metric."""
        captured = StringIO()
        with patch("sys.stdout", captured):
            emit_request_count(org_id="org-123", model="claude-3", count=5)

        output = captured.getvalue().strip()
        data = json.loads(output)

        assert data["RequestCount"] == 5
        assert data["org_id"] == "org-123"
        assert data["model"] == "claude-3"

    def test_emit_request_latency(self):
        """Test emit_request_latency metric."""
        captured = StringIO()
        with patch("sys.stdout", captured):
            emit_request_latency(org_id="org-123", model="claude-3", latency_ms=150.5)

        output = captured.getvalue().strip()
        data = json.loads(output)

        assert data["RequestLatencyMs"] == 150.5
        assert data["org_id"] == "org-123"

    def test_emit_tokens(self):
        """Test emit_tokens metric."""
        captured = StringIO()
        with patch("sys.stdout", captured):
            emit_tokens(org_id="org-123", model="claude-3", tokens_in=100, tokens_out=200)

        output = captured.getvalue().strip()
        data = json.loads(output)

        assert data["TokensIn"] == 100
        assert data["TokensOut"] == 200

    def test_emit_cost(self):
        """Test emit_cost metric."""
        captured = StringIO()
        with patch("sys.stdout", captured):
            emit_cost(org_id="org-123", model="claude-3", cost_usd=0.0025)

        output = captured.getvalue().strip()
        data = json.loads(output)

        assert data["CostUSD"] == 0.0025

    def test_emit_error_count(self):
        """Test emit_error_count metric."""
        captured = StringIO()
        with patch("sys.stdout", captured):
            emit_error_count(
                org_id="org-123",
                model="claude-3",
                error_type="BedrockInvocationError",
                count=3,
            )

        output = captured.getvalue().strip()
        data = json.loads(output)

        assert data["ErrorCount"] == 3
        assert data["error_type"] == "BedrockInvocationError"


class TestPoolMetrics:
    """Tests for pool-related metrics."""

    def test_emit_pool_health(self):
        """Test emit_pool_health metric."""
        captured = StringIO()
        with patch("sys.stdout", captured):
            emit_pool_health(healthy_count=8, unhealthy_count=2)

        output = captured.getvalue().strip()
        data = json.loads(output)

        assert data["PoolHealthy"] == 8
        assert data["PoolUnhealthy"] == 2


class TestBudgetMetrics:
    """Tests for budget-related metrics."""

    def test_emit_budget_utilization(self):
        """Test emit_budget_utilization metric."""
        captured = StringIO()
        with patch("sys.stdout", captured):
            emit_budget_utilization(
                org_id="org-123",
                entity_type="user",
                entity_id="user-456",
                utilization_percent=75.5,
            )

        output = captured.getvalue().strip()
        data = json.loads(output)

        assert data["BudgetUtilizationPercent"] == 75.5
        assert data["entity_type"] == "user"
        assert data["entity_id"] == "user-456"


class TestRateLimitMetrics:
    """Tests for rate limit metrics."""

    def test_emit_rate_limit_remaining(self):
        """Test emit_rate_limit_remaining metric."""
        captured = StringIO()
        with patch("sys.stdout", captured):
            emit_rate_limit_remaining(
                org_id="org-123",
                entity_type="user",
                entity_id="user-456",
                limit_type="rpm",
                remaining=50,
            )

        output = captured.getvalue().strip()
        data = json.loads(output)

        assert data["RateLimitRemaining"] == 50
        assert data["limit_type"] == "rpm"


class TestAuthMetrics:
    """Tests for auth-related metrics."""

    def test_emit_auth_exchange_count_success(self):
        """Test emit_auth_exchange_count metric for success."""
        captured = StringIO()
        with patch("sys.stdout", captured):
            emit_auth_exchange_count(
                org_id="org-123",
                account_type="human",
                success=True,
            )

        output = captured.getvalue().strip()
        data = json.loads(output)

        assert data["AuthExchangeCount"] == 1
        assert data["account_type"] == "human"
        assert data["success"] == "true"

    def test_emit_auth_exchange_count_failure(self):
        """Test emit_auth_exchange_count metric for failure."""
        captured = StringIO()
        with patch("sys.stdout", captured):
            emit_auth_exchange_count(
                org_id="org-123",
                account_type="service",
                success=False,
            )

        output = captured.getvalue().strip()
        data = json.loads(output)

        assert data["success"] == "false"


class TestCompositeMetrics:
    """Tests for composite metric functions."""

    def test_emit_request_metrics(self):
        """Test emit_request_metrics composite function."""
        captured = StringIO()
        with patch("sys.stdout", captured):
            emit_request_metrics(
                org_id="org-123",
                model="claude-3",
                latency_ms=150.0,
                tokens_in=100,
                tokens_out=200,
                cost_usd=0.005,
                success=True,
            )

        output = captured.getvalue()
        lines = [line for line in output.strip().split("\n") if line]

        # Should emit multiple metrics (request count, latency, tokens, cost)
        assert len(lines) == 4

        # Parse and check each metric
        metrics_found = set()
        for line in lines:
            data = json.loads(line)
            if "RequestCount" in data:
                metrics_found.add("RequestCount")
            if "RequestLatencyMs" in data:
                metrics_found.add("RequestLatencyMs")
            if "TokensIn" in data:
                metrics_found.add("Tokens")
            if "CostUSD" in data:
                metrics_found.add("CostUSD")

        assert "RequestCount" in metrics_found
        assert "RequestLatencyMs" in metrics_found
        assert "Tokens" in metrics_found
        assert "CostUSD" in metrics_found

    def test_emit_request_metrics_with_error(self):
        """Test emit_request_metrics with error."""
        captured = StringIO()
        with patch("sys.stdout", captured):
            emit_request_metrics(
                org_id="org-123",
                model="claude-3",
                latency_ms=150.0,
                tokens_in=100,
                tokens_out=200,
                cost_usd=0.005,
                success=False,
                error_type="BedrockInvocationError",
            )

        output = captured.getvalue()
        lines = [line for line in output.strip().split("\n") if line]

        # Should emit 5 metrics (including error count)
        assert len(lines) == 5

        # Check for error count
        error_found = False
        for line in lines:
            data = json.loads(line)
            if "ErrorCount" in data:
                error_found = True
                assert data["error_type"] == "BedrockInvocationError"
        assert error_found


class TestMetricUnits:
    """Tests for metric units."""

    def test_request_count_unit(self):
        """Test that RequestCount has Count unit."""
        captured = StringIO()
        with patch("sys.stdout", captured):
            emit_request_count(org_id="org", model="model")

        data = json.loads(captured.getvalue().strip())
        metrics = data["_aws"]["CloudWatchMetrics"][0]["Metrics"]
        assert metrics[0]["Unit"] == "Count"

    def test_latency_unit(self):
        """Test that RequestLatencyMs has Milliseconds unit."""
        captured = StringIO()
        with patch("sys.stdout", captured):
            emit_request_latency(org_id="org", model="model", latency_ms=100)

        data = json.loads(captured.getvalue().strip())
        metrics = data["_aws"]["CloudWatchMetrics"][0]["Metrics"]
        assert metrics[0]["Unit"] == "Milliseconds"

    def test_utilization_unit(self):
        """Test that BudgetUtilizationPercent has Percent unit."""
        captured = StringIO()
        with patch("sys.stdout", captured):
            emit_budget_utilization(
                org_id="org",
                entity_type="user",
                entity_id="user-1",
                utilization_percent=50,
            )

        data = json.loads(captured.getvalue().strip())
        metrics = data["_aws"]["CloudWatchMetrics"][0]["Metrics"]
        assert metrics[0]["Unit"] == "Percent"


class TestCallerProvenanceMetrics:
    """Tests for caller provenance rejection metrics."""

    def test_rejection_dimensions_are_not_declared_as_metrics(self):
        captured = StringIO()
        with patch("sys.stdout", captured):
            emit_caller_provenance_rejected(
                reason="missing_edge_marker",
                environment="test",
            )

        data = json.loads(captured.getvalue().strip())
        metric_definitions = data["_aws"]["CloudWatchMetrics"][0]["Metrics"]

        assert metric_definitions == [{"Name": "CallerProvenanceRejected", "Unit": "Count"}]
        assert all(isinstance(data[metric["Name"]], int | float) for metric in metric_definitions)
        assert data["reason"] == "missing_edge_marker"
        assert data["Environment"] == "test"
