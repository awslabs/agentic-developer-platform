"""
Hardcoded Model Pricing Fallback for Lambda Functions.

Rates used when the active V2 pricing generation is unavailable — a cold
deployment, or a database the Lambda cannot reach. This is the settlement
authority's bootstrap table: its values write ``budget_usage.total_cost_usd`` and
``usage_logs.cost_usd``.

Issue #234: Budget Usage Tracking Lambda
Issue #1486: Added cache_read_input/cache_creation_input rates and new model IDs
Issue #4592: Added Claude Opus 5, Sonnet 4.5 and bare-id Sonnet 4.6 entries
Issue #4969: The rates are no longer written here.

Until #4969 this module owned a hand-maintained literal, and so did
``src/budget/pricing.py``, because no import path exists between ``src/`` and
``lambda/``. Two independently edited tables priced the same traffic and drifted:
GPT-5.6 Luna was billing 5.00x its published rate. Both now derive from the
shared ``pricing_policy`` snapshot, which ships in this Lambda's zip as well as
the gateway image, so there is exactly one place a rate can be wrong.

``MODEL_PRICING``, ``resolve_model_id``, ``get_model_pricing`` and
``calculate_cost`` keep their signatures — the tracker handler and its tests call
them — but the numbers behind them come from the snapshot.

Every Claude entry still carries all four keys (input, output, cache_read_input,
cache_creation_input). Agent traffic is cache-dominated, and the "default" row has
no cache rates at all, so a base-rate-only entry misprices the majority of the
tokens it is supposed to fix. The snapshot's curated section preserves those
four-key entries verbatim (design §7).

Source: the versioned snapshot under ``pricing_policy/snapshots/``, itself
verified against AWS Bedrock publications — see design §10.
"""

import logging
from decimal import Decimal
from typing import Any

from pricing_policy import legacy_flat_rates, legacy_flat_table

logger = logging.getLogger(__name__)

# Derived from the shared snapshot, built once at import. Same flat shape the
# hand-maintained literal had, including the conservative "default" row.
MODEL_PRICING: dict[str, dict[str, Decimal]] = legacy_flat_table()


def resolve_model_id(model_id: str) -> str:
    """
    Resolve cross-region inference profile model IDs to base model IDs.

    Cross-region inference uses prefixes like:
    - us.anthropic.claude-3-5-sonnet-20241022-v2:0
    - global.anthropic.claude-sonnet-4-20250514-v1:0

    This strips the region prefix to get the base model ID for pricing lookup.

    Args:
        model_id: Model ID potentially with cross-region prefix

    Returns:
        Base model ID without cross-region prefix
    """
    # Strip cross-region inference profile prefixes
    if model_id.startswith("us."):
        return model_id[3:]  # Remove "us."
    elif model_id.startswith("global."):
        return model_id[7:]  # Remove "global."
    elif model_id.startswith("eu."):
        return model_id[3:]  # Remove "eu."
    elif model_id.startswith("apac."):
        return model_id[5:]  # Remove "apac."

    return model_id


def get_model_pricing(model_id: str) -> dict[str, Decimal]:
    """
    Get pricing for a specific model from the fallback table.

    Args:
        model_id: Bedrock model ID (may include cross-region prefix)

    Returns:
        Dict with 'input' and 'output' prices per 1000 tokens
        (may also include 'cache_read_input' and 'cache_creation_input')
    """
    resolved_id = resolve_model_id(model_id)

    # Issue #4969: the lookup itself now lives in the shared package. It applies
    # the same case-insensitive match and the same #4592 suffix-variant retry
    # ('anthropic.claude-opus-4-8' vs '...-4-8-v1', and ':0'-suffixed arrivals for
    # bare keys) that this function used to implement locally — one mechanism, in
    # one place, instead of two copies to keep in step.
    rates, known = legacy_flat_rates(model_id)
    if known:
        return rates

    # Issue #1486: Unknown model — log a WARNING so this is observable.
    # Previously this was silent, causing Opus 4.6 to be priced as Sonnet.
    # The boundary between "known" and "unknown" is unchanged by #4969, so the
    # metric fires on exactly the ids it fired on before.
    logger.warning(
        "Unknown model '%s' (resolved: '%s') — using default pricing. Add this model to the pricing_policy snapshot.",
        model_id,
        resolved_id,
    )
    _emit_unknown_model_metric(resolved_id)

    return rates


# Lazy singleton + per-container dedup for the unknown-model metric. The metric
# is deliberately DIMENSIONLESS: CloudWatch alarms cannot be created on SEARCH()
# expressions, so a per-ModelId dimension would leave the alarm unbuildable —
# and each distinct id would mint a permanent paid custom metric with caller-
# controlled cardinality. The offending id is already in the WARNING log above;
# the metric only needs to say "at least one record mispriced".
_cloudwatch_client = None
_emitted_unknown_ids: set[str] = set()


def _emit_unknown_model_metric(resolved_id: str) -> None:
    """Best-effort alarm signal, at most once per distinct id per container.

    Deduping bounds the hot-path cost when one unknown model produces thousands
    of records in a batch: the alarm threshold is > 0, so one datapoint carries
    the same signal as one per record. The id is only marked emitted on success,
    so a transient publish failure retries on the next record.
    """
    global _cloudwatch_client
    if resolved_id in _emitted_unknown_ids:
        return
    try:
        import boto3

        if _cloudwatch_client is None:
            _cloudwatch_client = boto3.client("cloudwatch")
        _cloudwatch_client.put_metric_data(
            Namespace="ADP/Gateway",
            MetricData=[
                {
                    "MetricName": "UnknownModelPricing",
                    "Value": 1,
                    "Unit": "Count",
                }
            ],
        )
        _emitted_unknown_ids.add(resolved_id)
    except Exception:
        # The silent-swallow variant of this except is how the pre-#4592 metric
        # failed unnoticed for weeks (AccessDenied, eaten). Never fail pricing on
        # a metrics problem, but leave a trace.
        logger.warning("UnknownModelPricing metric publish failed", exc_info=True)


def calculate_cost(
    model_id: str,
    input_tokens: int,
    output_tokens: int,
    pricing_table: dict[str, Any] | None = None,
    cache_read_input_tokens: int = 0,
    cache_creation_input_tokens: int = 0,
) -> Decimal:
    """
    Calculate total cost for a request including prompt-cache token costs.

    Issue #1486: Added cache_read_input_tokens and cache_creation_input_tokens
    parameters. Per AWS Bedrock prompt-caching docs:
    - cache_read is charged at ~0.1x the input rate
    - cache_creation is charged at ~1.25x the input rate
    - total input tokens = input_tokens + cache_read + cache_creation

    Args:
        model_id: Bedrock model ID
        input_tokens: Number of non-cached input tokens
        output_tokens: Number of output tokens
        pricing_table: Optional custom pricing table (for database-sourced pricing)
        cache_read_input_tokens: Tokens served from prompt cache
        cache_creation_input_tokens: Tokens written to prompt cache

    Returns:
        Total cost in USD (Decimal)
    """
    if pricing_table and model_id in pricing_table:
        pricing = pricing_table[model_id]
        input_price = Decimal(str(pricing.get("input", "0.003")))
        output_price = Decimal(str(pricing.get("output", "0.015")))
        # Cache rates: use explicit if available, else derive from input rate
        cache_read_price = Decimal(str(pricing.get("cache_read_input", str(input_price * Decimal("0.1")))))
        cache_creation_price = Decimal(str(pricing.get("cache_creation_input", str(input_price * Decimal("1.25")))))
    else:
        pricing = get_model_pricing(model_id)
        input_price = pricing["input"]
        output_price = pricing["output"]
        # Cache rates: use explicit if available, else derive from input rate
        cache_read_price = pricing.get("cache_read_input", input_price * Decimal("0.1"))
        cache_creation_price = pricing.get("cache_creation_input", input_price * Decimal("1.25"))

    input_cost = (Decimal(input_tokens) / Decimal("1000")) * input_price
    output_cost = (Decimal(output_tokens) / Decimal("1000")) * output_price
    cache_read_cost = (Decimal(cache_read_input_tokens) / Decimal("1000")) * cache_read_price
    cache_creation_cost = (Decimal(cache_creation_input_tokens) / Decimal("1000")) * cache_creation_price

    total_cost = input_cost + output_cost + cache_read_cost + cache_creation_cost

    # Round to 6 decimal places for precision
    return round(total_cost, 6)
