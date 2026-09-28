"""Actual cache-hit evidence must use online Claude rates, with stable replay."""

import json
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from pricing_policy import RoutingEvidence, build_pricing_decision, load_snapshot, normalize_usage, verify_pricing_decision
from pricing_policy.policy import UnsupportedVariantError, select_rate_row
from pricing_policy.storage import RateSourceState
from src.budget import pricing, pricing_decisions
from src.proxy import routes
from src.proxy.pricing_capture import PricingCapture
from src.proxy.service import ProxyService
from tests.proxy.conftest import MockBedrockClient, MockPoolService
from tests.proxy.test_claude_pricing_decision import flush_logs, metering  # noqa: F401 -- shared producer persistence fixture

FIXTURES = Path(__file__).parent / "fixtures" / "claude-online-tier"
MODEL = "anthropic.claude-haiku-4-5-20251001-v1:0"
FORWARDED = "global." + MODEL


def actual_events():
    return [json.loads(line[5:].strip()) for line in (FIXTURES / "cache-read.sse").read_text().splitlines() if line.startswith("data:")]


def actual_usage():
    events = actual_events()
    usage = dict(events[0]["message"]["usage"])
    usage.update(next(event["usage"] for event in events if event["type"] == "message_delta"))
    return usage


def evidence(tier=None, **changes):
    return replace(
        RoutingEvidence(
            original_model_id=FORWARDED,
            billing_model_id=MODEL,
            forwarded_model_id=FORWARDED,
            endpoint_region="us-east-1",
            geography="global_cris",
            served_service_tier_raw=tier,
        ),
        **changes,
    )


def price(rows, tier=None, **changes):
    return build_pricing_decision(
        request_id="online-cache-hit",
        org_id="test-org",
        usage=normalize_usage(actual_usage(), api_format="anthropic"),
        evidence=evidence(tier, **changes),
        rows=rows,
        snapshot=load_snapshot(),
        generation_id=4,
        pointer_revision=4,
    )


class NestedTierClient(MockBedrockClient):
    def __init__(self):
        super().__init__(
            response={
                "id": "cache-read",
                "type": "message",
                "role": "assistant",
                "model": MODEL,
                "content": [{"type": "text", "text": "pricing evidence ready"}],
                "stop_reason": "end_turn",
                "usage": actual_usage(),
            },
            stream_chunks=actual_events(),
        )
        # No synthetic top-level serviceTier: the actual response only confirms
        # it in usage, so the former producer tests could not detect this defect.
        self.meta = SimpleNamespace(region_name="us-east-1", endpoint_url="https://bedrock-runtime.us-east-1.amazonaws.com")


@pytest.fixture
def actual_metering(metering, monkeypatch):  # noqa: F811 -- imported pytest fixture
    snapshot = load_snapshot()
    now = datetime.now(UTC).isoformat()
    rows = tuple(replace(row, generation_id=4, verified_at=now) for row in snapshot.rates)
    state = RateSourceState(rows=rows, source="v2_generation:4", generation_id=4, pointer_revision=4, from_database=True)
    monkeypatch.setattr(pricing_decisions, "load_snapshot", lambda: snapshot)
    monkeypatch.setattr(pricing_decisions.pricing_v2_reader, "get_rate_state", AsyncMock(return_value=state))
    monkeypatch.setattr(pricing_decisions.pricing_v2_reader, "cached_rate_state", lambda: state)
    return SimpleNamespace(**metering.__dict__, actual_snapshot=snapshot, actual_state=state)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path,stream",
    [
        ("/v1/messages", False),
        ("/v1/messages", True),
        ("/v1/chat/completions", False),
        ("/v1/chat/completions", True),
        (f"/model/{FORWARDED}/invoke", False),
        (f"/model/{FORWARDED}/invoke-with-response-stream", True),
        ("/bedrock/invoke", False),
        ("/bedrock/invoke-with-response-stream", True),
    ],
)
async def test_actual_nested_only_cache_hit_settles_once_at_standard_price(actual_metering, path, stream):
    proxy = ProxyService(MockPoolService(NestedTierClient()))
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[routes.get_token_context] = lambda: actual_metering.context
    app.dependency_overrides[routes.get_proxy_service] = lambda: proxy
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            path,
            json={"model": FORWARDED, "messages": [], "max_tokens": 64, "stream": stream},
            headers={"accept": "text/event-stream", "user-agent": "claude-cli/2.1.0"},
        )
    assert response.status_code == 200, response.text
    await flush_logs()
    actual_metering.writer.write_log.assert_awaited_once()
    actual_metering.usage.log_request.assert_awaited_once()
    event = actual_metering.writer.write_log.await_args.kwargs["log_data"]
    decision = event["pricing_decision"]
    assert decision["variant_key"] == [MODEL, "global_cris", "standard", "flat", "us-east-1"]
    assert decision["routing"]["served_service_tier"] == "standard"
    assert "unconfirmed_service_tier" not in decision["estimate_reasons"]
    assert decision["usage"]["output_tokens"] == 6
    assert decision["usage"]["cache_read_input_tokens"] == 28170
    assert decision["confidence"] == "verified"
    assert verify_pricing_decision(decision, request_id=event["request_id"], org_id=event["org_id"]) == Decimal("0.002860")
    assert actual_metering.usage.log_request.await_args.kwargs["cost_usd"] == Decimal("0.002860")


@pytest.mark.parametrize("tier", [None, "", "default", "auto", "unknown", " STANDARD "])
def test_missing_unknown_and_confirmed_tiers_never_rank_offline_batch(tier):
    decision = price(load_snapshot().rates, tier)
    assert decision.variant_key[2] == "standard"
    assert decision.ledger_cost == Decimal("0.002860")
    assert ("unconfirmed_service_tier" in decision.estimate_reasons) is (tier != " STANDARD ")
    assert "unpublished_cache_read_rate" not in decision.estimate_reasons


@pytest.mark.parametrize("tier", [None, "auto", "unknown", "priority", "flex"])
def test_route_widening_cannot_reintroduce_offline_batch(tier):
    decision = price(load_snapshot().rates, tier, geography=None, endpoint_region="unpublished-region-1")
    assert decision.variant_key[2] == "standard"
    assert "unsupported_region" in decision.estimate_reasons


def test_missing_tier_still_ranks_supported_online_candidates_conservatively():
    standard = next(row for row in load_snapshot().rates if row.variant_key == (MODEL, "global_cris", "standard", "flat", "us-east-1"))
    priority = replace(standard, service_tier="priority", cache_read_price_per_1k_tokens=Decimal(".0002"))
    flex = replace(standard, service_tier="flex", cache_read_price_per_1k_tokens=Decimal(".00005"))
    chosen = price((*load_snapshot().rates, priority, flex))
    assert chosen.variant_key[2] == "priority"
    assert "unconfirmed_service_tier" in chosen.estimate_reasons


def test_explicit_batch_generic_selection_and_historical_replay_are_unchanged():
    assert price(load_snapshot().rates, "batch").ledger_cost == Decimal("0.014107")
    saved = json.loads((FIXTURES / "historical-decision.json").read_text())
    assert saved["variant_key"][2] == "batch" and saved["routing"]["served_service_tier"] is None
    assert verify_pricing_decision(saved, request_id=saved["request_id"], org_id=saved["org_id"]) == Decimal("0.014107")


def test_openai_selection_contract_is_unchanged():
    rows = tuple(replace(row, model_id="openai.test") for row in load_snapshot().rates if row.model_id == MODEL)
    chosen = price(rows, None, billing_model_id="openai.test", original_model_id="openai.test", forwarded_model_id="openai.test")
    assert chosen.variant_key[2] == "batch"
    assert chosen.decision_version == 1 and chosen.policy_version == 1


@pytest.mark.parametrize("tier", [None, "auto", "standard"])
def test_batch_only_active_rows_use_model_specific_bundle_without_losing_spend(actual_metering, tier):
    state = replace(
        actual_metering.actual_state,
        rows=tuple(row for row in actual_metering.actual_state.rows if row.model_id == MODEL and row.service_tier == "batch"),
    )
    decision = pricing_decisions.decision_from_state(
        request_id="batch-only-state",
        org_id="org",
        usage=normalize_usage(actual_usage(), api_format="anthropic"),
        evidence=evidence(tier),
        state=state,
    )
    assert decision.source_kind == "bundled_snapshot" and decision.generation_id is None
    assert decision.variant_key[2] == "standard" and decision.ledger_cost == Decimal("0.002860")
    assert "bootstrap_fallback" in decision.estimate_reasons
    with pytest.raises(UnsupportedVariantError):
        select_rate_row(
            rows=state.rows, usage=normalize_usage(actual_usage(), api_format="anthropic"), evidence=evidence(tier), short_threshold=272000
        )
    quote = pricing.PricingService().quote_cost(FORWARDED, 13, 6, state=state)
    assert quote == (Decimal("0.000043"), Decimal(".001"), Decimal(".005"))


@pytest.mark.parametrize("unconfirmed", [None, "", "default", "auto", "unknown", 123])
def test_confirmed_nested_tier_survives_later_missing_or_invalid_fields(unconfirmed):
    capture = PricingCapture("nested", FORWARDED)
    capture.forwarded(NestedTierClient(), FORWARDED)
    capture.response({"usage": {"service_tier": "standard"}}, {"serviceTier": "default"})
    capture.chunk(json.dumps({"type": "message_delta", "service_tier": unconfirmed, "usage": {"output_tokens": 6}}).encode())
    assert capture.routing.served_service_tier == "standard"


def test_nested_delta_tier_is_captured_and_conflicts_remain_unconfirmed():
    capture = PricingCapture("conflict", FORWARDED)
    capture.forwarded(NestedTierClient(), FORWARDED)
    capture.chunk(json.dumps({"type": "message_delta", "usage": {"output_tokens": 6, "service_tier": "standard"}}).encode())
    assert capture.routing.served_service_tier == "standard"
    capture.response({"usage": {"service_tier": "priority"}}, {})
    assert capture.routing.served_service_tier is None
    capture.chunk(json.dumps({"type": "message_delta", "usage": {"service_tier": "standard"}}).encode())
    assert capture.routing.served_service_tier is None
    decision = price(load_snapshot().rates, capture.routing.served_service_tier_raw)
    assert decision.variant_key[2] == "standard" and decision.confidence == "estimated"
    assert "unconfirmed_service_tier" in decision.estimate_reasons


def test_nested_confirmation_keeps_original_raw_spelling():
    capture = PricingCapture("raw-provenance", FORWARDED)
    capture.forwarded(NestedTierClient(), FORWARDED)
    capture.response({"usage": {"service_tier": " Standard "}}, {"serviceTier": "default"})
    capture.chunk(json.dumps({"type": "message_delta", "usage": {"service_tier": "standard"}}).encode())
    assert capture.routing.served_service_tier == "standard"
    assert capture.routing.served_service_tier_raw == " Standard "


def test_non_claude_capture_retains_legacy_tier_precedence_and_raw_values():
    capture = PricingCapture("openai-legacy", "openai.gpt-5.5")
    capture.forwarded(NestedTierClient(), "openai.gpt-5.5")
    capture.response({"usage": {"service_tier": "standard"}, "service_tier": "priority"}, {"serviceTier": "default"})
    assert capture.routing.served_service_tier is None
    assert capture.routing.served_service_tier_raw == "default"
    capture.chunk(json.dumps({"type": "message_start", "service_tier": " Flex ", "message": {"usage": {"service_tier": "standard"}}}).encode())
    assert capture.routing.served_service_tier_raw == " Flex "
    capture.chunk(json.dumps({"type": "message_delta", "usage": {"output_tokens": 6, "service_tier": "standard"}}).encode())
    assert capture.routing.served_service_tier_raw == " Flex "
