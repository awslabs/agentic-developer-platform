"""Actual Claude route -> raw metering -> Decimal persistence -> S3 decision."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from pricing_policy import canonical_billing_model_id, load_snapshot, normalize_usage, verify_pricing_decision
from pricing_policy.storage import RateSourceState
from src.budget import pricing_decisions
from src.chat_logging.config import ScrubLevel
from src.chat_logging.service import ChatLoggingService, StreamingResponseBuffer
from src.proxy import routes
from src.proxy.bedrock_enforcement import RoutingDecision
from src.proxy.model_resolver import ModelResolver
from src.proxy.pricing_capture import PricingCapture
from src.proxy.service import ProxyService
from tests.proxy.conftest import MockBedrockClient, MockPoolService

FORWARDED = "global.anthropic.claude-opus-4-6-v1"
MODEL = canonical_billing_model_id(FORWARDED)
USAGE = {
    "input_tokens": 100,
    "output_tokens": 50,
    "cache_read_input_tokens": 200,
    "cache_creation_input_tokens": 700,
    "cache_creation": {"ephemeral_5m_input_tokens": 300, "ephemeral_1h_input_tokens": 400},
}


@pytest.fixture
def metering(monkeypatch, token_context):
    # Synthetic independent rates exercise the producer; source correctness is
    # tested by the independently frozen seed/source inventory elsewhere.
    template = load_snapshot().rates[0]
    row = replace(
        template,
        model_id=MODEL,
        geography="global_cris",
        service_tier="standard",
        context_tier="flat",
        region="us-west-2",
        max_input_tokens=None,
        input_price_per_1k_tokens=Decimal(".005"),
        output_price_per_1k_tokens=Decimal(".025"),
        cache_read_price_per_1k_tokens=Decimal(".000125"),
        cache_write_price_per_1k_tokens=Decimal(".00625"),
        cache_write_1h_price_per_1k_tokens=Decimal(".010"),
        cache_write_policy="full_rate",
        generation_id=7,
        verified_at=datetime.now(UTC).isoformat(),
    )
    snapshot = replace(load_snapshot(), rates=(*(r for r in load_snapshot().rates if r.model_id != MODEL), replace(row, generation_id=None)))
    monkeypatch.setattr(pricing_decisions, "load_snapshot", lambda: snapshot)
    state = RateSourceState(rows=(row,), source="v2_generation:7", generation_id=7, pointer_revision=9, from_database=True)
    monkeypatch.setattr(pricing_decisions.pricing_v2_reader, "get_rate_state", AsyncMock(return_value=state))

    def factory():
        return AsyncMock()

    monkeypatch.setattr(pricing_decisions, "get_session_factory", lambda: factory)
    monkeypatch.setattr("src.proxy.service.get_session_factory", lambda: factory)
    usage_service = SimpleNamespace(log_request=AsyncMock())
    monkeypatch.setattr("src.proxy.service.UsageService", lambda _: usage_service)
    reconcile = AsyncMock()
    monkeypatch.setattr("src.proxy.service.reconcile_budget_reservation", reconcile)
    monkeypatch.setattr("src.proxy.service.resolve_routing_decision", AsyncMock(return_value=RoutingDecision()))
    monkeypatch.setattr("src.proxy.service.resolve_shadow_target", AsyncMock(return_value=None))
    writer = SimpleNamespace(write_log=AsyncMock())
    monkeypatch.setenv("BG_CHAT_LOGGING_BUCKET", "test-settlement-bucket")
    logger = ChatLoggingService(s3_writer=writer, enabled=True, scrub_level=ScrubLevel.BASIC)
    monkeypatch.setattr(routes, "get_chat_logging_service", lambda: logger)
    return SimpleNamespace(row=row, snapshot=snapshot, state=state, writer=writer, usage=usage_service, reconcile=reconcile, context=token_context)


class Client(MockBedrockClient):
    def __init__(self, usage):
        response = {
            "id": "msg-clause",
            "type": "message",
            "role": "assistant",
            "model": FORWARDED,
            "content": [{"type": "text", "text": "ok"}],
            "stop_reason": "end_turn",
            "usage": usage,
        }
        chunks = [
            {
                "type": "message_start",
                "message": {
                    "id": "msg-clause",
                    "role": "assistant",
                    "model": FORWARDED,
                    "content": [],
                    "usage": {key: value for key, value in usage.items() if key != "output_tokens"},
                },
            },
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "ok"}},
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn"},
                "usage": {"output_tokens": usage["output_tokens"]} if "output_tokens" in usage else {},
            },
            {"type": "message_stop"},
        ]
        super().__init__(response=response, stream_chunks=chunks)
        self.meta = SimpleNamespace(region_name="us-west-2", endpoint_url="https://bedrock-runtime.us-west-2.amazonaws.com")

    async def invoke_model(self, **kwargs):
        return {**await super().invoke_model(**kwargs), "serviceTier": "standard"}

    async def invoke_model_with_response_stream(self, **kwargs):
        return {**await super().invoke_model_with_response_stream(**kwargs), "serviceTier": "standard"}


async def flush_logs():
    pending = [task for task in asyncio.all_tasks() if task.get_name().startswith("chat_log_")]
    if pending:
        await asyncio.gather(*pending)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path,stream",
    [
        ("/model/custom-claude/invoke", False),
        ("/model/custom-claude/invoke-with-response-stream", True),
        ("/v1/messages", False),
        ("/v1/messages", True),
        ("/v1/chat/completions", False),
        ("/v1/chat/completions", True),
        ("/bedrock/invoke", False),
        ("/bedrock/invoke-with-response-stream", True),
    ],
)
async def test_claude_routes_emit_one_additive_durable_decision(metering, path, stream):
    client = Client(USAGE)
    proxy = ProxyService(MockPoolService(client), model_resolver=ModelResolver(custom_aliases={"custom-claude": FORWARDED}))
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[routes.get_token_context] = lambda: metering.context
    app.dependency_overrides[routes.get_proxy_service] = lambda: proxy
    body = {"model": "custom-claude", "max_tokens": 80, "messages": [{"role": "user", "content": "hello"}], "stream": stream}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
        response = await http.post(path, json=body, headers={"user-agent": "claude-cli/2.0", "accept": "text/event-stream"})
    assert response.status_code == 200, response.text
    await flush_logs()
    metering.writer.write_log.assert_awaited_once()
    event = metering.writer.write_log.await_args.kwargs["log_data"]
    decision = event["pricing_decision"]
    assert decision["decision_version"] == 2
    assert decision["source_kind"] == "database" and decision["generation_id"] == 7
    assert decision["routing"]["original_model_id"] == "custom-claude"
    assert decision["routing"]["forwarded_model_id"] == FORWARDED
    assert decision["routing"]["endpoint_region"] == "us-west-2"
    assert decision["routing"]["endpoint_host"] == "bedrock-runtime.us-west-2.amazonaws.com"
    assert decision["routing"]["geography"] == "global_cris"
    assert decision["usage"]["total_input_tokens"] == 1000
    assert decision["usage"]["cache_creation_5m_input_tokens"] == 300
    assert decision["usage"]["cache_creation_1h_input_tokens"] == 400
    assert event["response"]["usage"]["cache_creation"] == USAGE["cache_creation"]
    assert verify_pricing_decision(decision, request_id=event["request_id"], org_id=event["org_id"]) == Decimal(".007650")
    logged = metering.usage.log_request.await_args.kwargs
    assert logged["cost_usd"] == Decimal(".007650") and isinstance(logged["cost_usd"], Decimal)
    assert logged["input_tokens"] == 100 and logged["cache_creation_input_tokens"] == 700
    assert logged["client_tool"] == "claude_code"
    assert logged["request_id"] == event["request_id"]
    assert metering.reconcile.await_args.kwargs["actual_cost_usd"] == Decimal(".007650")
    assert client.invoke_calls[0]["modelId"] == FORWARDED


@pytest.mark.asyncio
@pytest.mark.parametrize("usage", [{}, {"input_tokens": 100}, {"output_tokens": 50}])
async def test_missing_usage_is_not_a_zero_cost_success(metering, usage):
    client = Client(usage)
    proxy = ProxyService(MockPoolService(client))
    capture = PricingCapture("request-missing-usage", FORWARDED)
    await proxy.invoke_model(FORWARDED, {"messages": [], "max_tokens": 80}, metering.context, pricing_capture=capture)
    assert capture.decision is None
    metering.usage.log_request.assert_not_awaited()
    metering.writer.write_log.assert_not_awaited()


@pytest.mark.asyncio
async def test_two_streams_keep_distinct_capture_objects(metering):
    proxy = ProxyService(MockPoolService(Client(USAGE)))
    first = PricingCapture("first-request", FORWARDED)
    second = PricingCapture("second-request", FORWARDED)
    a = await proxy.invoke_model(FORWARDED, {"messages": [], "max_tokens": 80}, metering.context, stream=True, pricing_capture=first)
    b = await proxy.invoke_model(FORWARDED, {"messages": [], "max_tokens": 80}, metering.context, stream=True, pricing_capture=second)

    async def drain(stream):
        return [chunk async for chunk in stream]

    await asyncio.gather(drain(a), drain(b))
    assert first.decision["request_id"] == "first-request"
    assert second.decision["request_id"] == "second-request"
    assert first.raw_usage is not second.raw_usage
    assert {call.kwargs["request_id"] for call in metering.usage.log_request.await_args_list} == {"first-request", "second-request"}


def test_openai_only_generation_uses_the_claude_bundle(metering):
    state = replace(metering.state, rows=tuple(row for row in load_snapshot().rates if row.model_id.startswith("openai.")))
    capture = PricingCapture("fallback-request", FORWARDED)
    capture.forwarded(Client(USAGE), FORWARDED)
    decision = pricing_decisions.decision_from_state(
        request_id=capture.request_id, org_id="org", usage=normalize_usage(USAGE, api_format="anthropic"), evidence=capture.routing, state=state
    )
    assert decision.source_kind == "bundled_snapshot"
    assert decision.variant_key[0] == MODEL and decision.generation_id is None
    assert "bootstrap_fallback" in decision.estimate_reasons
    assert decision.ledger_cost == Decimal(".007650")


def test_raw_stream_preserves_duration_split_and_missing_vs_zero():
    buffer = StreamingResponseBuffer()
    buffer.add_chunk(
        {
            "type": "message_start",
            "message": {"usage": {"input_tokens": 0, "cache_read_input_tokens": 0, "cache_creation": {"ephemeral_1h_input_tokens": 0}}},
        }
    )
    buffer.add_chunk({"type": "message_delta", "usage": {"output_tokens": 0}})
    assert buffer.usage == {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0, "cache_creation": {"ephemeral_1h_input_tokens": 0}}
    assert "cache_creation_input_tokens" not in buffer.usage


def test_provisional_stream_output_is_not_final_usage():
    buffer = StreamingResponseBuffer()
    buffer.add_chunk({"type": "message_start", "message": {"usage": {"input_tokens": 100, "output_tokens": 0}}})
    buffer.add_chunk({"type": "message_stop"})
    assert buffer.usage == {"input_tokens": 100}


def test_capture_reads_the_specific_wrapped_client_metadata():
    client = SimpleNamespace(
        _invoke_client=SimpleNamespace(meta=SimpleNamespace(region_name="us-east-1", endpoint_url="https://invoke.example")),
        _streaming_client=SimpleNamespace(meta=SimpleNamespace(region_name="us-west-2", endpoint_url="https://stream.example")),
    )
    capture = PricingCapture("routed-stream", "alias")
    capture.forwarded(client, FORWARDED, stream=True)
    assert capture.routing.endpoint_region == "us-west-2"
    assert capture.routing.endpoint_host == "stream.example"
    assert capture.routing.geography == "global_cris"


@pytest.mark.parametrize("counters", [{}, {"cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}])
@pytest.mark.asyncio
async def test_absent_and_reported_zero_cache_counters_reach_persistence(metering, counters):
    usage = {"input_tokens": 100, "output_tokens": 50, **counters}
    proxy = ProxyService(MockPoolService(Client(usage)))
    capture = PricingCapture("nullable-cache-request", FORWARDED)
    await proxy.invoke_model(FORWARDED, {"messages": [], "max_tokens": 80}, metering.context, pricing_capture=capture)
    logged = metering.usage.log_request.await_args.kwargs
    for name in ("cache_read_input_tokens", "cache_creation_input_tokens"):
        assert logged[name] == counters.get(name)
        assert capture.decision["usage"]["raw"][name] == counters.get(name)


def test_claude_quote_uses_current_generation_and_model_specific_bootstrap(metering, monkeypatch):
    from src.budget import pricing

    monkeypatch.setattr(pricing, "load_snapshot", lambda: metering.snapshot)
    service = pricing.PricingService()
    cost, input_rate, output_rate = service.quote_cost(FORWARDED, 100, 50, state=metering.state)
    assert (cost, input_rate, output_rate) == (Decimal(".001750"), Decimal(".005"), Decimal(".025"))
    openai_only = replace(metering.state, rows=tuple(row for row in load_snapshot().rates if row.model_id.startswith("openai.")))
    assert service.quote_cost(FORWARDED, 100, 50, state=openai_only) == (cost, input_rate, output_rate)


def test_current_claude_rate_view_does_not_return_curated_legacy_prices(metering, monkeypatch):
    from src.budget import pricing

    monkeypatch.setattr(pricing, "load_snapshot", lambda: metering.snapshot)
    monkeypatch.setattr(pricing_decisions.pricing_v2_reader, "cached_rate_state", lambda: metering.state)
    rates = pricing.PricingService().get_model_pricing(FORWARDED)
    assert rates == {
        "input": Decimal(".005"),
        "output": Decimal(".025"),
        "cache_read_input": Decimal(".000125"),
        "cache_creation_input": Decimal(".00625"),
        "cache_creation_1h_input": Decimal(".010"),
    }


@pytest.mark.asyncio
async def test_public_claude_cost_and_compatibility_helper_use_v2(metering, monkeypatch):
    from src.budget import pricing
    from src.budget.service import BudgetService
    from src.budget.utils import calculate_model_cost
    from src.shared.schemas.budget import CostCalculationRequest

    monkeypatch.setattr(pricing, "load_snapshot", lambda: metering.snapshot)
    monkeypatch.setattr(pricing_decisions.pricing_v2_reader, "cached_rate_state", lambda: metering.state)
    service = BudgetService(db_session=AsyncMock())
    response = await service.calculate_cost(CostCalculationRequest(model_name=FORWARDED, tokens_in=100, tokens_out=50))
    assert response.cost_usd == Decimal(".001750")
    assert response.input_cost_per_1k_tokens == Decimal(".005")
    assert calculate_model_cost(FORWARDED, 100, 50) == (Decimal(".001750"), Decimal(".005"), Decimal(".025"))
    service.record_cost = AsyncMock()
    await service.record_usage(metering.context, tokens_in=100, tokens_out=50, model=FORWARDED)
    assert all(call.args[0].request_cost_usd == Decimal(".001750") for call in service.record_cost.await_args_list)


@pytest.mark.asyncio
@pytest.mark.parametrize("model,unknown", [("claude-3.5-sonnet", False), ("anthropic.claude-unpublished-test", True)])
@pytest.mark.parametrize("stream", [False, True])
async def test_older_and_unknown_claude_still_settle_with_explicit_bundled_estimates(metering, model, unknown, stream):
    usage = {
        "input_tokens": 1000,
        "output_tokens": 500,
        "cache_read_input_tokens": 200,
        "cache_creation_input_tokens": 100,
        "cache_creation": {"ephemeral_5m_input_tokens": 100, "ephemeral_1h_input_tokens": 0},
    }
    proxy = ProxyService(MockPoolService(Client(usage)))
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[routes.get_token_context] = lambda: metering.context
    app.dependency_overrides[routes.get_proxy_service] = lambda: proxy
    operation = "invoke-with-response-stream" if stream else "invoke"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
        response = await http.post(f"/model/{model}/{operation}", json={"messages": [], "max_tokens": 80}, headers={"accept": "text/event-stream"})
    assert response.status_code == 200, response.text
    await flush_logs()
    metering.writer.write_log.assert_awaited_once()
    event = metering.writer.write_log.await_args.kwargs["log_data"]
    decision = event["pricing_decision"]
    assert decision["source_kind"] == "bundled_snapshot"
    assert decision["source"] == "bundled_snapshot"
    assert decision["source_url"].startswith("bundled://pricing_policy/snapshots/")
    assert decision["generation_id"] is None
    assert {"bootstrap_fallback", "unsupported_variant"} <= set(decision["estimate_reasons"])
    assert ("unknown_model" in decision["estimate_reasons"]) is unknown
    assert decision["confidence"] == "estimated"
    assert verify_pricing_decision(decision, request_id=event["request_id"], org_id=event["org_id"]) == Decimal(".010935")
    assert metering.usage.log_request.await_args.kwargs["cost_usd"] == Decimal(".010935")
    assert Decimal(decision["rates"]["cache_read_price_per_1k_tokens"]) == Decimal(".0003")
    assert Decimal(decision["rates"]["cache_write_price_per_1k_tokens"]) == Decimal(".00375")
    assert decision["rates"]["cache_write_1h_price_per_1k_tokens"] is None


def test_older_claude_quote_preserves_the_curated_model_price():
    from src.budget.pricing import PricingService
    from src.budget.utils import calculate_model_cost

    service = PricingService()
    expected = (Decimal(".010500"), Decimal(".003"), Decimal(".015"))
    assert service.quote_cost("claude-3-5-sonnet-20241022", 1000, 500) == expected
    assert calculate_model_cost("claude-3-5-sonnet-20241022", 1000, 500) == expected


class LateStreamFailureClient(Client):
    async def invoke_model_with_response_stream(self, **kwargs):
        response = await super().invoke_model_with_response_stream(**kwargs)
        original = response["body"]

        def events():
            yield from original
            raise RuntimeError("socket failure after final usage")

        response["body"] = events()
        return response


def settlement_wrapper(stream, capture, metering):
    from src.chat_logging.service import create_streaming_logging_wrapper

    logger = ChatLoggingService(s3_writer=metering.writer, enabled=True, scrub_level=ScrubLevel.BASIC)
    return create_streaming_logging_wrapper(
        stream=stream,
        chat_logger=logger,
        request_id=capture.request_id,
        timestamp=datetime.now(UTC),
        org_id=metering.context.attributed_org_id,
        user_id=metering.context.user_id,
        team_id=None,
        account_type="human",
        model=FORWARDED,
        api_format="bedrock",
        request_body={},
        headers={},
        start_time=0,
        pricing_capture=capture,
    )


@pytest.mark.asyncio
async def test_late_stream_error_still_emits_exactly_one_measured_settlement(metering):
    proxy = ProxyService(MockPoolService(LateStreamFailureClient(USAGE)))
    capture = PricingCapture("late-stream-error", FORWARDED)
    stream = await proxy.invoke_model(FORWARDED, {"messages": [], "max_tokens": 80}, metering.context, stream=True, pricing_capture=capture)
    wrapped = settlement_wrapper(stream, capture, metering)
    with pytest.raises(Exception, match="socket failure after final usage"):
        async for _ in wrapped:
            pass
    await flush_logs()
    metering.usage.log_request.assert_awaited_once()
    metering.writer.write_log.assert_awaited_once()
    event = metering.writer.write_log.await_args.kwargs["log_data"]
    assert event["pricing_decision"] == capture.decision
    assert verify_pricing_decision(capture.decision, request_id=capture.request_id, org_id=event["org_id"]) == Decimal(".007650")


@pytest.mark.asyncio
@pytest.mark.parametrize("after_final_usage", [False, True])
async def test_disconnect_at_yield_settles_only_final_measured_usage(metering, after_final_usage):
    proxy = ProxyService(MockPoolService(Client(USAGE)))
    capture = PricingCapture("disconnect-at-yield", FORWARDED)
    stream = await proxy.invoke_model(FORWARDED, {"messages": [], "max_tokens": 80}, metering.context, stream=True, pricing_capture=capture)
    wrapped = settlement_wrapper(stream, capture, metering)
    async for _ in wrapped:
        if not after_final_usage or "output_tokens" in capture.raw_usage:
            break
    await wrapped.aclose()
    await flush_logs()
    if after_final_usage:
        metering.writer.write_log.assert_awaited_once()
        metering.usage.log_request.assert_awaited_once()
        assert metering.writer.write_log.await_args.kwargs["log_data"]["pricing_decision"] == capture.decision
    else:
        assert capture.decision is None
        metering.writer.write_log.assert_not_awaited()
        metering.usage.log_request.assert_not_awaited()


@pytest.mark.asyncio
async def test_disconnect_during_async_pricing_cannot_cancel_settlement(metering, monkeypatch):
    started, release = asyncio.Event(), asyncio.Event()
    original = pricing_decisions.pricing_v2_reader.get_rate_state

    async def delayed_state(*args, **kwargs):
        started.set()
        await release.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(pricing_decisions.pricing_v2_reader, "get_rate_state", delayed_state)
    proxy = ProxyService(MockPoolService(Client(USAGE)))
    capture = PricingCapture("disconnect-during-pricing", FORWARDED)
    stream = await proxy.invoke_model(FORWARDED, {"messages": [], "max_tokens": 80}, metering.context, stream=True, pricing_capture=capture)
    wrapped = settlement_wrapper(stream, capture, metering)

    async def drain():
        async for _ in wrapped:
            pass

    consumer = asyncio.create_task(drain())
    await asyncio.wait_for(started.wait(), 2)
    consumer.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await consumer
    await capture.finalization_task
    finalizers = [task for task in asyncio.all_tasks() if task.get_name() == "chat_finalize_disconnect-during-pricing"]
    if finalizers:
        await asyncio.gather(*finalizers)
    await flush_logs()
    metering.writer.write_log.assert_awaited_once()
    metering.usage.log_request.assert_awaited_once()
    assert capture.decision is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/v1/messages", "/v1/chat/completions", "/model/custom-claude/invoke", "/bedrock/invoke"])
async def test_nonstream_translation_failure_preserves_settlement_and_error_response(metering, monkeypatch, path):
    proxy = ProxyService(MockPoolService(Client(USAGE)), model_resolver=ModelResolver(custom_aliases={"custom-claude": FORWARDED}))

    def fail_translation(*args, **kwargs):
        raise ValueError("translation failed after measured response")

    if path == "/v1/chat/completions":
        monkeypatch.setattr(proxy._translator, "bedrock_to_openai", fail_translation)
    elif path == "/v1/messages":
        monkeypatch.setattr(proxy._translator, "bedrock_to_anthropic", fail_translation)
    else:
        from src.proxy.schemas import BedrockInvokeResponse

        monkeypatch.setattr(BedrockInvokeResponse, "model_dump", fail_translation)
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[routes.get_token_context] = lambda: metering.context
    app.dependency_overrides[routes.get_proxy_service] = lambda: proxy
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
        response = await http.post(path, json={"model": "custom-claude", "messages": [], "max_tokens": 80})
    assert response.status_code == 500
    await flush_logs()
    metering.writer.write_log.assert_awaited_once()
    metering.usage.log_request.assert_awaited_once()
    event = metering.writer.write_log.await_args.kwargs["log_data"]
    assert verify_pricing_decision(event["pricing_decision"], request_id=event["request_id"], org_id=event["org_id"]) == Decimal(".007650")


@pytest.mark.asyncio
async def test_nonstream_cancellation_after_usage_preserves_settlement(metering, monkeypatch):
    started, release = asyncio.Event(), asyncio.Event()
    original = pricing_decisions.pricing_v2_reader.get_rate_state

    async def delayed_state(*args, **kwargs):
        started.set()
        await release.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(pricing_decisions.pricing_v2_reader, "get_rate_state", delayed_state)
    proxy = ProxyService(MockPoolService(Client(USAGE)))
    capture = PricingCapture("nonstream-cancel", FORWARDED)
    body = {"messages": [], "max_tokens": 80}
    consumer = asyncio.create_task(
        routes._invoke_with_failure_logging(
            proxy.invoke_model(FORWARDED, body, metering.context, pricing_capture=capture),
            SimpleNamespace(headers={}),
            metering.context,
            capture,
            body,
        )
    )
    await asyncio.wait_for(started.wait(), 2)
    consumer.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await consumer
    if routes._failure_finalizers:
        await asyncio.gather(*routes._failure_finalizers)
    await flush_logs()
    metering.writer.write_log.assert_awaited_once()
    metering.usage.log_request.assert_awaited_once()
    assert metering.writer.write_log.await_args.kwargs["log_data"]["pricing_decision"] == capture.decision


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["shadow", "reconcile", "usage"])
@pytest.mark.parametrize("mode", ["stream", "nonstream"])
@pytest.mark.parametrize("outcome", ["complete", "error", "cancel"])
async def test_blocked_persistence_cannot_withhold_measured_settlement(metering, monkeypatch, caplog, stage, mode, outcome):
    """A DB/Redis wait after pricing cannot retain a disconnected response forever."""
    from src.proxy import service as proxy_service

    monkeypatch.setattr(proxy_service, "CLAUDE_PERSISTENCE_TIMEOUT_SECONDS", 0.03)
    started = asyncio.Event()
    capture = PricingCapture(f"bounded-{stage}-{mode}-{outcome}", FORWARDED)

    async def blocked(*args, **kwargs):
        assert capture.decision is not None, "Pricing must precede optional I/O"
        started.set()
        await asyncio.Event().wait()

    if stage == "shadow":
        monkeypatch.setattr(proxy_service, "resolve_shadow_target", blocked)
    elif stage == "reconcile":
        metering.reconcile.side_effect = blocked
    else:
        metering.usage.log_request.side_effect = blocked
    client = LateStreamFailureClient(USAGE) if mode == "stream" and outcome == "error" else Client(USAGE)
    proxy = ProxyService(MockPoolService(client))
    body = {"messages": [], "max_tokens": 80}
    request = SimpleNamespace(headers={})

    async def run():
        if mode == "stream":
            source = await proxy.invoke_model(FORWARDED, body, metering.context, stream=True, pricing_capture=capture)
            async for _ in settlement_wrapper(source, capture, metering):
                pass
        else:

            async def invoke():
                response = await proxy.invoke_model(FORWARDED, body, metering.context, pricing_capture=capture)
                if outcome == "error":
                    raise RuntimeError("response error after measured usage")
                return response

            await routes._invoke_with_failure_logging(invoke(), request, metering.context, capture, body)
            routes.get_chat_logging_service().log_chat_async(
                **routes._pricing_log_args(request, metering.context, capture, body),
                response_body=capture.response_body,
                latency_ms=0,
            )

    consumer = asyncio.create_task(run())
    await asyncio.wait_for(started.wait(), 1)
    if outcome == "cancel":
        consumer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(consumer, 1)
    elif outcome == "error":
        with pytest.raises(Exception, match="socket failure after final usage|response error after measured usage"):
            await asyncio.wait_for(consumer, 1)
    else:
        await asyncio.wait_for(consumer, 1)
    from src.chat_logging.service import _stream_finalizers

    pending = [task for task in (*_stream_finalizers, *routes._failure_finalizers) if capture.request_id in task.get_name()]
    if pending:
        await asyncio.wait_for(asyncio.gather(*pending), 1)
    await flush_logs()
    assert capture.finalization_task.done() and not capture.finalization_task.cancelled()
    metering.writer.write_log.assert_awaited_once()
    event = metering.writer.write_log.await_args.kwargs["log_data"]
    assert event["pricing_decision"] == capture.decision
    assert event["org_id"] == metering.context.attributed_org_id
    assert verify_pricing_decision(capture.decision, request_id=capture.request_id, org_id=event["org_id"]) == Decimal(".007650")
    assert "Claude usage persistence timed out" in caplog.text
    assert not any(c.kwargs.get("actual_cost_usd") == Decimal("0") for c in metering.reconcile.await_args_list)


@pytest.mark.asyncio
async def test_pricing_has_its_own_read_budget_before_persistence_timeout(metering, monkeypatch):
    from src.proxy import service as proxy_service

    monkeypatch.setattr(proxy_service, "CLAUDE_PERSISTENCE_TIMEOUT_SECONDS", 0.01)
    original = pricing_decisions.pricing_v2_reader.get_rate_state

    async def slow_price(*args, **kwargs):
        await asyncio.sleep(0.03)
        return await original(*args, **kwargs)

    monkeypatch.setattr(pricing_decisions.pricing_v2_reader, "get_rate_state", slow_price)
    proxy = ProxyService(MockPoolService(Client(USAGE)))
    capture = PricingCapture("price-before-persistence-budget", FORWARDED)
    await proxy.invoke_model(FORWARDED, {"messages": [], "max_tokens": 80}, metering.context, pricing_capture=capture)
    assert capture.decision is not None
    metering.usage.log_request.assert_awaited_once()
    assert metering.usage.log_request.await_args.kwargs["cost_usd"] == Decimal(".007650")


@pytest.mark.asyncio
async def test_missing_usage_release_is_bounded_without_fabricating_settlement(metering, monkeypatch, caplog):
    from src.proxy import service as proxy_service

    monkeypatch.setattr(proxy_service, "CLAUDE_PERSISTENCE_TIMEOUT_SECONDS", 0.01)

    async def blocked_release(*args, **kwargs):
        await asyncio.Event().wait()

    metering.reconcile.side_effect = blocked_release
    proxy = ProxyService(MockPoolService(Client({})))
    capture = PricingCapture("unpriced-release-timeout", FORWARDED)
    await asyncio.wait_for(proxy.invoke_model(FORWARDED, {"messages": [], "max_tokens": 80}, metering.context, pricing_capture=capture), 1)
    assert capture.decision is None and capture.finalization_task.done()
    metering.usage.log_request.assert_not_awaited()
    metering.writer.write_log.assert_not_awaited()
    assert "Claude usage could not be priced" in caplog.text
    assert "Claude usage persistence timed out" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_upstream_failure_keeps_error_log_without_a_settlement_charge(metering, stream):
    client = Client(USAGE)
    client.invoke_model = AsyncMock(side_effect=RuntimeError("Bedrock unavailable"))
    client.invoke_model_with_response_stream = AsyncMock(side_effect=RuntimeError("Bedrock unavailable"))
    proxy = ProxyService(MockPoolService(client))
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[routes.get_token_context] = lambda: metering.context
    app.dependency_overrides[routes.get_proxy_service] = lambda: proxy
    operation = "invoke-with-response-stream" if stream else "invoke"
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        await http.post(f"/model/{FORWARDED}/{operation}", json={"messages": [], "max_tokens": 80})
    await flush_logs()
    metering.usage.log_request.assert_awaited_once()
    logged = metering.usage.log_request.await_args.kwargs
    assert logged["status_code"] == 500
    assert logged["input_tokens"] == logged["output_tokens"] == 0
    assert logged["cost_usd"] == Decimal("0")
    metering.reconcile.assert_awaited_once()
    assert metering.reconcile.await_args.kwargs["actual_cost_usd"] == Decimal("0")
    metering.writer.write_log.assert_not_awaited()
