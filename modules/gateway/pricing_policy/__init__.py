"""Shared Bedrock pricing policy and versioned rate snapshots (Issue #4969).

One dependency-free package, imported by three separate deploy artifacts:

* the gateway image (``src/budget``, ``src/proxy``),
* the budget-usage-tracker Lambda,
* the pricing-refresh Lambda.

It uses only the Python standard library. There is deliberately no database
access here — the gateway reaches V2 storage through its async SQLAlchemy
adapter and the Lambdas through their psycopg2 one, and both hand the rows they
read to this package. Consumers import ``pricing_policy``; they never import each
other's application modules (no import path exists between ``src/`` and
``lambda/``, and this package is what replaces the two hand-maintained rate
literals that gap produced).

Snapshot versions are immutable. A later rate bundle adds a NEW file under
``snapshots/`` and moves ``CURRENT_SNAPSHOT_VERSION``; it never edits a published
one. ``COMPATIBILITY_SNAPSHOT_VERSION`` is pinned separately and must keep
resolving for as long as settlement events written under it are retained, so a
retried old event reproduces its original cost exactly (design §4.3).
"""

from .policy import (
    COMPATIBILITY_SNAPSHOT_VERSION,
    CURRENT_SNAPSHOT_VERSION,
    CacheWritePolicy,
    Confidence,
    ContextTier,
    EstimateReason,
    Geography,
    NormalizedUsage,
    PricingDecision,
    PricingSnapshot,
    RateRow,
    RoutingEvidence,
    ServiceTier,
    Snapshot,
    UnsupportedVariantError,
    VariantKey,
    build_pricing_decision,
    canonical_billing_model_id,
    is_anthropic_model,
    is_openai_model,
    is_v2_priced_model,
    legacy_flat_rates,
    legacy_flat_table,
    load_snapshot,
    normalize_billing_model_id,
    normalize_usage,
    price_from_rate_row,
    quantize_ledger,
    resolve_curated_non_openai,
    select_rate_row,
    verify_pricing_decision,
)

__all__ = [
    "COMPATIBILITY_SNAPSHOT_VERSION",
    "CURRENT_SNAPSHOT_VERSION",
    "CacheWritePolicy",
    "Confidence",
    "ContextTier",
    "EstimateReason",
    "Geography",
    "NormalizedUsage",
    "PricingDecision",
    "PricingSnapshot",
    "RateRow",
    "RoutingEvidence",
    "ServiceTier",
    "Snapshot",
    "UnsupportedVariantError",
    "VariantKey",
    "build_pricing_decision",
    "canonical_billing_model_id",
    "is_anthropic_model",
    "is_v2_priced_model",
    "is_openai_model",
    "legacy_flat_rates",
    "legacy_flat_table",
    "load_snapshot",
    "normalize_billing_model_id",
    "normalize_usage",
    "price_from_rate_row",
    "quantize_ledger",
    "resolve_curated_non_openai",
    "select_rate_row",
    "verify_pricing_decision",
]
