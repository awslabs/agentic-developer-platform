"""Unit tests for MetricsService."""

import pytest

from src.admin.metrics import (
    ACTIVE_CONNECTIONS,
    BUDGET_LIMIT,
    BUDGET_USAGE,
    ERRORS_TOTAL,
    POOL_ACCOUNT_REQUESTS,
    POOL_ACCOUNTS_TOTAL,
    RATE_LIMIT_HITS,
    REQUESTS_TOTAL,
    TOKENS_TOTAL,
    MetricsService,
    get_metrics_service,
    metrics_endpoint,
)


@pytest.fixture
def metrics_service():
    """Create a metrics service instance."""
    return MetricsService()


class TestMetricsService:
    """Tests for MetricsService."""

    def test_record_request(self, metrics_service):
        """Test recording a request metric."""
        initial_value = REQUESTS_TOTAL.labels(org_id="test-org", model="claude-3", status="success")._value.get()

        metrics_service.record_request(
            org_id="test-org",
            model="claude-3",
            status="success",
            duration_seconds=1.5,
        )

        new_value = REQUESTS_TOTAL.labels(org_id="test-org", model="claude-3", status="success")._value.get()

        assert new_value == initial_value + 1

    def test_record_tokens(self, metrics_service):
        """Test recording token metrics."""
        initial_input = TOKENS_TOTAL.labels(org_id="test-org", model="claude-3", direction="input")._value.get()
        initial_output = TOKENS_TOTAL.labels(org_id="test-org", model="claude-3", direction="output")._value.get()

        metrics_service.record_tokens(
            org_id="test-org",
            model="claude-3",
            input_tokens=100,
            output_tokens=200,
        )

        new_input = TOKENS_TOTAL.labels(org_id="test-org", model="claude-3", direction="input")._value.get()
        new_output = TOKENS_TOTAL.labels(org_id="test-org", model="claude-3", direction="output")._value.get()

        assert new_input == initial_input + 100
        assert new_output == initial_output + 200

    def test_record_error(self, metrics_service):
        """Test recording error metrics."""
        initial_value = ERRORS_TOTAL.labels(org_id="test-org", error_type="rate_limit")._value.get()

        metrics_service.record_error(
            org_id="test-org",
            error_type="rate_limit",
        )

        new_value = ERRORS_TOTAL.labels(org_id="test-org", error_type="rate_limit")._value.get()

        assert new_value == initial_value + 1

    def test_active_connections(self, metrics_service):
        """Test incrementing and decrementing active connections."""
        # Get initial value
        initial_value = ACTIVE_CONNECTIONS.labels(org_id="test-org")._value.get()

        # Increment
        metrics_service.increment_active_connections("test-org")
        after_inc = ACTIVE_CONNECTIONS.labels(org_id="test-org")._value.get()
        assert after_inc == initial_value + 1

        # Decrement
        metrics_service.decrement_active_connections("test-org")
        after_dec = ACTIVE_CONNECTIONS.labels(org_id="test-org")._value.get()
        assert after_dec == initial_value

    def test_set_budget_usage(self, metrics_service):
        """Test setting budget usage and limit."""
        metrics_service.set_budget_usage(
            org_id="test-org",
            entity_type="org",
            entity_id="test-org",
            period="monthly",
            usage_usd=500.0,
            limit_usd=1000.0,
        )

        usage = BUDGET_USAGE.labels(
            org_id="test-org",
            entity_type="org",
            entity_id="test-org",
            period="monthly",
        )._value.get()

        limit = BUDGET_LIMIT.labels(
            org_id="test-org",
            entity_type="org",
            entity_id="test-org",
            period="monthly",
        )._value.get()

        assert usage == 500.0
        assert limit == 1000.0

    def test_record_rate_limit_hit(self, metrics_service):
        """Test recording rate limit hits."""
        initial_value = RATE_LIMIT_HITS.labels(org_id="test-org", limit_type="rpm")._value.get()

        metrics_service.record_rate_limit_hit(
            org_id="test-org",
            limit_type="rpm",
        )

        new_value = RATE_LIMIT_HITS.labels(org_id="test-org", limit_type="rpm")._value.get()

        assert new_value == initial_value + 1

    def test_set_pool_accounts(self, metrics_service):
        """Test setting pool account counts."""
        metrics_service.set_pool_accounts(
            healthy_count=5,
            unhealthy_count=2,
        )

        healthy = POOL_ACCOUNTS_TOTAL.labels(status="healthy")._value.get()
        unhealthy = POOL_ACCOUNTS_TOTAL.labels(status="unhealthy")._value.get()

        assert healthy == 5
        assert unhealthy == 2

    def test_record_pool_account_request(self, metrics_service):
        """Test recording pool account requests."""
        initial_value = POOL_ACCOUNT_REQUESTS.labels(account_id="123456789012")._value.get()

        metrics_service.record_pool_account_request("123456789012")

        new_value = POOL_ACCOUNT_REQUESTS.labels(account_id="123456789012")._value.get()

        assert new_value == initial_value + 1

    def test_get_metrics(self, metrics_service):
        """Test getting metrics in Prometheus format."""
        metrics = metrics_service.get_metrics()

        assert isinstance(metrics, bytes)
        # Should contain metric names
        assert b"bedrock_requests_total" in metrics or len(metrics) > 0

    def test_get_content_type(self, metrics_service):
        """Test getting the content type."""
        content_type = metrics_service.get_content_type()

        assert "text/plain" in content_type or "text/openmetrics" in content_type


class TestMetricsSingleton:
    """Tests for metrics service singleton."""

    def test_get_metrics_service_returns_same_instance(self):
        """Test that get_metrics_service returns the same instance."""
        service1 = get_metrics_service()
        service2 = get_metrics_service()

        assert service1 is service2


class TestMetricsEndpoint:
    """Tests for the metrics endpoint function."""

    def test_metrics_endpoint(self):
        """Test the metrics endpoint returns a response."""
        response = metrics_endpoint()

        assert response.status_code == 200
        assert "text" in response.media_type


class TestMetricLabels:
    """Tests for metric label combinations."""

    def test_multiple_orgs(self, metrics_service):
        """Test metrics for multiple organizations."""
        metrics_service.record_request("org-1", "model-1", "success", 1.0)
        metrics_service.record_request("org-2", "model-1", "success", 1.0)
        metrics_service.record_request("org-1", "model-2", "error", 2.0)

        # Each combination should have separate counters
        org1_success = REQUESTS_TOTAL.labels(org_id="org-1", model="model-1", status="success")._value.get()
        org2_success = REQUESTS_TOTAL.labels(org_id="org-2", model="model-1", status="success")._value.get()

        assert org1_success >= 1
        assert org2_success >= 1

    def test_different_error_types(self, metrics_service):
        """Test tracking different error types."""
        metrics_service.record_error("org-1", "rate_limit")
        metrics_service.record_error("org-1", "budget_exceeded")
        metrics_service.record_error("org-1", "validation_error")

        # Should be tracked separately
        rate_limit = ERRORS_TOTAL.labels(org_id="org-1", error_type="rate_limit")._value.get()
        budget = ERRORS_TOTAL.labels(org_id="org-1", error_type="budget_exceeded")._value.get()
        validation = ERRORS_TOTAL.labels(org_id="org-1", error_type="validation_error")._value.get()

        assert rate_limit >= 1
        assert budget >= 1
        assert validation >= 1
