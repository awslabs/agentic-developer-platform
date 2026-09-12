"""
Hardcoded Model Pricing Fallback for Lambda Functions.

This module provides a fallback pricing table when the AWS Pricing API is
unavailable or the model_pricing database table is empty. Prices are based
on AWS Bedrock published rates.

Issue #234: Budget Usage Tracking Lambda
Issue #1486: Added cache_read_input/cache_creation_input rates and new model IDs
Issue #4592: Added Claude Opus 5, Sonnet 4.5 and bare-id Sonnet 4.6 entries

Every Claude entry must carry all four keys (input, output, cache_read_input,
cache_creation_input). Agent traffic is cache-dominated, and the "default" row
has no cache rates at all — a base-rate-only entry misprices the majority of
the tokens it is supposed to fix.

Source: AWS Bedrock pricing page (https://aws.amazon.com/bedrock/pricing/)
"""

import logging
from decimal import Decimal
from typing import Any

logger = logging.getLogger(__name__)

# Pricing per 1000 tokens (USD)
# Source: AWS Bedrock pricing page
# Last updated: February 2026
MODEL_PRICING: dict[str, dict[str, Decimal]] = {
    # Claude 3.5 models (latest)
    "anthropic.claude-3-5-sonnet-20241022-v2:0": {
        "input": Decimal("0.003"),
        "output": Decimal("0.015"),
        "cache_read_input": Decimal("0.0003"),
        "cache_creation_input": Decimal("0.00375"),
    },
    "anthropic.claude-3-5-haiku-20241022-v1:0": {
        "input": Decimal("0.0008"),
        "output": Decimal("0.004"),
        "cache_read_input": Decimal("0.00008"),
        "cache_creation_input": Decimal("0.001"),
    },
    # Claude 4 models (2025)
    "anthropic.claude-opus-4-20250514-v1:0": {
        "input": Decimal("0.015"),
        "output": Decimal("0.075"),
        "cache_read_input": Decimal("0.0015"),
        "cache_creation_input": Decimal("0.01875"),
    },
    "anthropic.claude-sonnet-4-20250514-v1:0": {
        "input": Decimal("0.003"),
        "output": Decimal("0.015"),
        "cache_read_input": Decimal("0.0003"),
        "cache_creation_input": Decimal("0.00375"),
    },
    "anthropic.claude-haiku-4-20250514-v1:0": {
        "input": Decimal("0.0008"),
        "output": Decimal("0.004"),
        "cache_read_input": Decimal("0.00008"),
        "cache_creation_input": Decimal("0.001"),
    },
    # Claude 4.x updated models (2025-2026) — Issue #1486, #1622
    # These use version-number naming (no date suffix)
    # Opus 4.x rate: $5/$25 per MTok (corrected from retired $15/$75 Opus 4.1 rate — #1622)
    "anthropic.claude-opus-4-6-v1": {
        "input": Decimal("0.005"),
        "output": Decimal("0.025"),
        "cache_read_input": Decimal("0.0005"),  # 0.1× input
        "cache_creation_input": Decimal("0.00625"),  # 1.25× input
    },
    "anthropic.claude-opus-4-7-v1": {
        "input": Decimal("0.005"),
        "output": Decimal("0.025"),
        "cache_read_input": Decimal("0.0005"),  # 0.1× input
        "cache_creation_input": Decimal("0.00625"),  # 1.25× input
    },
    "anthropic.claude-opus-4-8-v1": {
        "input": Decimal("0.005"),
        "output": Decimal("0.025"),
        "cache_read_input": Decimal("0.0005"),  # 0.1× input
        "cache_creation_input": Decimal("0.00625"),  # 1.25× input
    },
    "anthropic.claude-sonnet-4-6-v1": {
        "input": Decimal("0.003"),
        "output": Decimal("0.015"),
        "cache_read_input": Decimal("0.0003"),
        "cache_creation_input": Decimal("0.00375"),
    },
    # NOTE: bare (un-suffixed) id forms like 'anthropic.claude-sonnet-4-6' and
    # 'anthropic.claude-opus-4-8' are resolved by the suffix-variant retry in
    # get_model_pricing, not by per-id alias rows — one mechanism for the whole
    # class instead of a hand-maintained duplicate row per id shape. Issue #4592.
    # Sonnet 4.5 keeps the dated id form. Sonnet-family rate: $3/$15 per MTok.
    # Issue #4592.
    "anthropic.claude-sonnet-4-5-20250929-v1:0": {
        "input": Decimal("0.003"),
        "output": Decimal("0.015"),
        "cache_read_input": Decimal("0.0003"),
        "cache_creation_input": Decimal("0.00375"),
    },
    # Claude 5 models (2026) — Issue #4592
    # Opus 5 rate VERIFIED against the published price list at $5/$25 per MTok
    # (same as Opus 4.6-4.8 — confirmed, not assumed).
    "anthropic.claude-opus-5": {
        "input": Decimal("0.005"),
        "output": Decimal("0.025"),
        "cache_read_input": Decimal("0.0005"),  # 0.1× input
        "cache_creation_input": Decimal("0.00625"),  # 1.25× input
    },
    # Opus 4.5 dated form — live via the 'opus45' /model alias
    # (src/proxy/model_resolver.py); previously missed the table entirely and
    # billed at the Sonnet-tier default. Opus 4.x rate. Issue #4592.
    "anthropic.claude-opus-4-5-20251101-v1:0": {
        "input": Decimal("0.005"),
        "output": Decimal("0.025"),
        "cache_read_input": Decimal("0.0005"),  # 0.1× input
        "cache_creation_input": Decimal("0.00625"),  # 1.25× input
    },
    "anthropic.claude-haiku-4-5-20251001-v1:0": {
        "input": Decimal("0.0008"),
        "output": Decimal("0.004"),
        "cache_read_input": Decimal("0.00008"),
        "cache_creation_input": Decimal("0.001"),
    },
    # Claude 3 models
    "anthropic.claude-3-opus-20240229-v1:0": {
        "input": Decimal("0.015"),
        "output": Decimal("0.075"),
    },
    "anthropic.claude-3-sonnet-20240229-v1:0": {
        "input": Decimal("0.003"),
        "output": Decimal("0.015"),
    },
    "anthropic.claude-3-haiku-20240307-v1:0": {
        "input": Decimal("0.00025"),
        "output": Decimal("0.00125"),
    },
    # Claude 2.x models (legacy)
    "anthropic.claude-v2:1": {
        "input": Decimal("0.008"),
        "output": Decimal("0.024"),
    },
    "anthropic.claude-v2": {
        "input": Decimal("0.008"),
        "output": Decimal("0.024"),
    },
    "anthropic.claude-instant-v1": {
        "input": Decimal("0.0008"),
        "output": Decimal("0.0024"),
    },
    # Amazon Titan Text models
    "amazon.titan-text-express-v1": {
        "input": Decimal("0.0002"),
        "output": Decimal("0.0006"),
    },
    "amazon.titan-text-lite-v1": {
        "input": Decimal("0.00015"),
        "output": Decimal("0.0002"),
    },
    "amazon.titan-text-premier-v1:0": {
        "input": Decimal("0.0005"),
        "output": Decimal("0.0015"),
    },
    # Amazon Titan Embed models (text)
    "amazon.titan-embed-text-v1": {
        "input": Decimal("0.0001"),
        "output": Decimal("0"),  # Embeddings don't have output tokens
    },
    "amazon.titan-embed-text-v2:0": {
        "input": Decimal("0.00002"),
        "output": Decimal("0"),
    },
    # Cohere models
    "cohere.command-text-v14": {
        "input": Decimal("0.0015"),
        "output": Decimal("0.002"),
    },
    "cohere.command-light-text-v14": {
        "input": Decimal("0.0003"),
        "output": Decimal("0.0006"),
    },
    "cohere.command-r-v1:0": {
        "input": Decimal("0.0005"),
        "output": Decimal("0.0015"),
    },
    "cohere.command-r-plus-v1:0": {
        "input": Decimal("0.003"),
        "output": Decimal("0.015"),
    },
    # Meta Llama models
    "meta.llama3-8b-instruct-v1:0": {
        "input": Decimal("0.0003"),
        "output": Decimal("0.0006"),
    },
    "meta.llama3-70b-instruct-v1:0": {
        "input": Decimal("0.00265"),
        "output": Decimal("0.0035"),
    },
    "meta.llama3-1-8b-instruct-v1:0": {
        "input": Decimal("0.00022"),
        "output": Decimal("0.00022"),
    },
    "meta.llama3-1-70b-instruct-v1:0": {
        "input": Decimal("0.00099"),
        "output": Decimal("0.00099"),
    },
    "meta.llama3-1-405b-instruct-v1:0": {
        "input": Decimal("0.00532"),
        "output": Decimal("0.016"),
    },
    "meta.llama3-2-1b-instruct-v1:0": {
        "input": Decimal("0.0001"),
        "output": Decimal("0.0001"),
    },
    "meta.llama3-2-3b-instruct-v1:0": {
        "input": Decimal("0.00015"),
        "output": Decimal("0.00015"),
    },
    "meta.llama3-2-11b-instruct-v1:0": {
        "input": Decimal("0.00016"),
        "output": Decimal("0.00016"),
    },
    "meta.llama3-2-90b-instruct-v1:0": {
        "input": Decimal("0.00072"),
        "output": Decimal("0.00072"),
    },
    # Mistral models
    "mistral.mistral-7b-instruct-v0:2": {
        "input": Decimal("0.00015"),
        "output": Decimal("0.0002"),
    },
    "mistral.mixtral-8x7b-instruct-v0:1": {
        "input": Decimal("0.00045"),
        "output": Decimal("0.0007"),
    },
    "mistral.mistral-large-2402-v1:0": {
        "input": Decimal("0.004"),
        "output": Decimal("0.012"),
    },
    "mistral.mistral-small-2402-v1:0": {
        "input": Decimal("0.001"),
        "output": Decimal("0.003"),
    },
    # AI21 Jurassic models
    "ai21.j2-ultra-v1": {
        "input": Decimal("0.0125"),
        "output": Decimal("0.0125"),
    },
    "ai21.j2-mid-v1": {
        "input": Decimal("0.0125"),
        "output": Decimal("0.0125"),
    },
    # OpenAI Responses models: match the gateway's existing per-1K-token rates
    # in src/budget/pricing.py. Codex settlement now reaches this Lambda too;
    # falling back to Claude's default would overwrite its recorded cost.
    "openai.gpt-5.5": {
        "input": Decimal("0.0055"),
        "output": Decimal("0.033"),
    },
    "openai.gpt-5.6-sol": {
        "input": Decimal("0.0055"),
        "output": Decimal("0.033"),
    },
    "openai.gpt-5.6-terra": {
        "input": Decimal("0.00275"),
        "output": Decimal("0.0165"),
    },
    "openai.gpt-5.6-luna": {
        "input": Decimal("0.0011"),
        "output": Decimal("0.0066"),
    },
    "openai.gpt-oss-120b": {
        "input": Decimal("0.0001545"),
        "output": Decimal("0.000618"),
    },
    # Default fallback pricing (conservative estimate)
    "default": {
        "input": Decimal("0.003"),
        "output": Decimal("0.015"),
    },
}


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

    if resolved_id in MODEL_PRICING:
        return MODEL_PRICING[resolved_id]

    # Try case-insensitive matching
    model_lower = resolved_id.lower()
    for key in MODEL_PRICING:
        if key.lower() == model_lower:
            return MODEL_PRICING[key]

    # Issue #4592: live callers and the table disagree about version suffixes —
    # model_resolver.py emits bare 'anthropic.claude-opus-4-8' while the table
    # keys 'anthropic.claude-opus-4-8-v1', and ':0'/'-v1'-suffixed forms arrive
    # for ids the table keys bare. Retrying the suffix variants fixes the whole
    # class instead of a hand-maintained alias row per id shape.
    for candidate in (
        f"{resolved_id}-v1",
        resolved_id.removesuffix(":0"),
        resolved_id.removesuffix("-v1:0"),
        resolved_id.removesuffix("-v1"),
    ):
        if candidate != resolved_id and candidate in MODEL_PRICING:
            return MODEL_PRICING[candidate]

    # Issue #1486: Unknown model — log a WARNING so this is observable.
    # Previously this was silent, causing Opus 4.6 to be priced as Sonnet.
    logger.warning(
        "Unknown model '%s' (resolved: '%s') — using default pricing. Add this model to MODEL_PRICING in pricing_fallback.py.",
        model_id,
        resolved_id,
    )
    _emit_unknown_model_metric(resolved_id)

    return MODEL_PRICING["default"]


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
