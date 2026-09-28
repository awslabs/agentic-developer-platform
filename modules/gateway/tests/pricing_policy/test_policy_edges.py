"""Boundary and failure-mode coverage for the shared pricing policy (§6, S1/S4).

Companion to test_cost_oracles.py: that file pins the nine hand-calculated
amounts, this one pins the behaviors around them — context boundaries, absent vs
measured-zero counters, invalid counters, unpublished cache policy, unknown
models, unsupported variants, and the durability contract on pricing decisions.
"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from pricing_policy import (
    CacheWritePolicy,
    Confidence,
    ContextTier,
    EstimateReason,
    Geography,
    RateRow,
    RoutingEvidence,
    ServiceTier,
    UnsupportedVariantError,
    build_pricing_decision,
    is_openai_model,
    load_snapshot,
    normalize_billing_model_id,
    normalize_usage,
    price_from_rate_row,
    quantize_ledger,
    resolve_curated_non_openai,
    select_rate_row,
    verify_pricing_decision,
)
from pricing_policy.policy import (
    COMPATIBILITY_SNAPSHOT_VERSION,
    DECISION_VERSION,
    InvalidPricingDecisionError,
    MissingUsageError,
    geography_from_model_prefix,
    parse_rate,
    select_context_tier,
    staleness_reasons,
)

SOL = "openai.gpt-5.6-sol"
ASTRA = "openai.gpt-6-astra"
CYBER = "openai.gpt-5.6-cyber"
LUNA = "openai.gpt-5.6-luna"
GPT_55 = "openai.gpt-5.5"
OSS_20B = "openai.gpt-oss-20b"
OSS_SG_20B = "openai.gpt-oss-safeguard-20b"


@pytest.fixture(scope="module")
def snapshot():
    return load_snapshot()


def _evidence(model_id: str, **kwargs):
    defaults = {
        "original_model_id": model_id,
        "billing_model_id": model_id,
        "endpoint_region": "us-east-1",
        "geography": Geography.IN_REGION,
        "served_service_tier_raw": ServiceTier.STANDARD,
    }
    defaults.update(kwargs)
    return RoutingEvidence(**defaults)


def _usage(total_input: int, output: int, **kwargs):
    """Usage with MEASURED-ZERO cache counters, matching the §6 oracle convention.

    Pass ``cache_read_input_tokens``/``cache_creation_input_tokens`` to override.
    Use ``_usage_without_cache_counters`` for the absent-counter case — the two are
    deliberately not the same thing.
    """
    block = {
        "input_tokens": total_input,
        "output_tokens": output,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
    }
    block.update(kwargs)
    return normalize_usage(block, api_format="openai")


def _usage_without_cache_counters(total_input: int, output: int):
    return normalize_usage({"input_tokens": total_input, "output_tokens": output}, api_format="openai")


# ---------------------------------------------------------------------------
# Model id normalization
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("us.openai.gpt-5.6-sol", "openai.gpt-5.6-sol"),
        ("global.openai.gpt-6-astra", "openai.gpt-6-astra"),
        ("us-gov.openai.gpt-5.4", "openai.gpt-5.4"),
        ("openai.gpt-oss-120b-1:0", "openai.gpt-oss-120b"),
        ("openai.gpt-5.6-sol", "openai.gpt-5.6-sol"),
        ("anthropic.claude-sonnet-4-5-20250929-v1:0", "anthropic.claude-sonnet-4-5-20250929-v1:0"),
    ],
)
def test_normalize_billing_model_id(raw, expected):
    assert normalize_billing_model_id(raw) == expected


def test_geography_is_not_guessed_from_a_bare_id():
    """A bare id may be invoked in-region OR through a profile.

    Returning None keeps the ambiguity visible so selection marks the decision
    estimated, instead of silently asserting in-region pricing.
    """
    assert geography_from_model_prefix("us.openai.gpt-5.6-sol") == Geography.GEO_CRIS
    assert geography_from_model_prefix("global.openai.gpt-5.6-sol") == Geography.GLOBAL_CRIS
    assert geography_from_model_prefix("us-gov.openai.gpt-5.4") == Geography.GOVCLOUD
    assert geography_from_model_prefix("openai.gpt-5.6-sol") is None


def test_is_openai_model_sees_through_profile_prefixes():
    assert is_openai_model("us.openai.gpt-5.6-sol")
    assert is_openai_model("openai.gpt-oss-20b-1:0")
    assert not is_openai_model("anthropic.claude-sonnet-4-5-20250929-v1:0")


# ---------------------------------------------------------------------------
# Context tiers
# ---------------------------------------------------------------------------


def test_short_long_boundary_is_exact(snapshot):
    """272,000 is short; 272,001 is long. The threshold is inclusive."""
    rows = snapshot.rows_for_model(SOL)
    assert select_context_tier(272_000, rows, short_threshold=272_000) == (ContextTier.SHORT, False)
    assert select_context_tier(272_001, rows, short_threshold=272_000) == (ContextTier.LONG, False)


def test_long_context_overflow_is_estimated_not_certified(snapshot):
    """Beyond the longest published window there is no published rate.

    Sol's long tier tops out at 1,000,000. A larger request still prices at the
    long rate — the tokens must be charged — but the decision says so.
    """
    rows = snapshot.rows_for_model(SOL)
    assert select_context_tier(1_000_000, rows, short_threshold=272_000) == (ContextTier.LONG, False)
    assert select_context_tier(1_000_001, rows, short_threshold=272_000) == (ContextTier.LONG, True)

    decision = build_pricing_decision(
        request_id="r",
        org_id="o",
        usage=_usage(1_000_001, 10),
        evidence=_evidence(SOL),
        rows=rows,
        snapshot=snapshot,
        generation_id=1,
        pointer_revision=1,
    )
    assert decision.confidence == Confidence.ESTIMATED
    assert EstimateReason.UNSUPPORTED_CONTEXT in decision.estimate_reasons


def test_astra_long_window_exceeds_the_others(snapshot):
    """Astra publishes 1,050,000, not the 1,000,000 the other frontier cards use."""
    rows = snapshot.rows_for_model(ASTRA)
    assert select_context_tier(1_050_000, rows, short_threshold=272_000) == (ContextTier.LONG, False)
    assert select_context_tier(1_050_001, rows, short_threshold=272_000) == (ContextTier.LONG, True)


def test_short_only_model_has_no_long_tier(snapshot):
    """Cyber publishes one in-region short table and nothing beyond it."""
    rows = snapshot.rows_for_model(CYBER)
    assert {r.context_tier for r in rows} == {ContextTier.SHORT}
    assert select_context_tier(272_001, rows, short_threshold=272_000) == (ContextTier.SHORT, True)


def test_flat_model_never_gets_a_context_tier(snapshot):
    """The GPT-OSS SKUs publish a single flat rate."""
    for model_id in (OSS_20B, OSS_SG_20B):
        rows = snapshot.rows_for_model(model_id)
        assert {r.context_tier for r in rows} == {ContextTier.FLAT}
        assert select_context_tier(9_000_000, rows, short_threshold=272_000) == (ContextTier.FLAT, False)


# ---------------------------------------------------------------------------
# Usage normalization
# ---------------------------------------------------------------------------


def test_missing_usage_raises_rather_than_billing_zero():
    """#4968's contract: absent usage is an anomaly, not a settled zero."""
    with pytest.raises(MissingUsageError):
        normalize_usage(None, api_format="openai")
    with pytest.raises(MissingUsageError):
        normalize_usage({}, api_format="openai")
    with pytest.raises(MissingUsageError):
        normalize_usage({"input_tokens": 100}, api_format="openai")
    with pytest.raises(MissingUsageError):
        normalize_usage({"output_tokens": 100}, api_format="openai")


def test_absent_cache_counters_differ_from_measured_zero():
    """Both bill the same, but only one of them is certain."""
    absent = _usage_without_cache_counters(100, 10)
    measured = _usage(100, 10)
    assert absent.uncached_input_tokens == measured.uncached_input_tokens == 100
    assert EstimateReason.ABSENT_CACHE_COUNTERS in absent.estimate_reasons
    assert measured.estimate_reasons == ()
    assert absent.raw_cache_read_input_tokens is None
    assert measured.raw_cache_read_input_tokens == 0


@pytest.mark.parametrize("bad", [-1, 1.5, "abc", True, None])
def test_invalid_cache_counters_are_flagged_not_trusted(bad):
    usage = normalize_usage(
        {"input_tokens": 1_000, "output_tokens": 10, "cache_read_input_tokens": bad},
        api_format="openai",
    )
    assert usage.uncached_input_tokens + usage.cache_read_input_tokens + usage.cache_creation_input_tokens == usage.total_input_tokens
    assert not usage.valid
    assert EstimateReason.INVALID_USAGE_COUNTERS in usage.estimate_reasons


@pytest.mark.parametrize("bad", [-5, "not-a-number", 2.5])
def test_invalid_input_or_output_is_missing_usage(bad):
    with pytest.raises(MissingUsageError):
        normalize_usage({"input_tokens": bad, "output_tokens": 10}, api_format="openai")
    with pytest.raises(MissingUsageError):
        normalize_usage({"input_tokens": 10, "output_tokens": bad}, api_format="openai")


def test_decomposition_always_sums_to_total():
    """The invariant that makes over-billing structurally impossible."""
    for read, write in ((0, 0), (500, 0), (0, 500), (400, 400), (900, 600), (5_000, 5_000)):
        usage = normalize_usage(
            {"input_tokens": 1_000, "output_tokens": 0, "cache_read_input_tokens": read, "cache_creation_input_tokens": write},
            api_format="openai",
        )
        assert usage.uncached_input_tokens + usage.cache_read_input_tokens + usage.cache_creation_input_tokens == usage.total_input_tokens
        assert usage.total_input_tokens == 1_000


def test_converse_malformed_cache_counter_does_not_poison_the_total():
    """An unparseable counter contributes zero to the additive total, and is flagged."""
    usage = normalize_usage(
        {"inputTokens": 100, "outputTokens": 10, "cacheReadInputTokens": "garbage"},
        api_format="bedrock",
    )
    assert usage.total_input_tokens == 100
    assert usage.uncached_input_tokens == 100
    assert not usage.valid


# ---------------------------------------------------------------------------
# Precision
# ---------------------------------------------------------------------------


def test_the_three_precision_critical_rates_survive_exactly(snapshot):
    """The rates that scale-6 storage corrupted (design §4.2).

    Cyber cache write 0.0171875 rounded to 0.017188 (0.003% error) and Luna
    GovCloud cache read 0.0000264 to 0.000026 (1.52% error). Both must now be
    exact, along with Safeguard-20B's 0.0002 output.
    """
    cyber = next(r for r in snapshot.rows_for_model(CYBER))
    assert cyber.cache_write_price_per_1k_tokens == Decimal("0.0171875")

    luna_gov_read = {r.cache_read_price_per_1k_tokens for r in snapshot.rows_for_model(LUNA) if r.geography == Geography.GOVCLOUD}
    assert Decimal("0.0000264") in luna_gov_read

    sg_outputs = {r.output_price_per_1k_tokens for r in snapshot.rows_for_model(OSS_SG_20B)}
    assert Decimal("0.0002") in sg_outputs


def test_rate_parsing_refuses_float_input():
    """A float rate is exactly how precision was lost. Refuse the type."""
    with pytest.raises(TypeError, match="decimal string"):
        parse_rate(0.0171875, field_name="cache_write_price_per_1k_tokens")
    assert parse_rate("0.0171875") == Decimal("0.0171875")


def test_rate_parsing_rejects_unstorable_scale_instead_of_rounding():
    """NUMERIC(14,10) cannot hold an 11th decimal place; say so rather than round."""
    assert parse_rate("0.0000000001") == Decimal("0.0000000001")
    with pytest.raises(ValueError, match="scale"):
        parse_rate("0.00000000001")


def test_ledger_quantization_is_half_up():
    assert quantize_ledger(Decimal("0.0013125")) == Decimal("0.001313")
    assert quantize_ledger(Decimal("0.0000005")) == Decimal("0.000001")
    assert quantize_ledger(Decimal("0.0000004")) == Decimal("0.000000")


def test_exact_cost_is_not_pre_quantized(snapshot):
    """Sub-micro-dollar requests keep their exact value alongside the ledger value."""
    decision = build_pricing_decision(
        request_id="r",
        org_id="o",
        usage=_usage(1, 1),
        evidence=_evidence(OSS_20B, served_service_tier_raw=ServiceTier.BATCH),
        rows=snapshot.rows_for_model(OSS_20B),
        snapshot=snapshot,
        generation_id=1,
        pointer_revision=1,
    )
    exact = Decimal(decision.exact_cost_usd)
    assert exact == Decimal("0.000000185")  # (0.000035 + 0.00015) / 1000
    assert Decimal(decision.ledger_cost_usd) == Decimal("0.000000")
    assert exact > 0  # the exact value is preserved even when the ledger rounds to nothing


# ---------------------------------------------------------------------------
# Cache policy tri-state
# ---------------------------------------------------------------------------


def test_gpt_oss_publishes_no_cache_rates_and_stores_null(snapshot):
    """Unpublished means NULL, never a zero that would silently under-bill."""
    for model_id in (OSS_20B, "openai.gpt-oss-120b", OSS_SG_20B, "openai.gpt-oss-safeguard-120b"):
        for row in snapshot.rows_for_model(model_id):
            assert row.cache_read_price_per_1k_tokens is None
            assert row.cache_write_price_per_1k_tokens is None
            assert row.cache_write_policy == CacheWritePolicy.UNPUBLISHED


def test_unpublished_cache_tokens_are_charged_at_the_input_rate(snapshot):
    """The tokens were consumed. Charge them, and mark the decision estimated."""
    rows = snapshot.rows_for_model(OSS_20B)
    decision = build_pricing_decision(
        request_id="r",
        org_id="o",
        usage=_usage(1_000, 0, cache_read_input_tokens=200, cache_creation_input_tokens=300),
        evidence=_evidence(OSS_20B, served_service_tier_raw=ServiceTier.STANDARD),
        rows=rows,
        snapshot=snapshot,
        generation_id=1,
        pointer_revision=1,
    )
    # All 1,000 input tokens at the flat $0.07/1M standard rate.
    assert Decimal(decision.exact_cost_usd) == Decimal("0.00007")
    assert decision.confidence == Confidence.ESTIMATED
    assert EstimateReason.UNPUBLISHED_CACHE_READ_RATE in decision.estimate_reasons
    assert EstimateReason.UNPUBLISHED_CACHE_WRITE_RATE in decision.estimate_reasons


def test_no_additional_fee_stores_the_input_rate_not_zero(snapshot):
    """A zero here would make cache writes free. They are ordinary paid input."""
    for model_id in (GPT_55, "openai.gpt-5.4"):
        for row in snapshot.rows_for_model(model_id):
            assert row.cache_write_policy == CacheWritePolicy.NO_ADDITIONAL_FEE
            assert row.cache_write_price_per_1k_tokens == row.input_price_per_1k_tokens
            assert row.cache_write_price_per_1k_tokens > 0


def test_full_rate_write_is_125_percent_of_input(snapshot):
    """Every full_rate frontier row publishes write at exactly 1.25× input."""
    checked = 0
    for row in (rate for rate in snapshot.rates if is_openai_model(rate.model_id)):
        if row.cache_write_policy != CacheWritePolicy.FULL_RATE:
            continue
        assert row.cache_write_price_per_1k_tokens == row.input_price_per_1k_tokens * Decimal("1.25")
        assert row.cache_read_price_per_1k_tokens == row.input_price_per_1k_tokens / Decimal("10")
        checked += 1
    assert checked > 0


def test_rate_row_rejects_policy_rate_disagreement():
    """The in-memory validator mirrors migration 044's CHECK constraints."""
    base = {
        "model_id": "openai.test",
        "geography": Geography.IN_REGION,
        "service_tier": ServiceTier.STANDARD,
        "context_tier": ContextTier.FLAT,
        "region": "us-east-1",
        "input_price_per_1k_tokens": "0.001",
        "output_price_per_1k_tokens": "0.002",
        "cache_read_price_per_1k_tokens": "0.0001",
        "cache_write_price_per_1k_tokens": "0.00125",
        "cache_write_policy": CacheWritePolicy.FULL_RATE,
        "source": "model_card",
        "source_url": "https://example.invalid/card",
        "source_content_sha256": "0" * 64,
        "verified_at": "2026-09-12T00:00:00+00:00",
    }
    assert RateRow.from_mapping(dict(base)).cache_write_policy == CacheWritePolicy.FULL_RATE

    # unpublished must store NULL, not a value
    with pytest.raises(ValueError, match="unpublished"):
        RateRow.from_mapping({**base, "cache_write_policy": CacheWritePolicy.UNPUBLISHED})

    # full_rate/no_additional_fee require a price
    with pytest.raises(ValueError, match="requires a full write price"):
        RateRow.from_mapping({**base, "cache_write_price_per_1k_tokens": None})

    # no_additional_fee must equal the input rate
    with pytest.raises(ValueError, match="must equal the input rate"):
        RateRow.from_mapping({**base, "cache_write_policy": CacheWritePolicy.NO_ADDITIONAL_FEE})


# ---------------------------------------------------------------------------
# Variant selection
# ---------------------------------------------------------------------------


def test_unknown_model_has_no_rows_at_all(snapshot):
    with pytest.raises(UnsupportedVariantError):
        select_rate_row(
            rows=snapshot.rows_for_model("openai.does-not-exist"),
            usage=_usage(100, 10),
            evidence=_evidence("openai.does-not-exist"),
            short_threshold=272_000,
        )


def test_the_whole_generation_still_prices_off_the_requested_model(snapshot):
    """Passing every row of a generation must not price off another model's rates.

    The selector widens its candidate set whenever a narrower dimension has no
    published row — that is deliberate for geography, tier, region and context.
    Model id is the one dimension where widening is never acceptable, so the
    selector filters on it rather than trusting the caller to have done so.

    Regression: settlement handed `select_rate_row` the full 54-row generation
    because the signature permitted it. gpt-5.5's only row is `flat`/0.0055, but
    the dearest row across all twelve models is a gpt-5.6-cyber `short` row, so
    a 1000-in/1000-out request settled at 0.096250 instead of 0.038500 — a 2.5x
    overcharge on every request, and the decision recorded cyber's variant key
    while its routing block said gpt-5.5.
    """
    usage = _usage(1_000, 1_000)
    evidence = _evidence("openai.gpt-5.5")

    row, _ = select_rate_row(rows=snapshot.rates, usage=usage, evidence=evidence, short_threshold=272_000)
    assert row.model_id == "openai.gpt-5.5", f"priced off {row.model_id}"

    # Identical to pre-filtering by hand, which is the property that makes the
    # filter safe to add: it cannot change any existing caller's answer.
    pre_filtered, _ = select_rate_row(
        rows=snapshot.rows_for_model("openai.gpt-5.5"),
        usage=usage,
        evidence=evidence,
        short_threshold=272_000,
    )
    assert row == pre_filtered

    whole = build_pricing_decision(request_id="r", org_id="o", usage=usage, evidence=evidence, rows=snapshot.rates, snapshot=snapshot)
    assert whole.ledger_cost_usd == "0.038500"
    # 1000/1000 * (0.0055 + 0.033), by hand — not read back from the row that
    # priced it, which would pass whichever row was chosen.
    assert whole.variant_key[0] == "openai.gpt-5.5"


def test_a_model_absent_from_the_generation_raises_rather_than_borrowing_rates(snapshot):
    """The filter must make an absent model unpriceable, not cheaply priceable.

    A curated non-OpenAI model has no rows in a V2 generation at all. If the
    selector fell back to the wider set instead of raising, the caller would get
    a plausible number computed off an unrelated OpenAI row and no signal that
    anything was wrong.
    """
    with pytest.raises(UnsupportedVariantError, match="anthropic.claude-3-5-sonnet-20241022-v2:0"):
        select_rate_row(
            rows=snapshot.rates,
            usage=_usage(1_000, 500),
            evidence=_evidence("anthropic.claude-3-5-sonnet-20241022-v2:0"),
            short_threshold=272_000,
        )


def test_unsupported_region_is_flagged_not_silently_substituted(snapshot):
    """Sol has no in-region eu-west-1 row; its global route does not verify it."""
    decision = build_pricing_decision(
        request_id="r",
        org_id="o",
        usage=_usage(1_000, 1_000),
        evidence=_evidence(SOL, endpoint_region="eu-west-1"),
        rows=snapshot.rows_for_model(SOL),
        snapshot=snapshot,
        generation_id=1,
        pointer_revision=1,
    )
    assert decision.confidence == Confidence.ESTIMATED
    assert EstimateReason.UNSUPPORTED_REGION in decision.estimate_reasons


def test_default_and_auto_tiers_are_not_treated_as_standard(snapshot):
    """No captured AWS contract says default/auto means standard (design §4.4)."""
    for raw in ("default", "auto", None, "", "unknown-tier"):
        decision = build_pricing_decision(
            request_id="r",
            org_id="o",
            usage=_usage(1_000, 1_000),
            evidence=_evidence("openai.gpt-oss-120b", served_service_tier_raw=raw),
            rows=snapshot.rows_for_model("openai.gpt-oss-120b"),
            snapshot=snapshot,
            generation_id=1,
            pointer_revision=1,
        )
        assert decision.confidence == Confidence.ESTIMATED
        assert EstimateReason.UNCONFIRMED_SERVICE_TIER in decision.estimate_reasons
        # conservative: the dearest published tier, not the cheapest
        assert decision.variant_key[2] == ServiceTier.PRIORITY


def test_requested_tier_alone_never_confirms_a_tier(snapshot):
    """Asking for Flex and being served something else must not price as Flex."""
    decision = build_pricing_decision(
        request_id="r",
        org_id="o",
        usage=_usage(1_000, 1_000),
        evidence=_evidence(
            "openai.gpt-oss-120b",
            requested_service_tier=ServiceTier.FLEX,
            served_service_tier_raw=None,
        ),
        rows=snapshot.rows_for_model("openai.gpt-oss-120b"),
        snapshot=snapshot,
        generation_id=1,
        pointer_revision=1,
    )
    assert decision.variant_key[2] == ServiceTier.PRIORITY
    assert decision.confidence == Confidence.ESTIMATED


def test_served_tier_overrides_the_requested_one(snapshot):
    """Requested Flex, served Priority → Priority pricing, and it is verified."""
    decision = build_pricing_decision(
        request_id="r",
        org_id="o",
        usage=_usage(1_000, 1_000),
        evidence=_evidence(
            "openai.gpt-oss-120b",
            requested_service_tier=ServiceTier.FLEX,
            served_service_tier_raw=ServiceTier.PRIORITY,
        ),
        rows=snapshot.rows_for_model("openai.gpt-oss-120b"),
        snapshot=snapshot,
        generation_id=1,
        pointer_revision=1,
    )
    assert decision.variant_key[2] == ServiceTier.PRIORITY
    assert Decimal(decision.exact_cost_usd) == Decimal("0.0013125")
    assert decision.confidence == Confidence.VERIFIED


def test_unconfirmed_geography_picks_the_dearest_published_geography(snapshot):
    """In-region/geo CRIS costs more than global CRIS, so ambiguity resolves there."""
    decision = build_pricing_decision(
        request_id="r",
        org_id="o",
        usage=_usage(1_000, 1_000),
        evidence=_evidence(SOL, geography=None),
        rows=snapshot.rows_for_model(SOL),
        snapshot=snapshot,
        generation_id=1,
        pointer_revision=1,
    )
    assert decision.confidence == Confidence.ESTIMATED
    assert EstimateReason.UNCONFIRMED_GEOGRAPHY in decision.estimate_reasons
    assert Decimal(decision.exact_cost_usd) == Decimal("0.0264")  # in-region, not global's 0.024


def test_selection_never_mixes_rates_from_two_rows(snapshot):
    """The chosen row's own rates are the only ones applied."""
    row, _ = select_rate_row(
        rows=snapshot.rows_for_model(SOL),
        usage=_usage(1_000, 1_000),
        evidence=_evidence(SOL, geography=None),
        short_threshold=272_000,
    )
    _, applied = price_from_rate_row(row, _usage(1_000, 1_000))
    assert applied["input_price_per_1k_tokens"] == "0.0044"
    assert applied["output_price_per_1k_tokens"] == "0.022"


def test_selection_is_deterministic_across_repeated_calls(snapshot):
    """Same inputs, same row — the tie-break has no dependence on dict order."""
    picks = {
        select_rate_row(
            rows=snapshot.rows_for_model(SOL),
            usage=_usage(1_000, 1_000),
            evidence=_evidence(SOL, geography=None, served_service_tier_raw=None),
            short_threshold=272_000,
        )[0].variant_key
        for _ in range(20)
    }
    assert len(picks) == 1


# ---------------------------------------------------------------------------
# Decision durability
# ---------------------------------------------------------------------------


def test_decision_round_trips_through_json(snapshot):
    """The decision travels as JSON in an S3 chat log; it must survive verbatim."""
    decision = build_pricing_decision(
        request_id="req-1",
        org_id="org-1",
        usage=_usage(2_048, 256, cache_read_input_tokens=1_920),
        evidence=_evidence(SOL),
        rows=snapshot.rows_for_model(SOL),
        snapshot=snapshot,
        generation_id=1,
        pointer_revision=1,
    )
    payload = json.loads(json.dumps(decision.to_dict()))
    assert verify_pricing_decision(payload, request_id="req-1", org_id="org-1") == Decimal("0.007040")


def test_verification_reproduces_cost_from_embedded_rates_only(snapshot):
    """No cache, no DB, no snapshot lookup — the event carries everything.

    This is what makes settlement stable across a rate publication, a process
    restart or a pointer rollback between inference and settlement.
    """
    decision = build_pricing_decision(
        request_id="req-1",
        org_id="org-1",
        usage=_usage(1_000, 100, cache_read_input_tokens=200, cache_creation_input_tokens=400),
        evidence=_evidence(SOL),
        rows=snapshot.rows_for_model(SOL),
        snapshot=snapshot,
        generation_id=1,
        pointer_revision=1,
    )
    payload = decision.to_dict()
    # Mutating the live snapshot cannot change a settled amount.
    assert verify_pricing_decision(payload, request_id="req-1", org_id="org-1") == Decimal("0.006248")


def test_decision_bound_to_its_own_request_and_tenant(snapshot):
    """A decision from another request or org is never accepted."""
    payload = build_pricing_decision(
        request_id="req-1",
        org_id="org-1",
        usage=_usage(1_000, 1_000),
        evidence=_evidence(SOL),
        rows=snapshot.rows_for_model(SOL),
        snapshot=snapshot,
        generation_id=1,
        pointer_revision=1,
    ).to_dict()

    with pytest.raises(InvalidPricingDecisionError, match="request_id"):
        verify_pricing_decision(payload, request_id="req-2", org_id="org-1")
    with pytest.raises(InvalidPricingDecisionError, match="org_id"):
        verify_pricing_decision(payload, request_id="req-1", org_id="org-2")


@pytest.mark.parametrize(
    ("mutation", "match"),
    [
        ({"decision_version": 99}, "decision_version"),
        ({"policy_version": 99}, "policy_version"),
        ({"exact_cost_usd": "999.0"}, "does not reproduce"),
        ({"ledger_cost_usd": "999.0"}, "not the quantized"),
        ({"rates": {}}, "rates are malformed"),
        ({"usage": {}}, "usage is malformed"),
    ],
)
def test_corrupt_decisions_fail_explicitly(snapshot, mutation, match):
    """Never silently downgraded to "treat as legacy" — that would reprice it."""
    payload = build_pricing_decision(
        request_id="req-1",
        org_id="org-1",
        usage=_usage(1_000, 1_000),
        evidence=_evidence(SOL),
        rows=snapshot.rows_for_model(SOL),
        snapshot=snapshot,
        generation_id=1,
        pointer_revision=1,
    ).to_dict()
    payload.update(mutation)
    with pytest.raises(InvalidPricingDecisionError, match=match):
        verify_pricing_decision(payload, request_id="req-1", org_id="org-1")


def test_tampered_usage_decomposition_is_rejected(snapshot):
    """Components that do not sum to the total are a corruption signal."""
    payload = build_pricing_decision(
        request_id="req-1",
        org_id="org-1",
        usage=_usage(1_000, 1_000),
        evidence=_evidence(SOL),
        rows=snapshot.rows_for_model(SOL),
        snapshot=snapshot,
        generation_id=1,
        pointer_revision=1,
    ).to_dict()
    payload["usage"]["uncached_input_tokens"] = 5_000
    with pytest.raises(InvalidPricingDecisionError, match="does not decompose"):
        verify_pricing_decision(payload, request_id="req-1", org_id="org-1")


def test_decision_records_full_provenance(snapshot):
    """Every decision must be auditable back to a source document."""
    decision = build_pricing_decision(
        request_id="req-1",
        org_id="org-1",
        usage=_usage(1_000, 1_000),
        evidence=_evidence(SOL),
        rows=snapshot.rows_for_model(SOL),
        snapshot=snapshot,
        generation_id=1,
        pointer_revision=1,
    )
    assert decision.source == "model_card"
    assert decision.source_url.startswith("https://")
    assert len(decision.source_content_sha256) == 64
    assert decision.verified_at == "2026-09-12T00:00:00+00:00"
    assert decision.decision_version == DECISION_VERSION
    assert decision.routing["billing_model_id"] == SOL


def test_compatibility_snapshot_stays_resolvable():
    """Legacy events without a decision are priced from this pinned version.

    It must keep loading independently of CURRENT_SNAPSHOT_VERSION advancing, or
    retried old events would reprice at today's rates.
    """
    assert load_snapshot(COMPATIBILITY_SNAPSHOT_VERSION).snapshot_version == COMPATIBILITY_SNAPSHOT_VERSION


# ---------------------------------------------------------------------------
# Staleness
# ---------------------------------------------------------------------------


def test_rows_older_than_48h_are_estimated():
    now = "2026-09-15T00:00:00+00:00"
    assert staleness_reasons(row_verified_at="2026-09-14T00:00:00+00:00", now_iso=now) == ()
    assert EstimateReason.STALE_RATE_SOURCE in staleness_reasons(row_verified_at="2026-09-12T00:00:00+00:00", now_iso=now)


def test_cache_refresh_failure_after_30_minutes_is_estimated():
    now = "2026-09-15T00:00:00+00:00"
    fresh = "2026-09-14T23:00:00+00:00"
    assert staleness_reasons(row_verified_at=fresh, now_iso=now, cache_failure_minutes=29) == ()
    assert EstimateReason.CACHE_REFRESH_FAILING in staleness_reasons(row_verified_at=fresh, now_iso=now, cache_failure_minutes=31)


# ---------------------------------------------------------------------------
# Non-OpenAI curated policy (#1486 / #4592 preservation)
# ---------------------------------------------------------------------------


def test_claude_four_key_cache_rates_are_preserved(snapshot):
    """The curated explicit cache prices must survive the migration into this package."""
    rates, known = resolve_curated_non_openai("anthropic.claude-sonnet-4-5-20250929-v1:0", snapshot=snapshot)
    assert known
    assert "cache_read_input" in rates
    assert "cache_creation_input" in rates
    assert rates["cache_read_input"] < rates["input"] < rates["cache_creation_input"]


def test_profile_prefixed_claude_ids_resolve(snapshot):
    bare, _ = resolve_curated_non_openai("anthropic.claude-sonnet-4-5-20250929-v1:0", snapshot=snapshot)
    prefixed, known = resolve_curated_non_openai("us.anthropic.claude-sonnet-4-5-20250929-v1:0", snapshot=snapshot)
    assert known
    assert prefixed == bare


def test_suffix_variant_retry_still_works(snapshot):
    """Issue #4592: callers and the table disagree about version suffixes.

    The retry that exists goes bare → ``-v1`` (a caller sending
    ``anthropic.claude-sonnet-4-6`` hits the ``-v1``-keyed row). Pinned in the
    direction the existing implementation actually resolves, verified against
    ``lambda/shared/pricing_fallback.get_model_pricing`` — not the reverse.
    """
    for caller_id, table_key in (
        ("anthropic.claude-sonnet-4-6", "anthropic.claude-sonnet-4-6-v1"),
        ("anthropic.claude-opus-4-8", "anthropic.claude-opus-4-8-v1"),
        ("amazon.titan-text-express", "amazon.titan-text-express-v1"),
    ):
        rates, known = resolve_curated_non_openai(caller_id, snapshot=snapshot)
        expected, _ = resolve_curated_non_openai(table_key, snapshot=snapshot)
        assert known, caller_id
        assert rates == expected, caller_id


def test_unknown_model_falls_through_to_default_and_says_so(snapshot):
    """`known=False` is what keeps the UnknownModelPricing metric reachable."""
    rates, known = resolve_curated_non_openai("meta.some-unreleased-model-v9:0", snapshot=snapshot)
    assert not known
    assert rates["input"] > 0


def test_db_row_overrides_base_rates_but_not_curated_cache_policy(snapshot):
    """The pre-#4976 bug: a three-column DB row erased explicit cache prices.

    Selection used to be by whole-dict truthiness, so any non-empty DB table
    bypassed the curated four-key entries entirely and inferred cache rates from
    a universal multiplier instead. Now the override is per-field.
    """
    model_id = "anthropic.claude-sonnet-4-5-20250929-v1:0"
    curated, _ = resolve_curated_non_openai(model_id, snapshot=snapshot)
    merged, known = resolve_curated_non_openai(
        model_id,
        snapshot=snapshot,
        db_rates={model_id: {"input": "0.009", "output": "0.045"}},
    )
    assert known
    assert merged["input"] == Decimal("0.009")
    assert merged["output"] == Decimal("0.045")
    assert merged["cache_read_input"] == curated["cache_read_input"]
    assert merged["cache_creation_input"] == curated["cache_creation_input"]


def test_nova_behavior_is_unchanged_by_this_release(snapshot):
    """Nova is absent from both pre-#4976 literals, so it resolves to `default`.

    That is the EXISTING behavior and this release must not alter it — the design
    requires pinning resolved non-OpenAI behavior, not improving it. Adding real
    Nova rates is out of scope here (no published inventory was verified for it),
    so this test documents the gap rather than papering over it: `known=False`
    keeps `UnknownModelPricing` firing, which is how the gap stays visible.
    """
    default_rates, _ = resolve_curated_non_openai("definitely-not-a-model", snapshot=snapshot)
    for model_id in ("amazon.nova-pro-v1:0", "amazon.nova-lite-v1:0", "amazon.nova-micro-v1:0"):
        rates, known = resolve_curated_non_openai(model_id, snapshot=snapshot)
        assert not known, model_id
        assert rates == default_rates, model_id


def test_alternate_spellings_never_shadow_curated_cache_rates(snapshot):
    """Regression: a bare-keyed entry must not hide the `-v1` entry's cache rates.

    The two pre-#4976 literals spell three Claude ids differently — the gateway
    bare with input/output only, the settlement Lambda `-v1` with all four keys.
    When they merged into one table, an exact match on the bare spelling shadowed
    the richer entry and dropped its curated cache-read/write prices, which is
    exactly the substitution §7 forbids. Both spellings must resolve identically.
    """
    curated = snapshot.curated_non_openai["rates"]
    pairs = [(k.removesuffix("-v1"), k) for k in curated if k.endswith("-v1") and k.removesuffix("-v1") in curated]
    assert pairs, "expected at least one bare/-v1 spelling pair to guard"
    for bare, versioned in pairs:
        bare_rates, bare_known = resolve_curated_non_openai(bare, snapshot=snapshot)
        ver_rates, ver_known = resolve_curated_non_openai(versioned, snapshot=snapshot)
        assert bare_known and ver_known, bare
        assert bare_rates == ver_rates, bare
        # And the cache policy actually survived rather than both degrading.
        assert "cache_read_input" in bare_rates, bare
        assert "cache_creation_input" in bare_rates, bare


def test_curated_table_has_no_openai_entries(snapshot):
    """OpenAI pricing comes from the variant-dimensioned rates, never from here."""
    assert not [k for k in snapshot.curated_non_openai["rates"] if k.startswith("openai.")]


# ---------------------------------------------------------------------------
# Snapshot integrity
# ---------------------------------------------------------------------------


def test_snapshot_covers_all_fourteen_openai_models_and_required_variants(snapshot):
    assert len([model for model in snapshot.models if is_openai_model(model)]) == 14
    assert len([model for model in snapshot.models if model.startswith("anthropic.")]) == 19
    assert len(snapshot.rates) == len(snapshot.required_variants) == 1652
    present = {r.variant_key for r in snapshot.rates}
    assert present == set(snapshot.required_variants)


def test_every_snapshot_rate_is_positive_and_decimal(snapshot):
    for row in snapshot.rates:
        assert isinstance(row.input_price_per_1k_tokens, Decimal)
        assert row.input_price_per_1k_tokens > 0
        assert row.output_price_per_1k_tokens > row.input_price_per_1k_tokens


def test_snapshot_declares_its_unit_and_provenance(snapshot):
    assert snapshot.provenance["unit"] == "USD per 1000 tokens"
    assert snapshot.provenance["issue"] == 4969
    assert snapshot.short_context_max_input_tokens == 272_000
