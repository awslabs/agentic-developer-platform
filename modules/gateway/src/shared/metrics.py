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


def emit_budget_grace_engaged(
    engaged: int,
    environment: str = "production",
) -> None:
    """
    Emit BudgetCheckFailOpenGrace metric (Issue #4075).

    Signals that budget enforcement is currently allowing requests it could not
    verify, because the ledger read is failing and we are inside the bounded
    grace window. This is the alarm that must page on-call: every second it is
    engaged is a second of potentially uncapped spend, and when the window
    expires all enforced paths start denying.

    IMPORTANT: this is emitted with ``engaged=0`` on the healthy path too. A
    metric that only appears during an incident leaves its alarm permanently in
    INSUFFICIENT_DATA and it never transitions — which is the most common way
    this class of alarm ships silently broken. The alarm pairs this with
    ``treat_missing_data = "notBreaching"``.

    Args:
        engaged: 1 while the grace window is engaged, 0 when healthy
        environment: Environment name
    """
    _emit_emf(
        metrics={
            "BudgetCheckFailOpenGrace": engaged,
            "Environment": environment,
        },
        dimensions=[["Environment"]],
    )


def emit_budget_check_failure(
    fault_class: str,
    outcome: str,
    count: int = 1,
    environment: str = "production",
) -> None:
    """
    Emit BudgetCheckFailure metric (Issue #4075).

    Args:
        fault_class: "infrastructure" for transient DB/IAM faults the grace
            window is designed to absorb, or "unexpected" for exception types
            that indicate a code bug. The distinction matters: an unexpected
            fault is deterministic, recurs on every request, and no grace
            window rescues it — so it fails OPEN with a high-severity signal
            rather than permanently downing all inference.
        outcome: "allowed_under_grace", "allowed_fail_open", or "denied"
        count: Number of failures (default 1)
        environment: Environment name
    """
    _emit_emf(
        metrics={
            "BudgetCheckFailure": count,
            "fault_class": fault_class,
            "outcome": outcome,
            "Environment": environment,
        },
        dimensions=[
            ["fault_class", "outcome", "Environment"],
            ["fault_class", "Environment"],
            ["Environment"],
        ],
    )


def emit_run_binding_drift(
    reason: str,
    count: int = 1,
    environment: str = "production",
) -> None:
    """
    Emit BudgetRunBindingDrift metric (Issue #4187).

    Counts requests whose asserted ``X-Agent-RunId`` could NOT be bound to the
    authenticated caller — an unknown run, or one belonging to another identity or
    tenant.

    This is what makes shipping the run cap in shadow mode meaningful. In
    ``shadow`` mode nothing is denied and this metric is the only output: it says
    how much real traffic the deny rule would reject if enabled. Enforce only once
    it sits at zero. In ``enforce`` mode the same signal becomes the denial rate,
    so an unexpected spike after the flip is the rollback trigger.

    Args:
        reason: "unknown_run", "identity_mismatch", "tenant_mismatch", or
            "missing_run_id" — which check refused the binding.
        count: Number of occurrences (default 1)
        environment: Environment name
    """
    _emit_emf(
        metrics={
            "BudgetRunBindingDrift": count,
            "reason": reason,
            "Environment": environment,
        },
        dimensions=[
            ["reason", "Environment"],
            ["Environment"],
        ],
    )


def emit_budget_reservation_outcome(
    outcome: str,
    count: int = 1,
    environment: str = "production",
) -> None:
    """
    Emit BudgetReservationOutcome metric (Issue #4287).

    The live-denominator reservation puts Redis on the HEALTHY hot path of every
    enforced request for the first time — Wave 1 (#4075) only touched Redis after
    a DB read had already failed. That is a new availability surface, so it needs
    its own signal, separate from BudgetCheckFailure: a Redis blip here is NOT a
    ledger fault, does not consume the DB grace window, and never denies.

    ``degraded`` is the one to alarm on. While it is firing, caps are being
    enforced against the lagged settled total only — i.e. exactly the overshoot
    #4287 exists to close is back, silently, until Redis returns.

    Args:
        outcome: "reserved" (live denominator had room), "denied" (in-flight
            spend exhausted the cap), or "degraded" (reservation backend
            unreachable; fell back to the settled-ledger check).
        count: Number of occurrences (default 1)
        environment: Environment name
    """
    _emit_emf(
        metrics={
            "BudgetReservationOutcome": count,
            "outcome": outcome,
            "Environment": environment,
        },
        dimensions=[
            ["outcome", "Environment"],
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
