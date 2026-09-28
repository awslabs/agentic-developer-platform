"""
CloudWatch Embedded Metric Format (EMF) metrics module for BedrockGateway.

This module provides functions to emit metrics in CloudWatch EMF format,
which is automatically picked up by the CloudWatch agent running in
container environments.

EMF outputs JSON to stdout with a special _aws namespace that CloudWatch
understands and converts to metrics.

Metrics provided:
- RequestCount: Number of requests (per org/model)
- RequestLatencyMs: Request latency in milliseconds
- TokensIn: Input tokens processed
- TokensOut: Output tokens generated
- CostUSD: Cost in USD
- ErrorCount: Number of errors (per org/model)
- PoolHealthy: Number of healthy pool accounts
- PoolUnhealthy: Number of unhealthy pool accounts
- BudgetUtilizationPercent: Budget utilization percentage
- RateLimitRemaining: Remaining rate limit tokens
- AuthExchangeCount: Number of auth token exchanges
"""

import json
import logging
import sys
import time
from typing import Any

from src.shared.logging import get_request_id, org_id_var

logger = logging.getLogger(__name__)

# CloudWatch EMF namespace
NAMESPACE = "BedrockGateway"

# Default dimensions
DEFAULT_DIMENSIONS = ["Environment"]


def _get_timestamp() -> int:
    """Get current timestamp in milliseconds."""
    return int(time.time() * 1000)


def _emit_emf(
    metrics: dict[str, Any],
    dimensions: list[list[str]],
    namespace: str = NAMESPACE,
    timestamp: int | None = None,
) -> None:
    """
    Emit metrics in CloudWatch EMF format.

    Args:
        metrics: Dictionary of metric name -> value
        dimensions: List of dimension sets (each is a list of dimension names)
        namespace: CloudWatch namespace
        timestamp: Optional timestamp in milliseconds
    """
    if timestamp is None:
        timestamp = _get_timestamp()

    # Build EMF structure
    emf_data = {
        "_aws": {
            "Timestamp": timestamp,
            "CloudWatchMetrics": [
                {
                    "Namespace": namespace,
                    "Dimensions": dimensions,
                    "Metrics": [{"Name": name, "Unit": _get_unit(name)} for name in metrics.keys()],
                }
            ],
        }
    }

    # Add metric values
    emf_data.update(metrics)

    # Add standard context
    request_id = get_request_id()
    if request_id:
        emf_data["request_id"] = request_id

    org_id = org_id_var.get()
    if org_id:
        emf_data["org_id"] = org_id

    # Output EMF JSON to stdout (CloudWatch agent picks this up)
    print(json.dumps(emf_data), file=sys.stdout, flush=True)


def _get_unit(metric_name: str) -> str:
    """Get the unit for a metric."""
    units = {
        "RequestCount": "Count",
        "RequestLatencyMs": "Milliseconds",
        "TokensIn": "Count",
        "TokensOut": "Count",
        "CostUSD": "None",  # No standard unit for currency
        "ErrorCount": "Count",
        "PoolHealthy": "Count",
        "PoolUnhealthy": "Count",
        "BudgetUtilizationPercent": "Percent",
        "RateLimitRemaining": "Count",
        "AuthExchangeCount": "Count",
    }
    return units.get(metric_name, "None")


# ============================================================================
# Request Metrics
# ============================================================================


def emit_request_count(
    org_id: str,
    model: str,
    count: int = 1,
    environment: str = "production",
) -> None:
    """
    Emit RequestCount metric.

    Args:
        org_id: Organization ID
        model: Model name/ID
        count: Number of requests (default 1)
        environment: Environment name
    """
    _emit_emf(
        metrics={
            "RequestCount": count,
            "org_id": org_id,
            "model": model,
            "Environment": environment,
        },
        dimensions=[["org_id", "model", "Environment"], ["org_id", "Environment"], ["Environment"]],
    )


def emit_request_latency(
    org_id: str,
    model: str,
    latency_ms: float,
    environment: str = "production",
) -> None:
    """
    Emit RequestLatencyMs metric.

    Args:
        org_id: Organization ID
        model: Model name/ID
        latency_ms: Request latency in milliseconds
        environment: Environment name
    """
    _emit_emf(
        metrics={
            "RequestLatencyMs": latency_ms,
            "org_id": org_id,
            "model": model,
            "Environment": environment,
        },
        dimensions=[["org_id", "model", "Environment"], ["org_id", "Environment"], ["Environment"]],
    )


def emit_tokens(
    org_id: str,
    model: str,
    tokens_in: int,
    tokens_out: int,
    environment: str = "production",
) -> None:
    """
    Emit TokensIn and TokensOut metrics.

    Args:
        org_id: Organization ID
        model: Model name/ID
        tokens_in: Number of input tokens
        tokens_out: Number of output tokens
        environment: Environment name
    """
    _emit_emf(
        metrics={
            "TokensIn": tokens_in,
            "TokensOut": tokens_out,
            "org_id": org_id,
            "model": model,
            "Environment": environment,
        },
        dimensions=[["org_id", "model", "Environment"], ["org_id", "Environment"], ["Environment"]],
    )


def emit_cost(
    org_id: str,
    model: str,
    cost_usd: float,
    environment: str = "production",
) -> None:
    """
    Emit CostUSD metric.

    Args:
        org_id: Organization ID
        model: Model name/ID
        cost_usd: Cost in USD
        environment: Environment name
    """
    _emit_emf(
        metrics={
            "CostUSD": cost_usd,
            "org_id": org_id,
            "model": model,
            "Environment": environment,
        },
        dimensions=[["org_id", "model", "Environment"], ["org_id", "Environment"], ["Environment"]],
    )


def emit_error_count(
    org_id: str,
    model: str,
    error_type: str,
    count: int = 1,
    environment: str = "production",
) -> None:
    """
    Emit ErrorCount metric.

    Args:
        org_id: Organization ID
        model: Model name/ID
        error_type: Type of error
        count: Number of errors (default 1)
        environment: Environment name
    """
    _emit_emf(
        metrics={
            "ErrorCount": count,
            "org_id": org_id,
            "model": model,
            "error_type": error_type,
            "Environment": environment,
        },
        dimensions=[
            ["org_id", "model", "error_type", "Environment"],
            ["org_id", "model", "Environment"],
            ["org_id", "Environment"],
            ["Environment"],
        ],
    )


# ============================================================================
# Pool Metrics
# ============================================================================


def emit_pool_health(
    healthy_count: int,
    unhealthy_count: int,
    environment: str = "production",
) -> None:
    """
    Emit PoolHealthy and PoolUnhealthy metrics.

    Args:
        healthy_count: Number of healthy pool accounts
        unhealthy_count: Number of unhealthy pool accounts
        environment: Environment name
    """
    _emit_emf(
        metrics={
            "PoolHealthy": healthy_count,
            "PoolUnhealthy": unhealthy_count,
            "Environment": environment,
        },
        dimensions=[["Environment"]],
    )


# ============================================================================
# Budget Metrics
# ============================================================================


def emit_budget_utilization(
    org_id: str,
    entity_type: str,
    entity_id: str,
    utilization_percent: float,
    environment: str = "production",
) -> None:
    """
    Emit BudgetUtilizationPercent metric.

    Args:
        org_id: Organization ID
        entity_type: Entity type (user, team, department, organization)
        entity_id: Entity ID
        utilization_percent: Budget utilization percentage
        environment: Environment name
    """
    _emit_emf(
        metrics={
            "BudgetUtilizationPercent": utilization_percent,
            "org_id": org_id,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "Environment": environment,
        },
        dimensions=[
            ["org_id", "entity_type", "Environment"],
            ["org_id", "Environment"],
            ["Environment"],
        ],
    )


# ============================================================================
# Rate Limit Metrics
# ============================================================================


def emit_rate_limit_remaining(
    org_id: str,
    entity_type: str,
    entity_id: str,
    limit_type: str,
    remaining: int,
    environment: str = "production",
) -> None:
    """
    Emit RateLimitRemaining metric.

    Args:
        org_id: Organization ID
        entity_type: Entity type (user, team, department, organization)
        entity_id: Entity ID
        limit_type: Type of limit (rpm, tpm, concurrent)
        remaining: Remaining limit
        environment: Environment name
    """
    _emit_emf(
        metrics={
            "RateLimitRemaining": remaining,
            "org_id": org_id,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "limit_type": limit_type,
            "Environment": environment,
        },
        dimensions=[
            ["org_id", "entity_type", "limit_type", "Environment"],
            ["org_id", "limit_type", "Environment"],
            ["Environment"],
        ],
    )


# ============================================================================
# Auth Metrics
# ============================================================================


def emit_auth_exchange_count(
    org_id: str,
    account_type: str,
    success: bool,
    count: int = 1,
    environment: str = "production",
) -> None:
    """
    Emit AuthExchangeCount metric.

    Args:
        org_id: Organization ID
        account_type: Account type (human, service)
        success: Whether the exchange was successful
        count: Number of exchanges (default 1)
        environment: Environment name
    """
    _emit_emf(
        metrics={
            "AuthExchangeCount": count,
            "org_id": org_id,
            "account_type": account_type,
            "success": str(success).lower(),
            "Environment": environment,
        },
        dimensions=[
            ["org_id", "account_type", "success", "Environment"],
            ["org_id", "account_type", "Environment"],
            ["org_id", "Environment"],
            ["Environment"],
        ],
    )


# ============================================================================
# Composite Metrics (convenience functions)
# ============================================================================


def emit_request_metrics(
    org_id: str,
    model: str,
    latency_ms: float,
    tokens_in: int,
    tokens_out: int,
    cost_usd: float,
    success: bool = True,
    error_type: str | None = None,
    environment: str = "production",
) -> None:
    """
    Emit all request-related metrics in one call.

    Args:
        org_id: Organization ID
        model: Model name/ID
        latency_ms: Request latency in milliseconds
        tokens_in: Number of input tokens
        tokens_out: Number of output tokens
        cost_usd: Cost in USD
        success: Whether the request was successful
        error_type: Type of error (if not successful)
        environment: Environment name
    """
    # Emit all metrics
    emit_request_count(org_id, model, 1, environment)
    emit_request_latency(org_id, model, latency_ms, environment)
    emit_tokens(org_id, model, tokens_in, tokens_out, environment)
    emit_cost(org_id, model, cost_usd, environment)

    if not success and error_type:
        emit_error_count(org_id, model, error_type, 1, environment)
