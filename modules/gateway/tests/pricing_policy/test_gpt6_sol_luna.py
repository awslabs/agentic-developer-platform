"""Published GPT-6 rates must price cached developer requests, never fallback."""

from decimal import Decimal
from pathlib import Path

import pytest

from pricing_policy import RoutingEvidence, build_pricing_decision, load_snapshot, normalize_usage, verify_pricing_decision
from pricing_policy.aws_sources import CARD_SLUGS, parse_model_card


@pytest.mark.parametrize("family", ["sol", "luna"])
def test_full_aws_card_covers_reviewed_routes(family):
    model = "openai.gpt-6-" + family
    snapshot = load_snapshot()
    content = (Path(__file__).parent / f"fixtures/aws/gpt-6-{family}.md").read_bytes()
    rows = parse_model_card(content, model, snapshot.rates, source_url=snapshot.models[model]["source_url"], verified_at="2026-09-28T00:25:00+00:00")
    assert CARD_SLUGS[model] == "gpt-6-" + family
    assert {r.variant_key for r in rows} == {r.variant_key for r in snapshot.rows_for_model(model)}
    assert len(rows) == 76
    assert all(r.service_tier == "standard" for r in rows)
    assert not any(r.geography == "in_region" and r.region != "us-east-1" for r in rows)


def test_old_rates_and_decisions_stay_replayable():
    old, new = load_snapshot("2026-09-24.2"), load_snapshot()
    assert set(old.rates) <= set(new.rates)
    assert old.snapshot_version in new.supported_predecessor_versions
    assert len(new.required_variants - old.required_variants) == 152


@pytest.mark.parametrize("family,expected", [("sol", "0.058437"), ("luna", "0.002922")])
def test_real_cached_developer_usage_cost(family, expected):
    model = "openai.gpt-6-" + family
    snapshot = load_snapshot()
    usage = normalize_usage(
        {"input_tokens": 201635, "output_tokens": 1169, "input_tokens_details": {"cached_tokens": 201153, "cache_write_tokens": 480}},
        api_format="openai",
    )
    decision = build_pricing_decision(
        request_id="sample",
        org_id="tenant",
        usage=usage,
        evidence=RoutingEvidence(
            original_model_id=model,
            billing_model_id=model,
            forwarded_model_id="us." + model,
            endpoint_region="us-east-1",
            geography="geo_cris",
            served_service_tier_raw="default",
        ),
        rows=snapshot.rates,
        snapshot=snapshot,
        source_kind="bundled_snapshot",
    )
    assert decision.ledger_cost == Decimal(expected)
    assert decision.variant_key[3] == "short"
    assert "unknown_model" not in decision.estimate_reasons
    assert "unpublished_cache_read_rate" not in decision.estimate_reasons
    assert verify_pricing_decision(decision.to_dict(), request_id="sample", org_id="tenant") == Decimal(expected)


@pytest.mark.parametrize("family,multiplier", [("sol", Decimal(1)), ("luna", Decimal("0.05"))])
@pytest.mark.parametrize("tokens,tier,input_rate,output_rate", [(272000, "short", "2.2", "11"), (272001, "long", "4.4", "16.5")])
def test_total_input_including_cache_selects_full_request_tier(family, multiplier, tokens, tier, input_rate, output_rate):
    snapshot = load_snapshot()
    model = "openai.gpt-6-" + family
    decision = build_pricing_decision(
        request_id="boundary",
        org_id="tenant",
        usage=normalize_usage(
            {"input_tokens": tokens, "output_tokens": 1000, "input_tokens_details": {"cached_tokens": tokens - 1000}}, api_format="openai"
        ),
        evidence=RoutingEvidence(
            original_model_id=model,
            billing_model_id=model,
            forwarded_model_id="us." + model,
            endpoint_region="us-east-1",
            geography="geo_cris",
            served_service_tier_raw="standard",
        ),
        rows=snapshot.rates,
        snapshot=snapshot,
    )
    expected = (Decimal(input_rate) * (1000 + Decimal(tokens - 1000) / 10) + Decimal(output_rate) * 1000) * multiplier / 1_000_000
    assert decision.variant_key[3] == tier
    assert decision.ledger_cost == expected.quantize(Decimal("0.000001"))
