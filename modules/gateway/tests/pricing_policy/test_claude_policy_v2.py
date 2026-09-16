"""Numerical and replay contracts for the additive Claude pricing policy."""

import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from pricing_policy import (
    EstimateReason,
    RoutingEvidence,
    build_pricing_decision,
    canonical_billing_model_id,
    load_snapshot,
    normalize_usage,
    verify_pricing_decision,
)
from pricing_policy.policy import InvalidPricingDecisionError, _decision_content_hash
from pricing_policy.refresh import canonical_content_hash
from pricing_policy.storage import build_active_generation


def row(**changes):
    original = next(r for r in load_snapshot("2026-09-12.1").rates if r.context_tier == "flat")
    base = replace(
        original,
        model_id="anthropic.claude-test",
        geography="in_region",
        service_tier="standard",
        region="us-east-1",
        input_price_per_1k_tokens=Decimal(".003"),
        output_price_per_1k_tokens=Decimal(".015"),
        cache_read_price_per_1k_tokens=Decimal(".0003"),
        cache_write_price_per_1k_tokens=Decimal(".00375"),
        cache_write_1h_price_per_1k_tokens=Decimal(".006"),
        cache_write_policy="full_rate",
    )
    return replace(base, **changes)


def decision(raw, *, rate=None, ttl=None):
    rate = rate or row()
    evidence = RoutingEvidence(
        original_model_id=rate.model_id,
        billing_model_id=rate.model_id,
        forwarded_model_id=rate.model_id,
        endpoint_region="us-east-1",
        geography="in_region",
        served_service_tier_raw="standard",
    )
    return build_pricing_decision(
        request_id="r",
        org_id="o",
        usage=normalize_usage(raw, api_format="anthropic", cache_write_ttl=ttl),
        evidence=evidence,
        rows=(rate,),
        snapshot=load_snapshot(),
        generation_id=7,
        pointer_revision=9,
    )


def usage(**extra):
    return {"input_tokens": 1000, "output_tokens": 500, "cache_read_input_tokens": 200, "cache_creation_input_tokens": 400, **extra}


def test_mixed_write_durations_have_independent_prices_and_exact_replay():
    d = decision(usage(cache_creation={"ephemeral_5m_input_tokens": 100, "ephemeral_1h_input_tokens": 300}))
    # 1000*.003 + 500*.015 + 200*.0003 + 100*.00375 + 300*.006, all /1000.
    assert d.ledger_cost == Decimal(".012735")
    assert d.usage["total_input_tokens"] == 1600
    assert d.decision_version == 2 and d.policy_version == 2
    assert verify_pricing_decision(d.to_dict(), request_id="r", org_id="o") == d.ledger_cost


def test_missing_duration_is_conservative_and_explicit():
    d = decision(usage())
    assert d.ledger_cost == Decimal(".012960")
    assert d.usage["cache_creation_unconfirmed_input_tokens"] == 400
    assert EstimateReason.UNCONFIRMED_CACHE_WRITE_DURATION in d.estimate_reasons


def test_captured_default_ttl_can_confirm_five_minute_writes():
    d = decision(usage(), ttl="5m")
    assert d.ledger_cost == Decimal(".012060")
    assert d.usage["cache_creation_5m_input_tokens"] == 400
    assert EstimateReason.UNCONFIRMED_CACHE_WRITE_DURATION not in d.estimate_reasons


def test_partial_breakdown_retains_unconfirmed_remainder():
    d = decision(usage(cache_creation={"ephemeral_5m_input_tokens": 100}))
    assert d.usage["cache_creation_5m_input_tokens"] == 100
    assert d.usage["cache_creation_unconfirmed_input_tokens"] == 300
    assert d.ledger_cost == Decimal(".012735")


def test_inconsistent_breakdown_is_bounded_without_extra_input():
    d = decision(usage(cache_creation={"ephemeral_5m_input_tokens": 300, "ephemeral_1h_input_tokens": 300}))
    assert d.usage["cache_creation_input_tokens"] == 400
    assert d.usage["cache_creation_1h_input_tokens"] == 300
    assert d.usage["cache_creation_5m_input_tokens"] == 100
    assert EstimateReason.INVALID_USAGE_COUNTERS in d.estimate_reasons


def test_nested_only_breakdown_supplies_measured_aggregate():
    raw = usage(cache_creation={"ephemeral_5m_input_tokens": 100, "ephemeral_1h_input_tokens": 300})
    del raw["cache_creation_input_tokens"]
    assert decision(raw).ledger_cost == Decimal(".012735")


@pytest.mark.parametrize("mutation", ["decomposition", "effective_rate", "missing_reason", "published_rate"])
def test_tampered_v2_decision_is_rejected_even_with_recomputed_hash(mutation):
    p = decision(usage()).to_dict()
    if mutation == "decomposition":
        p["usage"]["cache_creation_1h_input_tokens"] = 1
    elif mutation == "effective_rate":
        p["rates"]["effective_cache_write_1h_price_per_1k_tokens"] = ".004"
    elif mutation == "missing_reason":
        p["estimate_reasons"].remove(EstimateReason.UNCONFIRMED_CACHE_WRITE_DURATION)
        p["confidence"] = "verified"
    else:
        p["rates"].pop("cache_write_1h_price_per_1k_tokens")
    p["content_sha256"] = _decision_content_hash(p)
    with pytest.raises(InvalidPricingDecisionError):
        verify_pricing_decision(p, request_id="r", org_id="o")


def test_v1_saved_decision_and_generation_hash_are_unchanged():
    old = json.loads(Path(__file__).with_name("claude_v1_compatibility_oracle.json").read_text())
    assert verify_pricing_decision(old["decision"], request_id="v1-fixture", org_id="org-fixture") == Decimal(old["decision"]["ledger_cost_usd"])
    assert canonical_content_hash(load_snapshot("2026-09-12.1").rates) == old["generation_hash"]


@pytest.mark.parametrize("version", [1, 2])
def test_readers_accept_prior_and_combined_generation_policies(version):
    pointer = {
        "current_generation_id": 7,
        "pointer_revision": 9,
        "consumers_enabled": True,
        "generation_status": "validated",
        "schema_version": 2,
        "policy_version": version,
    }
    r = replace(row(), generation_id=7)
    assert build_active_generation(pointer=pointer, rate_rows=[r.__dict__], loaded_at=r.verified_at).policy_version == version


def test_nullable_extension_does_not_change_prior_hash_but_published_rate_does():
    r = row()
    assert canonical_content_hash((r,)) != canonical_content_hash((replace(r, cache_write_1h_price_per_1k_tokens=None),))


def test_reviewed_public_aliases_resolve_to_claude_models():
    assert canonical_billing_model_id("opus48") == "anthropic.claude-opus-4-8"
    assert canonical_billing_model_id("jp.anthropic.claude-opus-4-8") == "anthropic.claude-opus-4-8"


def test_flat_claude_still_marks_context_overflow_from_model_metadata():
    snapshot = load_snapshot()
    r = next(r for r in snapshot.rates if r.model_id == "anthropic.claude-opus-4-8" and r.service_tier == "standard")
    raw = {"input_tokens": 1000001, "output_tokens": 0, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
    assert EstimateReason.UNSUPPORTED_CONTEXT in decision(raw, rate=r).estimate_reasons


def test_context_overflow_cannot_be_certified_by_removing_reason():
    r = next(r for r in load_snapshot().rates if r.model_id == "anthropic.claude-opus-4-8" and r.service_tier == "standard")
    p = decision({"input_tokens": 1000001, "output_tokens": 0, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}, rate=r).to_dict()
    p["estimate_reasons"].remove(EstimateReason.UNSUPPORTED_CONTEXT)
    p["confidence"] = "estimated" if p["estimate_reasons"] else "verified"
    p["content_sha256"] = _decision_content_hash(p)
    with pytest.raises(InvalidPricingDecisionError, match="context overflow"):
        verify_pricing_decision(p, request_id="r", org_id="o")


def test_combined_generation_keeps_openai_decision_version_one():
    r = next(r for r in load_snapshot().rates if r.model_id.startswith("openai.") and r.service_tier == "standard")
    evidence = RoutingEvidence(
        original_model_id=r.model_id,
        billing_model_id=r.model_id,
        forwarded_model_id=r.model_id,
        endpoint_region=r.region,
        geography=r.geography,
        served_service_tier_raw="standard",
    )
    d = build_pricing_decision(
        request_id="r",
        org_id="o",
        usage=normalize_usage(
            {"input_tokens": 10, "output_tokens": 2, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}, api_format="openai"
        ),
        evidence=evidence,
        rows=(r,),
        snapshot=load_snapshot(),
        generation_id=7,
        pointer_revision=9,
    )
    assert (d.decision_version, d.policy_version) == (1, 1)
    assert "cache_creation_1h_input_tokens" not in d.usage
    assert "context_max_input_tokens" not in d.rates
    assert verify_pricing_decision(d.to_dict(), request_id="r", org_id="o") == d.ledger_cost


def test_invalid_nested_write_evidence_stays_json_serializable():
    d = decision(usage(cache_creation={"ephemeral_5m_input_tokens": Decimal("NaN"), "ephemeral_1h_input_tokens": float("inf")}))
    json.dumps(d.to_dict(), allow_nan=False)
    assert EstimateReason.INVALID_USAGE_COUNTERS in d.estimate_reasons


@pytest.mark.parametrize("prefix", ["au.", "jp."])
@pytest.mark.parametrize("version", ["2026-09-12.1", "2026-09-12.2"])
def test_new_geography_aliases_do_not_reprice_legacy_curated_events(prefix, version):
    from pricing_policy import resolve_curated_non_openai

    snapshot = load_snapshot(version)
    rates, known = resolve_curated_non_openai(prefix + "anthropic.claude-opus-4-8", snapshot=snapshot)
    assert not known
    assert rates == {key: Decimal(value) for key, value in snapshot.curated_non_openai["rates"]["default"].items()}
    assert canonical_billing_model_id(prefix + "anthropic.claude-opus-4-8") == "anthropic.claude-opus-4-8"


def test_measured_one_hour_writes_with_unpublished_price_remain_estimated():
    d = decision(usage(), rate=row(cache_write_1h_price_per_1k_tokens=None), ttl="1h")
    assert d.ledger_cost == Decimal(".012060")
    assert EstimateReason.UNPUBLISHED_CACHE_WRITE_1H_RATE in d.estimate_reasons
    assert d.rates["cache_write_1h_price_per_1k_tokens"] is None
    assert verify_pricing_decision(d.to_dict(), request_id="r", org_id="o") == d.ledger_cost


def test_explicit_zero_one_hour_price_is_distinct_from_unpublished():
    d = decision(usage(), rate=row(cache_write_1h_price_per_1k_tokens=Decimal("0")), ttl="1h")
    assert d.ledger_cost == Decimal(".010560")
    assert EstimateReason.UNPUBLISHED_CACHE_WRITE_1H_RATE not in d.estimate_reasons
    assert d.rates["cache_write_1h_price_per_1k_tokens"] == "0"
    assert verify_pricing_decision(d.to_dict(), request_id="r", org_id="o") == d.ledger_cost
