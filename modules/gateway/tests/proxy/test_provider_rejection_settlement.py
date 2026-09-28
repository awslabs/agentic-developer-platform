"""Real reservation Lua -> proxy provider boundary -> settlement -> flow read."""

import json
from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import fakeredis.aioredis
import pytest
from botocore.exceptions import ClientError

from src.budget import enforcement_service
from src.budget.config import budget_config
from src.budget.reservations import ReservationStore
from src.orchestration import flow_budget, flow_meter
from src.proxy.bedrock_enforcement import RoutingDecision
from src.proxy.pricing_capture import PricingCapture
from src.proxy.service import ProxyService
from tests.orchestration.test_policy_admission import _policy
from tests.proxy.conftest import MockBedrockClient, MockPoolService

MODEL = "us.anthropic.claude-sonnet-4-20250514-v1:0"
RESERVED = Decimal("0.750015")


def rejection(operation="InvokeModel", **changes):
    response = {
        "Error": {"Code": "ResourceNotFoundException", "Message": "You no longer have access to this legacy model after 30 days of inactivity"},
        "ResponseMetadata": {"HTTPStatusCode": 404, "RequestId": "provider-rejected-request", "RetryAttempts": 0},
    }
    response.update(changes)
    return ClientError(response, operation)


class Client(MockBedrockClient):
    def __init__(self, error=None, *, after_response=False, after_chunk=False, missing_usage=False):
        super().__init__(error=error)
        self.meta = SimpleNamespace(region_name="us-east-1", endpoint_url="https://bedrock-runtime.us-east-1.amazonaws.com")
        self.after_response, self.after_chunk, self.missing_usage = after_response, after_chunk, missing_usage

    async def invoke_model(self, **kwargs):
        if self.after_response:
            body = MagicMock()
            body.read.side_effect = self.error
            return {"body": body}
        if self.missing_usage:
            body = MagicMock()
            body.read.return_value = json.dumps({"id": "msg", "type": "message", "role": "assistant", "content": [], "usage": {}}).encode()
            return {"body": body}
        return await super().invoke_model(**kwargs)

    async def invoke_model_with_response_stream(self, **kwargs):
        if self.after_response:

            def body():
                if self.after_chunk:
                    yield {"chunk": {"bytes": b'{"type":"content_block_delta","delta":{"type":"text_delta","text":"hello"}}'}}
                raise self.error

            return {"body": body(), "ResponseMetadata": {"RequestId": "accepted-stream"}}
        return await super().invoke_model_with_response_stream(**kwargs)


class Pool(MockPoolService):
    async def get_client(self, credentials=None, *, single_attempt=False):
        assert single_attempt, "governed provider invocation must disable hidden retries"
        return await super().get_client(credentials)


@pytest.fixture
async def ledger(monkeypatch, token_context):
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    store = ReservationStore(redis_url=None, ttl_seconds=120, client=redis, clock=lambda: 1000.0)
    policy = _policy()
    target = flow_meter.meter_target(org_id=policy.org_id, flow_id="rejected-model-flow", policy=policy)
    monkeypatch.setattr(budget_config, "budget_reservation_enabled", True)
    monkeypatch.setattr(flow_budget, "_reservations", store)
    service = enforcement_service.BudgetEnforcementService()
    service._reservations = store
    monkeypatch.setattr(enforcement_service, "budget_enforcement_service", service)
    monkeypatch.setattr("src.proxy.service.resolve_routing_decision", AsyncMock(return_value=RoutingDecision()))
    monkeypatch.setattr("src.proxy.service.resolve_shadow_target", AsyncMock(return_value=None))
    monkeypatch.setattr("src.proxy.service.get_session_factory", lambda: lambda: AsyncMock())
    usage = SimpleNamespace(log_request=AsyncMock())
    monkeypatch.setattr("src.proxy.service.UsageService", lambda _: usage)
    pricing = AsyncMock(side_effect=ValueError("usage or pricing unavailable"))
    monkeypatch.setattr("src.proxy.service.price_completed_usage", pricing)
    token_context._policy_flow_target = target
    token_context._run_scope_reservations = [target]
    assert (await store.reserve("__initialized__", Decimal(0), [replace(target, require_initialization=False)])).admitted
    assert (await store.reserve("other-charge", Decimal(7), [target])).admitted
    await store.reconcile("other-charge", Decimal(7), [target])
    assert (await store.reserve("current", RESERVED, [target])).admitted
    original = await redis.hgetall(target.key())
    yield SimpleNamespace(
        store=store, redis=redis, target=target, policy=policy, context=token_context, usage=usage, pricing=pricing, original=original
    )
    await redis.aclose()


async def invoke(ledger, client, *, stream=False):
    proxy = ProxyService(Pool(client))
    capture = PricingCapture("current", MODEL)
    try:
        result = await proxy.invoke_model(MODEL, {"messages": [], "max_tokens": 1}, ledger.context, stream=stream, pricing_capture=capture)
        if stream:
            async for _ in result:
                pass
    except Exception:
        pass
    return capture


async def snapshot(ledger):
    return await flow_meter.read_flow_meter(org_id=ledger.policy.org_id, flow_id="rejected-model-flow", policy=ledger.policy)


@pytest.mark.parametrize("stream", [False, True])
async def test_initial_missing_model_rejection_releases_only_its_reservation(ledger, stream):
    operation = "InvokeModelWithResponseStream" if stream else "InvokeModel"
    capture = await invoke(ledger, Client(rejection(operation)), stream=stream)
    assert capture.no_inference_rejection.code == "ResourceNotFoundException"
    assert capture.no_inference_rejection.operation == operation
    assert capture.decision is None
    ledger.pricing.assert_not_awaited()
    logged = ledger.usage.log_request.await_args.kwargs
    assert logged["status_code"] == (502 if stream else 500)  # existing failed-invocation status
    assert logged["cost_usd"] == 0 and logged["pricing_decision"] is None
    assert logged["provider_request_id"] == "provider-rejected-request"
    observed = await snapshot(ledger)
    assert observed.total_usd == 7 and not observed.has_pending
    entries = await ledger.redis.hgetall(ledger.target.key())
    assert entries["other-charge"] == ledger.original["other-charge"]
    assert entries["__initialized__"] == ledger.original["__initialized__"]
    assert entries["current"].startswith("0:") and "pending:current" not in entries
    assert (await ledger.store.reserve("next", Decimal(43), [ledger.target])).admitted
    assert not (await ledger.store.reserve("over-budget", Decimal("0.000001"), [ledger.target])).admitted


@pytest.mark.parametrize(
    "error",
    [
        TimeoutError("unknown upstream outcome"),
        ConnectionResetError("response lost"),
        RuntimeError("ResourceNotFoundException"),
        rejection(Error={"Code": "InternalServerException", "Message": "failure"}),
        rejection(Error={"Code": "AccessDeniedException", "Message": "denied"}),
        rejection(ResponseMetadata={"HTTPStatusCode": 500, "RequestId": "p", "RetryAttempts": 0}),
        rejection(ResponseMetadata={"HTTPStatusCode": 404, "RequestId": "p", "RetryAttempts": 1}),
        rejection(ResponseMetadata={"HTTPStatusCode": 404, "RequestId": "p"}),
        rejection(ResponseMetadata={"HTTPStatusCode": 404, "RetryAttempts": 0}),
        rejection(operation="ListModels"),
    ],
)
async def test_uncertain_or_unproven_initial_failure_still_blocks(ledger, error):
    capture = await invoke(ledger, Client(error))
    assert capture.no_inference_rejection is None
    assert await snapshot(ledger) is None
    assert await ledger.redis.hget(ledger.target.key(), "current") == ledger.original["current"]
    assert await ledger.redis.hget(ledger.target.key(), "other-charge") == ledger.original["other-charge"]
    assert await ledger.store.reserve("next", Decimal(1), [ledger.target]) is None


@pytest.mark.parametrize("stream,after_chunk", [(False, False), (True, False), (True, True)])
async def test_same_typed_error_after_response_is_never_zero_settled(ledger, stream, after_chunk):
    operation = "InvokeModelWithResponseStream" if stream else "InvokeModel"
    capture = await invoke(ledger, Client(rejection(operation), after_response=True, after_chunk=after_chunk), stream=stream)
    assert capture.no_inference_rejection is None
    assert await snapshot(ledger) is None
    assert await ledger.redis.hget(ledger.target.key(), "current") == ledger.original["current"]


@pytest.mark.parametrize("missing_usage", [False, True])
async def test_success_with_missing_usage_or_failed_pricing_is_not_a_rejection(ledger, missing_usage):
    capture = await invoke(ledger, Client(missing_usage=missing_usage))
    assert capture.no_inference_rejection is None
    assert capture.decision is None
    ledger.pricing.assert_awaited_once()
    assert await snapshot(ledger) is None
    assert await ledger.redis.hget(ledger.target.key(), "current") == ledger.original["current"]


async def test_rejected_request_cannot_clear_another_unknown_request(ledger):
    assert (await ledger.store.reserve("unknown-sibling", Decimal(2), [ledger.target])).admitted
    await ledger.store.mark_unknown("unknown-sibling", ledger.target)
    before = await ledger.redis.hgetall(ledger.target.key())
    await invoke(ledger, Client(rejection()))
    assert await snapshot(ledger) is None
    after = await ledger.redis.hgetall(ledger.target.key())
    assert after["unknown-sibling"] == before["unknown-sibling"]
    assert after["pending:unknown-sibling"] == before["pending:unknown-sibling"]
    assert after["current"].startswith("0:") and "pending:current" not in after
