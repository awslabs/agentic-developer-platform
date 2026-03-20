"""Prometheus metrics service for monitoring and observability."""

from fastapi import Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest

# Define Prometheus metrics

# Request metrics
REQUESTS_TOTAL = Counter(
    "bedrock_requests_total",
    "Total number of Bedrock API requests",
    ["org_id", "model", "status"],
)

REQUEST_DURATION = Histogram(
    "bedrock_request_duration_seconds",
    "Request duration in seconds",
    ["org_id", "model"],
    buckets=[0.1, 0.25, 0.5, 0.75, 1.0, 2.5, 5.0, 7.5, 10.0, 30.0, 60.0],
)

# Token metrics
TOKENS_TOTAL = Counter(
    "bedrock_tokens_total",
    "Total number of tokens processed",
    ["org_id", "model", "direction"],  # direction: input/output
)

# Connection metrics
ACTIVE_CONNECTIONS = Gauge(
    "bedrock_active_connections",
    "Number of active connections",
    ["org_id"],
)

# Error metrics
ERRORS_TOTAL = Counter(
    "bedrock_errors_total",
    "Total number of errors",
    ["org_id", "error_type"],
)

# Budget metrics
BUDGET_USAGE = Gauge(
    "bedrock_budget_usage_usd",
    "Current budget usage in USD",
    ["org_id", "entity_type", "entity_id", "period"],
)

BUDGET_LIMIT = Gauge(
    "bedrock_budget_limit_usd",
    "Budget limit in USD",
    ["org_id", "entity_type", "entity_id", "period"],
)

# Rate limit metrics
RATE_LIMIT_HITS = Counter(
    "bedrock_rate_limit_hits_total",
    "Total number of rate limit hits",
    ["org_id", "limit_type"],
)

# Pool metrics
POOL_ACCOUNTS_TOTAL = Gauge(
    "bedrock_pool_accounts_total",
    "Total number of pool accounts",
    ["status"],  # healthy/unhealthy
)

POOL_ACCOUNT_REQUESTS = Counter(
    "bedrock_pool_account_requests_total",
    "Total requests per pool account",
    ["account_id"],
)


class MetricsService:
    """
    Service for managing Prometheus metrics.

    Provides methods to:
    - Record request metrics
    - Track token usage
    - Monitor active connections
    - Export metrics in Prometheus format
    """

    def __init__(self):
        """Initialize metrics service."""
        pass

    def record_request(
        self,
        org_id: str,
        model: str,
        status: str,
        duration_seconds: float,
    ) -> None:
        """
        Record a Bedrock API request.

        Args:
            org_id: Organization ID
            model: Model name/ID
            status: Request status (success/error)
            duration_seconds: Request duration in seconds
        """
        REQUESTS_TOTAL.labels(org_id=org_id, model=model, status=status).inc()
        REQUEST_DURATION.labels(org_id=org_id, model=model).observe(duration_seconds)

    def record_tokens(
        self,
        org_id: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
    ) -> None:
        """
        Record token usage.

        Args:
            org_id: Organization ID
            model: Model name/ID
            input_tokens: Number of input tokens
            output_tokens: Number of output tokens
        """
        TOKENS_TOTAL.labels(org_id=org_id, model=model, direction="input").inc(input_tokens)
        TOKENS_TOTAL.labels(org_id=org_id, model=model, direction="output").inc(output_tokens)

    def record_error(
        self,
        org_id: str,
        error_type: str,
    ) -> None:
        """
        Record an error.

        Args:
            org_id: Organization ID
            error_type: Type of error
        """
        ERRORS_TOTAL.labels(org_id=org_id, error_type=error_type).inc()

    def increment_active_connections(self, org_id: str) -> None:
        """
        Increment active connection count.

        Args:
            org_id: Organization ID
        """
        ACTIVE_CONNECTIONS.labels(org_id=org_id).inc()

    def decrement_active_connections(self, org_id: str) -> None:
        """
        Decrement active connection count.

        Args:
            org_id: Organization ID
        """
        ACTIVE_CONNECTIONS.labels(org_id=org_id).dec()

    def set_budget_usage(
        self,
        org_id: str,
        entity_type: str,
        entity_id: str,
        period: str,
        usage_usd: float,
        limit_usd: float,
    ) -> None:
        """
        Set budget usage and limit metrics.

        Args:
            org_id: Organization ID
            entity_type: Entity type (org, department, team, user)
            entity_id: Entity ID
            period: Budget period (daily, weekly, monthly)
            usage_usd: Current usage in USD
            limit_usd: Budget limit in USD
        """
        BUDGET_USAGE.labels(org_id=org_id, entity_type=entity_type, entity_id=entity_id, period=period).set(usage_usd)
        BUDGET_LIMIT.labels(org_id=org_id, entity_type=entity_type, entity_id=entity_id, period=period).set(limit_usd)

    def record_rate_limit_hit(
        self,
        org_id: str,
        limit_type: str,
    ) -> None:
        """
        Record a rate limit hit.

        Args:
            org_id: Organization ID
            limit_type: Type of limit hit (rpm, tpm, concurrent)
        """
        RATE_LIMIT_HITS.labels(org_id=org_id, limit_type=limit_type).inc()

    def set_pool_accounts(
        self,
        healthy_count: int,
        unhealthy_count: int,
    ) -> None:
        """
        Set pool account counts.

        Args:
            healthy_count: Number of healthy accounts
            unhealthy_count: Number of unhealthy accounts
        """
        POOL_ACCOUNTS_TOTAL.labels(status="healthy").set(healthy_count)
        POOL_ACCOUNTS_TOTAL.labels(status="unhealthy").set(unhealthy_count)

    def record_pool_account_request(self, account_id: str) -> None:
        """
        Record a request to a pool account.

        Args:
            account_id: AWS account ID
        """
        POOL_ACCOUNT_REQUESTS.labels(account_id=account_id).inc()

    def get_metrics(self) -> bytes:
        """
        Generate Prometheus metrics output.

        Returns:
            Metrics in Prometheus text format
        """
        return generate_latest()

    def get_content_type(self) -> str:
        """
        Get the content type for Prometheus metrics.

        Returns:
            Prometheus content type string
        """
        return CONTENT_TYPE_LATEST


# Singleton instance
_metrics_service: MetricsService | None = None


def get_metrics_service() -> MetricsService:
    """Get the metrics service singleton."""
    global _metrics_service
    if _metrics_service is None:
        _metrics_service = MetricsService()
    return _metrics_service


def metrics_endpoint() -> Response:
    """
    FastAPI endpoint handler for /metrics.

    Returns:
        Response with Prometheus metrics
    """
    service = get_metrics_service()
    return Response(
        content=service.get_metrics(),
        media_type=service.get_content_type(),
    )
