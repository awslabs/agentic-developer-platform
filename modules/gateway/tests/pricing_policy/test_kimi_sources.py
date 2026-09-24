from decimal import Decimal
from pathlib import Path

import pytest

from pricing_policy.aws_sources import SourceValidationError
from pricing_policy.kimi_sources import KIMI_MODEL, parse_kimi_card
from pricing_policy.policy import is_v2_priced_model, load_snapshot

CARD = Path(__file__).parent / "fixtures/aws/kimi-k3.md"
NOW = "2026-09-24T23:00:00+00:00"


def test_kimi_published_rates_and_complete_endpoint_coverage():
    snapshot = load_snapshot()
    rows = parse_kimi_card(CARD.read_bytes(), snapshot.rates, verified_at=NOW)
    assert len(rows) == 111
    assert all(r.source == "model_card" and r.verified_at == NOW for r in rows)
    lookup = {(r.geography, r.service_tier): r for r in rows if r.region == "us-east-1"}
    assert lookup["global_cris", "standard"].input_price_per_1k_tokens == Decimal(".003")
    assert lookup["geo_cris", "priority"].cache_write_price_per_1k_tokens == Decimal(".00721875")
    assert lookup["global_cris", "flex"].output_price_per_1k_tokens == Decimal(".0075")
    assert is_v2_priced_model("global." + KIMI_MODEL)
    assert not is_v2_priced_model("moonshotai.unknown")


@pytest.mark.parametrize(
    "old,new",
    [
        ("per 1 million tokens", "per token"),
        ("Cache read", "Cache hit"),
        ("1.75x", "2x"),
        ("0.5x", "0.4x"),
        ("$3.00", "unknown"),
        ("US CRIS | $3.30", "Global CRIS | $3.30"),
    ],
)
def test_kimi_source_change_refuses_publication(old, new):
    with pytest.raises(SourceValidationError):
        parse_kimi_card(CARD.read_bytes().replace(old.encode(), new.encode()), load_snapshot().rates, verified_at=NOW)


def test_old_snapshot_is_preserved():
    old = load_snapshot("2026-09-24.1")
    new = load_snapshot()
    assert set(old.rates) <= set(new.rates)
    assert len(new.required_variants - old.required_variants) == 111


def test_kimi_default_tier_and_cache_cost_survive_settlement():
    from pricing_policy.policy import RoutingEvidence, build_pricing_decision, normalize_usage, verify_pricing_decision

    snapshot = load_snapshot()
    evidence = RoutingEvidence(
        original_model_id="global." + KIMI_MODEL,
        billing_model_id=KIMI_MODEL,
        forwarded_model_id="global." + KIMI_MODEL,
        endpoint_host="bedrock-runtime.us-east-1.amazonaws.com",
        endpoint_region="us-east-1",
        geography="global_cris",
        served_service_tier_raw="default",
    )
    usage = normalize_usage(
        {"input_tokens": 110, "output_tokens": 10, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 100}, api_format="openai"
    )
    decision = build_pricing_decision(
        request_id="kimi-test",
        org_id="test",
        usage=usage,
        evidence=evidence,
        rows=snapshot.rates,
        snapshot=snapshot,
        generation_id=1,
        pointer_revision=1,
        source_kind="database",
    )
    assert decision.variant_key[2] == "standard"
    assert decision.confidence == "verified"
    assert decision.ledger_cost == Decimal("0.000555")
    assert verify_pricing_decision(decision.to_dict(), request_id="kimi-test", org_id="test") == Decimal("0.000555")
    from dataclasses import replace

    assert replace(evidence, billing_model_id="openai.gpt-6-astra").served_service_tier is None


def test_historical_kimi_unconfirmed_tier_estimate_remains_replayable():
    from pricing_policy.policy import RoutingEvidence, _decision_content_hash, build_pricing_decision, normalize_usage, verify_pricing_decision

    snapshot = load_snapshot()
    evidence = RoutingEvidence(
        original_model_id="global." + KIMI_MODEL,
        billing_model_id=KIMI_MODEL,
        forwarded_model_id="global." + KIMI_MODEL,
        endpoint_region="us-east-1",
        geography="global_cris",
    )
    usage = normalize_usage(
        {"input_tokens": 10, "output_tokens": 10, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}, api_format="openai"
    )
    decision = build_pricing_decision(
        request_id="old-kimi-test",
        org_id="test",
        usage=usage,
        evidence=evidence,
        rows=snapshot.rates,
        snapshot=snapshot,
        generation_id=1,
        pointer_revision=1,
        source_kind="database",
    )
    payload = decision.to_dict()
    payload["routing"]["served_service_tier_raw"] = "default"
    payload["content_sha256"] = _decision_content_hash(payload)
    assert verify_pricing_decision(payload, request_id="old-kimi-test", org_id="test") == decision.ledger_cost
