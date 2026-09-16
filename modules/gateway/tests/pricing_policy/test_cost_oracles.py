"""Fixed hand-calculated cost oracles from design note §6 (issue #4969).

Every expected number here is transcribed from the design note's oracle table,
which was hand-calculated from the §10 per-1M rates BEFORE this implementation
existed. They are not outputs of the code under test and must never be
"corrected" to match it: if a test here fails, the pricing math is wrong, not the
oracle. Rederive from §10 by hand before touching an expected value.

All cases are commercial in-region, standard tier, with explicit upstream tier
and region confirmation and measured-zero cache counters unless stated.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from pricing_policy import (
    Confidence,
    ContextTier,
    EstimateReason,
    Geography,
    ServiceTier,
    build_pricing_decision,
    load_snapshot,
    normalize_usage,
)
from pricing_policy.policy import RoutingEvidence

SOL = "openai.gpt-5.6-sol"
ASTRA = "openai.gpt-6-astra"
GPT_55 = "openai.gpt-5.5"
GPT_54 = "openai.gpt-5.4"
OSS_120B = "openai.gpt-oss-120b"


@pytest.fixture(scope="module")
def snapshot():
    return load_snapshot()


def _price(
    snapshot,
    model_id: str,
    *,
    total_input: int,
    output: int,
    cache_read: int | None = 0,
    cache_write: int | None = 0,
    region: str = "us-east-1",
    geography: str = Geography.IN_REGION,
    served_tier: str | None = ServiceTier.STANDARD,
    api_format: str = "openai",
):
    """Build a decision the way the gateway does, from a Responses usage block."""
    usage_block: dict[str, object] = {"input_tokens": total_input, "output_tokens": output}
    if cache_read is not None:
        usage_block["cache_read_input_tokens"] = cache_read
    if cache_write is not None:
        usage_block["cache_creation_input_tokens"] = cache_write

    usage = normalize_usage(usage_block, api_format=api_format)
    evidence = RoutingEvidence(
        original_model_id=model_id,
        billing_model_id=model_id,
        endpoint_region=region,
        geography=geography,
        served_service_tier_raw=served_tier,
    )
    return build_pricing_decision(
        request_id="req-oracle",
        org_id="org-oracle",
        usage=usage,
        evidence=evidence,
        rows=snapshot.rows_for_model(model_id),
        snapshot=snapshot,
        generation_id=1,
        pointer_revision=1,
        source_kind="database",
    )


# ---------------------------------------------------------------------------
# The nine oracles, in design-note order.
# ---------------------------------------------------------------------------


def test_sol_short_no_cache(snapshot):
    """Sol short: T=1,000, O=1,000, no cache → 0.0264.

    Proves the corrected $4.40/$22.00 rates replaced the wrong $5.50/$33.00.
    """
    decision = _price(snapshot, SOL, total_input=1_000, output=1_000)
    assert Decimal(decision.exact_cost_usd) == Decimal("0.0264")
    assert Decimal(decision.ledger_cost_usd) == Decimal("0.026400")
    assert decision.confidence == Confidence.VERIFIED
    assert decision.variant_key[3] == ContextTier.SHORT


def test_sol_aws_documented_example(snapshot):
    """Sol short: T=2,048, C=1,920, W=0, O=256 → 0.00704.

    AWS's own documented Responses example. Input is INCLUSIVE of cached tokens,
    so U = 2048 - 1920 = 128 and the cached tokens are charged once, at the read
    rate. Treating input as additive would bill 2,048 uncached plus 1,920 cached.
    """
    decision = _price(snapshot, SOL, total_input=2_048, output=256, cache_read=1_920)
    assert Decimal(decision.exact_cost_usd) == Decimal("0.00704")
    assert Decimal(decision.ledger_cost_usd) == Decimal("0.007040")
    assert decision.usage["uncached_input_tokens"] == 128
    assert decision.usage["total_input_tokens"] == 2_048
    assert decision.confidence == Confidence.VERIFIED


def test_astra_long_tier_from_raw_total(snapshot):
    """Astra: T=400,000, C=320,000, W=0, O=1,000 → 2.5465.

    80,000 × $22 + 320,000 × $2.20 + 1,000 × $82.50, per 1M. The context tier
    comes from the RAW total, so this is long context even though only 80,000
    tokens are billed as uncached input.
    """
    decision = _price(snapshot, ASTRA, total_input=400_000, output=1_000, cache_read=320_000, region="us-west-2")
    assert Decimal(decision.exact_cost_usd) == Decimal("2.5465")
    assert Decimal(decision.ledger_cost_usd) == Decimal("2.546500")
    assert decision.variant_key[3] == ContextTier.LONG
    assert decision.confidence == Confidence.VERIFIED


def test_sol_full_rate_cache_write(snapshot):
    """Sol short: T=1,000, C=200, W=400, O=100 → 0.006248.

    Written tokens are charged once, at the full 1.25× write rate, and NOT again
    as input: 400 × $5.50 + 200 × $0.44 + 400 × $4.40 + 100 × $22, per 1M.
    """
    decision = _price(snapshot, SOL, total_input=1_000, output=100, cache_read=200, cache_write=400)
    assert Decimal(decision.exact_cost_usd) == Decimal("0.006248")
    assert Decimal(decision.ledger_cost_usd) == Decimal("0.006248")
    assert decision.usage["uncached_input_tokens"] == 400


def test_gpt55_no_additional_fee(snapshot):
    """GPT-5.5: T=1,000, C=200, W=400, O=100 → 0.00781.

    "No additional fee" means no UPLIFT, not free: newly written tokens are still
    charged as ordinary paid input. 800 input-rate tokens (400 uncached + 400
    written) × $5.50 + 200 × $0.55 + 100 × $33, per 1M.
    """
    decision = _price(snapshot, GPT_55, total_input=1_000, output=100, cache_read=200, cache_write=400)
    assert Decimal(decision.exact_cost_usd) == Decimal("0.00781")
    assert Decimal(decision.ledger_cost_usd) == Decimal("0.007810")
    assert decision.rates["cache_write_policy"] == "no_additional_fee"


def test_gpt54_no_additional_fee_distinct_rate(snapshot):
    """GPT-5.4: T=1,000, C=200, W=400, O=100 → 0.003905. Same policy, own rates."""
    decision = _price(snapshot, GPT_54, total_input=1_000, output=100, cache_read=200, cache_write=400)
    assert Decimal(decision.exact_cost_usd) == Decimal("0.003905")
    assert Decimal(decision.ledger_cost_usd) == Decimal("0.003905")
    assert decision.rates["cache_write_policy"] == "no_additional_fee"


def test_gpt_oss_confirmed_flex_tier(snapshot):
    """GPT-OSS-120B confirmed Flex: T=1,000, O=1,000 → 0.000375.

    Uses the actual Flex SKU ($0.075/$0.30), not the standard one.
    """
    decision = _price(snapshot, OSS_120B, total_input=1_000, output=1_000, served_tier=ServiceTier.FLEX)
    assert Decimal(decision.exact_cost_usd) == Decimal("0.000375")
    assert Decimal(decision.ledger_cost_usd) == Decimal("0.000375")
    assert decision.variant_key[2] == ServiceTier.FLEX
    assert decision.confidence == Confidence.VERIFIED


def test_gpt_oss_unconfirmed_tier_is_conservative(snapshot):
    """GPT-OSS-120B unconfirmed tier: T=1,000, O=1,000 → 0.0013125, ledger 0.001313.

    With no confirmed served tier the estimate is the most expensive complete
    published row (Priority, $0.2625/$1.05) rather than an assumed standard. Also
    pins the half-up rounding of the 7th decimal place.
    """
    decision = _price(snapshot, OSS_120B, total_input=1_000, output=1_000, served_tier=None)
    assert Decimal(decision.exact_cost_usd) == Decimal("0.0013125")
    assert Decimal(decision.ledger_cost_usd) == Decimal("0.001313")
    assert decision.confidence == Confidence.ESTIMATED
    assert EstimateReason.UNCONFIRMED_SERVICE_TIER in decision.estimate_reasons
    assert decision.variant_key[2] == ServiceTier.PRIORITY


def test_sol_invalid_overlapping_counters_are_bounded(snapshot):
    """Sol short invalid overlap: T=1,000, raw C=900, raw W=600, O=0 → 0.003476.

    The counters contradict each other (900 + 600 > 1,000). The bounded
    decomposition gives W=600, C=400, U=0 — writes take precedence over the
    discounted read class, and total charged input can never exceed T.
    600 × $5.50 + 400 × $0.44, per 1M.
    """
    decision = _price(snapshot, SOL, total_input=1_000, output=0, cache_read=900, cache_write=600)
    assert Decimal(decision.exact_cost_usd) == Decimal("0.003476")
    assert Decimal(decision.ledger_cost_usd) == Decimal("0.003476")
    assert decision.usage["cache_creation_input_tokens"] == 600
    assert decision.usage["cache_read_input_tokens"] == 400
    assert decision.usage["uncached_input_tokens"] == 0
    assert decision.confidence == Confidence.ESTIMATED
    assert EstimateReason.INVALID_USAGE_COUNTERS in decision.estimate_reasons


# ---------------------------------------------------------------------------
# The wrong rates this release removes.
# ---------------------------------------------------------------------------


def test_sol_no_longer_prices_at_the_wrong_fallback_rate(snapshot):
    """The pre-#4976 literal charged Sol $5.50/$33.00 per 1M — GPT-5.5's rates.

    Guards the specific regression: 1,000 in / 1,000 out cost 0.0385 under the
    wrong figures and must now cost 0.0264, a 31% overcharge removed.
    """
    decision = _price(snapshot, SOL, total_input=1_000, output=1_000)
    assert Decimal(decision.exact_cost_usd) != Decimal("0.0385")
    assert Decimal(decision.exact_cost_usd) == Decimal("0.0264")


def test_converse_additive_input_matches_responses_inclusive(snapshot):
    """Equivalent usage under both conventions must cost exactly the same.

    Responses reports T directly (2,048 inclusive of 1,920 cached); Converse
    reports only the 128 non-cached tokens and the cache counters separately. Same
    request, same decomposition, same cost.
    """
    responses = _price(snapshot, SOL, total_input=2_048, output=256, cache_read=1_920)

    converse_usage = normalize_usage(
        {"inputTokens": 128, "outputTokens": 256, "cacheReadInputTokens": 1_920},
        api_format="bedrock",
    )
    assert converse_usage.total_input_tokens == 2_048
    assert converse_usage.uncached_input_tokens == 128

    converse = build_pricing_decision(
        request_id="req-oracle",
        org_id="org-oracle",
        usage=converse_usage,
        evidence=RoutingEvidence(
            original_model_id=SOL,
            billing_model_id=SOL,
            endpoint_region="us-east-1",
            geography=Geography.IN_REGION,
            served_service_tier_raw=ServiceTier.STANDARD,
        ),
        rows=snapshot.rows_for_model(SOL),
        snapshot=snapshot,
        generation_id=1,
        pointer_revision=1,
        source_kind="database",
    )
    assert Decimal(converse.exact_cost_usd) == Decimal(responses.exact_cost_usd) == Decimal("0.00704")
