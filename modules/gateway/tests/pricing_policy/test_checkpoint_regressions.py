"""Reproductions for defects found in the independent S1/S2 checkpoint review."""

import copy
from decimal import Decimal

import pytest

from pricing_policy import (
    Confidence,
    EstimateReason,
    RoutingEvidence,
    build_pricing_decision,
    is_openai_model,
    load_snapshot,
    normalize_billing_model_id,
    normalize_usage,
    verify_pricing_decision,
)
from pricing_policy.policy import (
    InvalidPricingDecisionError,
    MissingUsageError,
    _decision_content_hash,
    geography_from_model_prefix,
    parse_rate,
)


def decision(model="openai.gpt-5.6-sol", *, region="us-east-1", geography="in_region", total=1000, **kwargs):
    snapshot = load_snapshot()
    args = {"generation_id": 1, "pointer_revision": 1}
    args.update(kwargs)
    return build_pricing_decision(
        request_id="r",
        org_id="o",
        snapshot=snapshot,
        rows=snapshot.rows_for_model(model),
        usage=normalize_usage(
            {"input_tokens": total, "output_tokens": 1000, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}, api_format="openai"
        ),
        evidence=RoutingEvidence(
            original_model_id=model, billing_model_id=model, endpoint_region=region, geography=geography, served_service_tier_raw="standard"
        ),
        **args,
    )


@pytest.mark.parametrize("model", ["openai.gpt-5.6-terra", "openai.gpt-5.6-luna"])
def test_india_profile_is_openai_and_has_published_geo_rows(model):
    assert is_openai_model("in." + model)
    assert normalize_billing_model_id("in." + model) == model
    assert geography_from_model_prefix("in." + model) == "geo_cris"
    for region in ("ap-south-1", "ap-south-2"):
        result = decision(model, region=region, geography="geo_cris")
        assert result.confidence == Confidence.VERIFIED
        assert result.variant_key == (model, "geo_cris", "standard", "short", region)


def test_individually_supported_dimensions_do_not_verify_unsupported_combination():
    result = decision("openai.gpt-5.4", region="us-gov-west-1", geography="in_region")
    assert result.confidence == Confidence.ESTIMATED
    assert EstimateReason.UNSUPPORTED_VARIANT in result.estimate_reasons
    assert EstimateReason.UNSUPPORTED_REGION in result.estimate_reasons


def test_missing_endpoint_region_is_estimated():
    result = decision(region=None)
    assert result.confidence == Confidence.ESTIMATED
    assert EstimateReason.UNCONFIRMED_REGION in result.estimate_reasons


@pytest.mark.parametrize(("model", "expected"), [("openai.gpt-5.4", "1.67475"), ("openai.gpt-5.5", "3.34950")])
def test_current_official_long_context_prices(model, expected):
    # Manually: 300k * $5.50/1M + 1k * $24.75/1M; GPT-5.5 doubles both.
    result = decision(model, total=300000)
    assert result.variant_key[3] == "long"
    assert Decimal(result.exact_cost_usd) == Decimal(expected)
    assert result.confidence == Confidence.VERIFIED


def test_govcloud_short_only_context_does_not_borrow_commercial_long_rate():
    result = decision("openai.gpt-5.4", total=300000, region="us-gov-west-1", geography="govcloud")
    assert result.variant_key == ("openai.gpt-5.4", "govcloud", "standard", "short", "us-gov-west-1")
    assert EstimateReason.UNSUPPORTED_CONTEXT in result.estimate_reasons
    assert result.confidence == Confidence.ESTIMATED
    assert Decimal(result.exact_cost_usd) == Decimal("1.0098")


def test_current_daybreak_rate_and_region():
    result = decision("openai.gpt-daybreak-blue-5.6-sol", region="us-east-2")
    assert Decimal(result.exact_cost_usd) == Decimal("0.0264")
    assert result.confidence == Confidence.VERIFIED
    assert not load_snapshot().rows_for_model("openai.daybreak-blue-5.6-sol")


@pytest.mark.parametrize("value", ["10000", "1E100", "99999.9999999999"])
def test_rate_integral_digits_fit_numeric_14_10(value):
    with pytest.raises(ValueError, match="precision"):
        parse_rate(value)
    assert parse_rate("9999.9999999999") == Decimal("9999.9999999999")


@pytest.mark.parametrize("bad", ["Infinity", "-Infinity", "NaN", float("inf"), float("nan"), Decimal("Infinity")])
def test_nonfinite_counters_follow_missing_usage_contract(bad):
    with pytest.raises(MissingUsageError):
        normalize_usage({"input_tokens": bad, "output_tokens": 10}, api_format="openai")
    usage = normalize_usage({"input_tokens": 100, "output_tokens": 10, "cache_read_input_tokens": bad}, api_format="openai")
    assert not usage.valid
    assert usage.uncached_input_tokens == 100
    assert EstimateReason.INVALID_USAGE_COUNTERS in usage.estimate_reasons


def test_invalid_raw_counter_is_retained():
    usage = normalize_usage({"input_tokens": 100, "output_tokens": 10, "cache_read_input_tokens": -100}, api_format="openai")
    assert usage.to_dict()["raw"]["cache_read_input_tokens"] == -100
    assert usage.cache_read_input_tokens == 0


@pytest.mark.parametrize("present", ["cache_read_input_tokens", "cache_creation_input_tokens"])
def test_one_absent_cache_counter_is_not_measured_zero(present):
    usage = normalize_usage({"input_tokens": 100, "output_tokens": 10, present: 0}, api_format="openai")
    assert EstimateReason.ABSENT_CACHE_COUNTERS in usage.estimate_reasons


def test_nested_responses_cache_details_count_inclusive_input_once():
    usage = normalize_usage(
        {"input_tokens": 2048, "output_tokens": 256, "input_tokens_details": {"cached_tokens": 1920, "cache_write_tokens": 0}}, api_format="openai"
    )
    assert usage.total_input_tokens == 2048
    assert usage.uncached_input_tokens == 128
    assert usage.cache_read_input_tokens == 1920
    assert not usage.estimate_reasons


@pytest.mark.parametrize("kind", [None, "bundled_snapshot"])
def test_bootstrap_cannot_claim_verified_database_rates(kind):
    result = decision(generation_id=None, pointer_revision=None, source_kind=kind)
    assert result.source_kind == "bundled_snapshot"
    assert result.confidence == Confidence.ESTIMATED
    assert EstimateReason.BOOTSTRAP_FALLBACK in result.estimate_reasons
    assert verify_pricing_decision(result.to_dict(), request_id="r", org_id="o") == Decimal("0.026400")


def test_database_builder_requires_complete_generation_binding():
    with pytest.raises(ValueError, match="generation_id"):
        decision(generation_id=None, pointer_revision=1, source_kind="database")


@pytest.mark.parametrize(
    "field",
    [
        "variant_key",
        "confidence",
        "generation_id",
        "pointer_revision",
        "snapshot_version",
        "source_kind",
        "source",
        "source_url",
        "source_content_sha256",
        "verified_at",
        "routing",
        "estimate_reasons",
        "content_sha256",
    ],
)
def test_durable_decision_requires_all_binding_fields(field):
    payload = decision().to_dict()
    del payload[field]
    with pytest.raises(InvalidPricingDecisionError):
        verify_pricing_decision(payload, request_id="r", org_id="o")


@pytest.mark.parametrize("bad", [1000.9, "1000", True, None, float("inf"), float("nan")])
def test_verifier_does_not_coerce_malformed_normalized_counters(bad):
    payload = decision().to_dict()
    payload["usage"]["total_input_tokens"] = bad
    payload["usage"]["uncached_input_tokens"] = bad
    with pytest.raises(InvalidPricingDecisionError, match="usage"):
        verify_pricing_decision(payload, request_id="r", org_id="o")


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("confidence",), "estimated"),
        (("generation_id",), None),
        (("pointer_revision",), True),
        (("source_kind",), "unknown"),
        (("source_content_sha256",), "bad"),
        (("verified_at",), "2026-09-12"),
        (("routing", "billing_model_id"), "openai.gpt-5.5"),
        (("routing", "geography"), "global_cris"),
        (("routing", "served_service_tier_raw"), "flex"),
        (("rates", "cache_write_policy"), "no_additional_fee"),
        (("rates", "cache_read_price_per_1k_tokens"), "0.0044"),
        (("usage", "input_semantics"), "additive"),
        (("usage", "valid"), False),
    ],
)
def test_verifier_checks_policy_and_binding_even_with_recomputed_diagnostic_hash(path, value):
    payload = copy.deepcopy(decision().to_dict())
    target = payload
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    payload["content_sha256"] = _decision_content_hash(payload)
    with pytest.raises(InvalidPricingDecisionError):
        verify_pricing_decision(payload, request_id="r", org_id="o")


def test_verifier_checks_diagnostic_hash():
    payload = decision().to_dict()
    payload["content_sha256"] = "0" * 64
    with pytest.raises(InvalidPricingDecisionError, match="content_sha256"):
        verify_pricing_decision(payload, request_id="r", org_id="o")
