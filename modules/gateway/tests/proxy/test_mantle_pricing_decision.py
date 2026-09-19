"""Official AWS usage evidence through the real passthrough and S3 builder."""

import importlib
import json
from dataclasses import replace
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from pricing_policy import load_snapshot, verify_pricing_decision
from pricing_policy.storage import RateSourceState
from src.budget import pricing_v2_reader

base = importlib.import_module("tests.lambda.test_mantle_budget_settlement")
metering = base.metering


class SplitSSE(httpx.AsyncByteStream):
    def __init__(self, payload):
        self.payload = payload

    async def __aiter__(self):
        for start in range(0, len(self.payload), 7):
            yield self.payload[start : start + 7]


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_aws_sol_example_records_one_bound_decimal_decision(metering, monkeypatch, stream):
    writer, usage_service, _ = metering
    rows = tuple(replace(row, generation_id=7) for row in load_snapshot().rates)
    state = RateSourceState(rows=rows, source="v2_generation:7", generation_id=7, pointer_revision=9, from_database=True)
    monkeypatch.setattr(pricing_v2_reader, "get_rate_state", AsyncMock(return_value=state))
    response = {
        "model": "openai.gpt-5.6-sol",
        "service_tier": "standard",
        "output": [],
        "usage": {"input_tokens": 2048, "output_tokens": 256, "input_tokens_details": {"cached_tokens": 1920, "cache_write_tokens": 0}},
    }
    payload = json.dumps(response).encode()
    wire = b'data: {"type":"response.completed","response":' + payload + b"}\n\n" if stream else payload
    auth = MagicMock()
    auth.sign.return_value = {"Authorization": "synthetic"}
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, headers={"x-amzn-requestid": "provider-original"}, stream=SplitSSE(wire)))
    ) as client:
        service = base.mantle_service.MantlePassthroughService(auth, "https://bedrock-mantle.us-east-1.api.aws", http_client=client)
        result = await service.create_response(
            b'{"model":"openai.gpt-5.6-sol","service_tier":"flex"}',
            base.context(),
            stream=stream,
            model="openai.gpt-5.6-sol",
            request_id="sol-priced",
        )
        actual = b"".join([chunk async for chunk in result]) if stream else result.content
    assert actual == wire
    await base.flush_chat_logs()
    event = writer.write_log.await_args.kwargs["log_data"]
    decision = event["pricing_decision"]
    assert verify_pricing_decision(decision, request_id="sol-priced", org_id="tenant") == Decimal("0.007040")
    assert Decimal(decision["exact_cost_usd"]) == Decimal("0.00704")
    assert decision["generation_id"] == 7 and decision["pointer_revision"] == 9
    assert decision["source_kind"] == "database"
    assert decision["routing"]["requested_service_tier"] == "flex"
    assert decision["routing"]["served_service_tier"] == "standard"
    assert decision["routing"]["endpoint_host"] == "bedrock-mantle.us-east-1.api.aws"
    logged = usage_service.log_request.await_args.kwargs
    assert logged["cost_usd"] == Decimal("0.007040") and isinstance(logged["cost_usd"], Decimal)
    assert logged["input_tokens"] == 128 and logged["cache_read_input_tokens"] == 1920
    assert logged["provider_request_id"] == "provider-original"
    assert logged["destination_region"] == "us-east-1"
    assert logged["cache_creation_input_tokens"] == 0
    assert base.mantle_service.reconcile_budget_reservation.await_args.kwargs["actual_cost_usd"] == Decimal("0.007040")


@pytest.mark.parametrize("bad_url", ["bundled://arbitrary.json", "file:///tmp/rates", "http://example.com/rates"])
def test_bundled_provenance_does_not_accept_arbitrary_source_urls(bad_url):
    from pricing_policy import RoutingEvidence, normalize_usage
    from pricing_policy.policy import InvalidPricingDecisionError, _decision_content_hash
    from src.budget.pricing_decisions import decision_from_state

    state = pricing_v2_reader.cached_rate_state()
    decision = decision_from_state(
        request_id="unknown",
        org_id="tenant",
        state=state,
        usage=normalize_usage({"input_tokens": 1000, "output_tokens": 1000}, api_format="openai"),
        evidence=RoutingEvidence(original_model_id="openai.unreleased", billing_model_id="openai.unreleased"),
    )
    payload = decision.to_dict()
    assert verify_pricing_decision(payload, request_id="unknown", org_id="tenant") == Decimal("0.018000")
    assert "unknown_model" in payload["estimate_reasons"]
    payload["source_url"] = bad_url
    payload["content_sha256"] = _decision_content_hash(payload)
    with pytest.raises(InvalidPricingDecisionError):
        verify_pricing_decision(payload, request_id="unknown", org_id="tenant")
