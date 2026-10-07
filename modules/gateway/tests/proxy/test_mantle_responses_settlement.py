"""Reserved headroom is only released by usage the quote contract can trust.

These drive the real ``MantlePassthroughService.create_response`` — both the
streaming and non-streaming completion hooks — and assert on the arguments the
production ``reconcile_budget_reservation`` call receives. The rule under test:
``normalize_usage`` is deliberately forgiving (it clamps contradictory counters
and records the conflict rather than refusing, because the usage_logs row is
worth keeping either way), so settlement gated only on "a pricing decision
exists" would replace a real hold with an understated charge. Issue #5226 routes
that decision through the adapter that issued the bound.
"""

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import fakeredis.aioredis
import httpx
import pytest

from src.proxy import mantle_service
from src.shared.schemas.auth import TokenContext

MODEL = "openai.gpt-5.6-luna"


@pytest.fixture
def settlement(monkeypatch):
    """Real service, real _log_usage; only the ledger writes are captured."""
    monkeypatch.setattr("boto3.client", MagicMock())
    monkeypatch.setattr(mantle_service, "get_session_factory", lambda: lambda: AsyncMock())
    usage_service = MagicMock()
    usage_service.log_request = AsyncMock()
    monkeypatch.setattr(mantle_service, "UsageService", lambda db: usage_service)
    monkeypatch.setattr(mantle_service, "resolve_routing_decision", AsyncMock(return_value=mantle_service.RoutingDecision()))
    chat_logger = MagicMock()
    chat_logger.log_chat_async = MagicMock()
    monkeypatch.setattr(mantle_service, "ChatLoggingService", lambda: chat_logger, raising=False)
    reconcile = AsyncMock()
    monkeypatch.setattr(mantle_service, "reconcile_budget_reservation", reconcile)
    return reconcile


def context():
    return TokenContext(
        user_id="worker",
        org_id="__platform__",
        attributed_org_id="tenant",
        attributed_user_id="canonical-human",
        team_id="team",
        department_id="",
        account_type="service",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )


async def call(usage, *, stream):
    """Send one Responses call whose upstream reports ``usage``."""
    response = {"id": "resp_1", "output": [{"text": "hi"}]}
    if usage is not None:
        response["usage"] = usage
    payload = json.dumps(response).encode()
    wire = b'data: {"type":"response.completed","response":' + payload + b"}\n\n" if stream else payload
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, content=wire)))
    auth = MagicMock()
    auth.sign.return_value = {"Authorization": "upstream-credential"}
    service = mantle_service.MantlePassthroughService(auth, "https://bedrock-runtime.us-east-1.amazonaws.com", http_client=client)
    async with client:
        result = await service.create_response(
            json.dumps({"model": MODEL, "input": "hello", "max_output_tokens": 16}).encode(),
            context(),
            stream=stream,
            model=MODEL,
            request_id="req-1",
        )
        body = b"".join([chunk async for chunk in result]) if stream else result.content
    assert body == wire  # the passthrough is byte-exact either way
    return service


async def settled(settlement, usage, *, stream):
    """The usage_known flag the production hook passed for this response."""
    await call(usage, stream=stream)
    settlement.assert_awaited_once()
    kwargs = settlement.await_args.kwargs
    logged = mantle_service.UsageService(None).log_request.await_args.kwargs
    assert logged["reservation_usage_known"] is kwargs["usage_known"]
    return kwargs


@pytest.mark.parametrize("stream", [False, True])
async def test_a_complete_usage_block_settles_the_reservation(settlement, stream):
    kwargs = await settled(settlement, {"input_tokens": 500, "output_tokens": 16}, stream=stream)
    assert kwargs["usage_known"] is True
    assert kwargs["model_id"] == MODEL and kwargs["request_id"] == "req-1"


@pytest.mark.parametrize("stream", [False, True])
async def test_cached_input_settles_and_is_not_double_counted(settlement, stream):
    """On this API input_tokens ALREADY includes cached input."""
    usage = {"input_tokens": 500, "output_tokens": 16, "input_tokens_details": {"cached_tokens": 480}}
    kwargs = await settled(settlement, usage, stream=stream)
    assert kwargs["usage_known"] is True
    # The uncached remainder is charged at the full rate; the cached part is not
    # added on top, so the settled input cannot exceed the reported total.
    assert kwargs["input_tokens"] <= 500


@pytest.mark.parametrize("stream", [False, True])
async def test_reasoning_output_settles_within_the_reported_output(settlement, stream):
    usage = {"input_tokens": 100, "output_tokens": 16, "output_tokens_details": {"reasoning_tokens": 12}}
    kwargs = await settled(settlement, usage, stream=stream)
    assert kwargs["usage_known"] is True and kwargs["output_tokens"] <= 16


@pytest.mark.parametrize("stream", [False, True])
async def test_an_absent_usage_block_retains_the_hold(settlement, stream):
    kwargs = await settled(settlement, None, stream=stream)
    assert kwargs["usage_known"] is False
    assert not kwargs.get("retain_failed_bound", False)


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize(
    "usage",
    [
        pytest.param({"output_tokens": 16}, id="input_absent"),
        pytest.param({"input_tokens": 500}, id="output_absent"),
        pytest.param({"input_tokens": -1, "output_tokens": 16}, id="negative_input"),
        pytest.param({"input_tokens": "500", "output_tokens": 16}, id="input_not_a_count"),
    ],
)
async def test_incomplete_usage_retains_the_hold(settlement, usage, stream):
    kwargs = await settled(settlement, usage, stream=stream)
    assert kwargs["usage_known"] is False


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize(
    "usage",
    [
        pytest.param({"input_tokens": 10, "output_tokens": 5, "input_tokens_details": {"cached_tokens": 900}}, id="cached_exceeds_input"),
        pytest.param({"input_tokens": 10, "output_tokens": 5, "output_tokens_details": {"reasoning_tokens": 900}}, id="reasoning_exceeds_output"),
        pytest.param({"input_tokens": 10, "output_tokens": 5, "input_tokens_details": {"cached_tokens": -1}}, id="negative_cached"),
    ],
)
async def test_contradictory_usage_retains_the_hold(settlement, usage, stream):
    """A subset exceeding its own total means the counts disagree.

    The pricing path still writes a bounded, flagged row — that is intentional —
    but the reservation must NOT be released against numbers whose conflict was
    resolved by clamping rather than by measurement.
    """
    kwargs = await settled(settlement, usage, stream=stream)
    assert kwargs["usage_known"] is False


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize(
    "usage",
    [
        pytest.param({"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 900}, id="top_level_read_exceeds_input"),
        pytest.param({"input_tokens": 10, "output_tokens": 5, "cache_creation_input_tokens": 900}, id="top_level_write_exceeds_input"),
        pytest.param({"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": -1}, id="top_level_read_negative"),
        pytest.param({"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": "bad"}, id="top_level_read_not_a_count"),
        pytest.param(
            {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 6, "cache_creation_input_tokens": 6},
            id="top_level_read_plus_write_exceeds_input",
        ),
        pytest.param({"input_tokens": 10, "output_tokens": 5, "input_tokens_details": {"cache_write_tokens": 900}}, id="nested_write_exceeds_input"),
        pytest.param(
            {"input_tokens": 10, "output_tokens": 5, "input_tokens_details": {"cached_tokens": 6, "cache_write_tokens": 6}},
            id="nested_read_plus_write_exceeds_input",
        ),
    ],
)
async def test_every_cache_spelling_production_prices_also_retains_the_hold(settlement, usage, stream):
    """The trust gate must not be bypassable by which cache spelling arrives.

    ``normalize_usage`` reads cache reads and writes from EITHER a top-level
    counter or the nested details block, and clamps whichever will not fit. Each
    case below is one the pricing path clamps and flags ``valid=False``, so each
    must retain the reservation. Before #5333's repair only the nested
    ``cached_tokens`` spelling did, and the rest released a real hold against a
    clamped total — the same defect the gate was added to prevent, reachable
    under a different field name.
    """
    kwargs = await settled(settlement, usage, stream=stream)
    assert kwargs["usage_known"] is False


@pytest.mark.parametrize("stream", [False, True])
async def test_consistent_top_level_cache_usage_still_settles(settlement, stream):
    """The repair must not over-refuse: ordinary reported cache activity settles.

    Reads and writes that fit inside the inclusive input total are consistent
    evidence, so the hold is released — and cache is still a subset rather than
    an addend, so the settled input cannot exceed the reported total.
    """
    usage = {"input_tokens": 500, "output_tokens": 16, "cache_read_input_tokens": 300, "cache_creation_input_tokens": 100}
    kwargs = await settled(settlement, usage, stream=stream)
    assert kwargs["usage_known"] is True
    assert kwargs["input_tokens"] <= 500


async def test_a_truncated_stream_retains_the_hold(settlement):
    """No response.completed event arrived, so there is no final usage at all.

    The upstream deltas are still forwarded verbatim, and because no terminal
    event arrived the stream is closed with an explicit ``error`` event rather
    than a fabricated completion (``upstream_stream_incomplete``, added by
    #5336). That trailer is the *only* thing appended: what matters here is that
    an interrupted stream yields no trustworthy final usage, so the reservation
    is retained instead of being settled at an understated amount.
    """
    wire = b'data: {"type":"response.output_text.delta","delta":"hi"}\n\n'
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, content=wire)))
    auth = MagicMock()
    auth.sign.return_value = {"Authorization": "upstream-credential"}
    service = mantle_service.MantlePassthroughService(auth, "https://bedrock-runtime.us-east-1.amazonaws.com", http_client=client)
    async with client:
        result = await service.create_response(b'{"model": "openai.gpt-5.6-luna", "input": "hello"}', context(), stream=True, model=MODEL)
        received = [chunk async for chunk in result]
    # Upstream bytes pass through unmodified, followed by the incomplete-stream
    # trailer; no response.completed is synthesized.
    assert b"".join(received[:-1]) == wire
    trailer = json.loads(received[-1].split(b"data: ", 1)[1])
    assert trailer["type"] == "error"
    assert trailer["code"] == "upstream_stream_incomplete"
    settlement.assert_awaited_once()
    assert settlement.await_args.kwargs["usage_known"] is False


@pytest.mark.parametrize("error", [httpx.ReadTimeout("quiet upstream"), httpx.RemoteProtocolError("connection lost"), None])
async def test_interrupted_stream_allows_retry_while_counting_full_bound(settlement, monkeypatch, error):
    from src.budget import enforcement_service
    from src.budget.config import budget_config
    from src.budget.reservations import ReservationStore, ReservationTarget
    from src.orchestration.provider_quotes import quote_request
    from tests.proxy.test_mantle_stream_lifecycle import DELTA, ScriptedStream

    body = json.dumps({"model": MODEL, "input": "hello", "max_output_tokens": 16, "stream": True}).encode()
    quote = await quote_request(body, "/openai/v1/responses")
    target = ReservationTarget(
        org_id="tenant",
        entity_type="flow",
        entity_id="flow",
        period_type="run",
        period_start="lifetime",
        headroom_usd=quote.total_usd * 2,
        ttl_seconds=172800,
        require_initialization=True,
    )
    ctx = context()
    ctx._policy_quote = quote
    ctx._policy_flow_target = target
    ctx._budget_admission_targets = [target]
    monkeypatch.setattr(budget_config, "budget_reservation_enabled", True)
    async with fakeredis.aioredis.FakeRedis(decode_responses=True) as redis:
        store = ReservationStore(redis_url=None, ttl_seconds=120, client=redis)
        budget = enforcement_service.BudgetEnforcementService()
        budget._reservations = store
        monkeypatch.setattr(enforcement_service, "budget_enforcement_service", budget)
        monkeypatch.setattr(mantle_service, "reconcile_budget_reservation", enforcement_service.reconcile_budget_reservation)
        assert (await store.reserve("__initialized__", Decimal(0), [replace(target, require_initialization=False)])).admitted
        assert (await store.reserve("failed", quote.total_usd, [target])).admitted
        before = await redis.hgetall(target.key())
        upstream = ScriptedStream([DELTA], error)
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=upstream))) as client:
            auth = MagicMock()
            auth.sign.return_value = {}
            proxy = mantle_service.MantlePassthroughService(auth, "https://bedrock-runtime.us-east-1.amazonaws.com", http_client=client)
            stream = await proxy.create_response(body, ctx, stream=True, model=MODEL, request_id="failed")
            received = b"".join([chunk async for chunk in stream])
        assert b'"type": "error"' in received and b"response.completed" not in received
        assert (await store.snapshot(target)).total_usd == quote.total_usd
        after = await redis.hgetall(target.key())
        assert after["failed"] == before["failed"]  # Neither amount nor expiry is reset.
        assert after["__initialized__"] == before["__initialized__"]
        assert "bounded:failed" in after and "pending:failed" not in after
        assert mantle_service.UsageService(None).log_request.await_args.kwargs["pricing_decision"] is None
        assert (await store.reserve("retry", quote.total_usd, [target])).admitted
        assert not (await store.reserve("over-cap", Decimal("0.01"), [target])).admitted
        await store.reconcile("failed", Decimal("0.01"), [target])
        assert (await store.snapshot(target)).total_usd == quote.total_usd + Decimal("0.01")
        assert "bounded:failed" not in await redis.hgetall(target.key())


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("status", [429, 500, 503])
async def test_an_upstream_error_does_not_settle_at_zero(settlement, stream, status):
    """An error body carries no usage; the hold stands rather than clearing."""
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(status, content=b'{"error":"upstream"}', headers={"x-amzn-requestid": "provider-error-id"})
        )
    )
    auth = MagicMock()
    auth.sign.return_value = {"Authorization": "upstream-credential"}
    service = mantle_service.MantlePassthroughService(auth, "https://bedrock-runtime.us-east-1.amazonaws.com", http_client=client)
    async with client:
        if stream:
            with pytest.raises(mantle_service.MantleUpstreamError) as error:
                await service.create_response(
                    b'{"model": "openai.gpt-5.6-luna", "input": "hello"}', context(), stream=True, model=MODEL, request_id="req-error"
                )
            assert error.value.status_code == status
        else:
            result = await service.create_response(
                b'{"model": "openai.gpt-5.6-luna", "input": "hello"}', context(), stream=False, model=MODEL, request_id="req-error"
            )
            assert result.status_code == status
    settlement.assert_awaited_once()
    assert settlement.await_args.kwargs["usage_known"] is False
    assert settlement.await_args.kwargs.get("retain_failed_bound", False) is (status >= 500)
    logged = mantle_service.UsageService(None).log_request.await_args.kwargs
    assert logged["provider_request_id"] == "provider-error-id"
    assert logged["pricing_decision"] is None


async def test_the_trust_decision_comes_from_the_routes_own_adapter(monkeypatch):
    """The proxy must ask the registered adapter, never re-implement the rule."""
    from src.orchestration.provider_quotes import TrustedUsage

    asked = []

    class Recording:
        async def reconcile(self, response):
            asked.append(response)
            return TrustedUsage.unknown("adapter said no")

    monkeypatch.setattr(mantle_service, "MANTLE_RESPONSES_PATH", mantle_service.MANTLE_RESPONSES_PATH)
    monkeypatch.setattr("src.orchestration.provider_quotes.adapter_for", lambda path: Recording())
    trusted = await mantle_service.MantlePassthroughService._trusted_usage({"input_tokens": 5, "output_tokens": 1})
    assert not trusted.known and trusted.reason == "adapter said no"
    # It was handed the usage under the key the adapter contract reads.
    assert asked == [{"usage": {"input_tokens": 5, "output_tokens": 1}}]


async def test_an_adapter_failure_is_not_a_grant_of_trust(monkeypatch):
    """Failing to establish trust must never be treated as having established it."""

    class Exploding:
        async def reconcile(self, response):
            raise RuntimeError("adapter blew up")

    monkeypatch.setattr("src.orchestration.provider_quotes.adapter_for", lambda path: Exploding())
    trusted = await mantle_service.MantlePassthroughService._trusted_usage({"input_tokens": 5, "output_tokens": 1})
    assert not trusted.known


async def test_a_route_with_no_adapter_meters_as_before(monkeypatch):
    """No adapter means no reservation to protect: ordinary metering is unchanged."""
    monkeypatch.setattr("src.orchestration.provider_quotes.adapter_for", lambda path: None)
    trusted = await mantle_service.MantlePassthroughService._trusted_usage({})
    assert trusted.known


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_sql_failure_does_not_release_mantle_reservation(settlement, monkeypatch, stream):
    service = MagicMock()
    service.log_request = AsyncMock(side_effect=RuntimeError("ledger unavailable"))
    monkeypatch.setattr(mantle_service, "UsageService", lambda db: service)
    await call({"input_tokens": 500, "output_tokens": 16}, stream=stream)
    assert settlement.await_count == 1
    assert settlement.await_args.kwargs["usage_known"] is False
