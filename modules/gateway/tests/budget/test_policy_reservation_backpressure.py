"""Real reservation Lua + HTTP gate for overlapping policy model requests."""

import asyncio
from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import fakeredis.aioredis
import pytest

from src.budget.config import budget_config
from src.budget.enforcement_service import BudgetEnforcementService
from src.budget.reservations import ReservationStore, ReservationTarget
from src.shared.schemas.budget import EnforcementResult
from tests.budget.test_budget_overshoot import _context, _Harness

QUOTE = Decimal("13.92")


@pytest.fixture
async def ledger(monkeypatch):
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    clock = [1000.0]
    store = ReservationStore(redis_url=None, ttl_seconds=86400, client=client, clock=lambda: clock[0])
    flow = ReservationTarget(
        org_id="org-456",
        entity_type="flow",
        entity_id="flow",
        period_type="run",
        period_start="lifetime",
        headroom_usd=Decimal(120),
        require_initialization=True,
    )
    run = replace(flow, entity_type="run", entity_id="run", headroom_usd=Decimal(25), require_initialization=False)
    chain = replace(run, entity_type="chain", entity_id="chain", headroom_usd=Decimal(100))
    targets = [flow, run, chain]
    assert (await store.reserve("__initialized__", Decimal(0), [replace(flow, require_initialization=False)])).admitted
    monkeypatch.setattr(budget_config, "budget_reservation_enabled", True)
    service = BudgetEnforcementService()
    service._reservations = store
    yield SimpleNamespace(client=client, clock=clock, store=store, flow=flow, run=run, chain=chain, targets=targets, service=service)
    await client.aclose()


async def request(ledger, *, request_id="next", targets=None, quote=QUOTE, strict=True):
    # The real service/Lua cap gate and real ASGI denial; unrelated DB identity
    # fixtures are covered by test_flow_meter's full hierarchy tests.
    async def check(context, estimated_cost, request_id=None, run_id=None):
        result = await ledger.service._reserve_or_degrade(request_id, quote, targets or ledger.targets, strict=strict)
        return result or EnforcementResult(allowed=True)

    harness = _Harness(
        SimpleNamespace(
            prepare_enforcement_context=AsyncMock(return_value=None),
            check_budget_hierarchy=check,
            estimate_cost_from_payload_size=ledger.service.estimate_cost_from_payload_size,
        )
    )
    await harness.post("/model/anthropic.claude-opus-5/invoke-with-response-stream", token_context=_context(), request_id=request_id)
    return harness


async def snapshot(ledger):
    return {target.key(): (await ledger.client.hgetall(target.key()), await ledger.client.ttl(target.key())) for target in ledger.targets}


async def test_overlapping_requests_wait_then_retry_without_releasing_another_hold(ledger):
    first = await request(ledger, request_id="first")
    assert first.status == 200 and first.app_invoked
    before = await snapshot(ledger)
    waiting = await request(ledger)
    assert waiting.status == 429 and not waiting.app_invoked
    assert waiting.body["error"] == "budget_reservations_pending"
    assert waiting.body["details"]["scope"] == "run"
    headers = dict(waiting.messages[0]["headers"])
    assert headers[b"retry-after"] == b"2"
    assert b"x-budget-remaining" not in headers
    assert await snapshot(ledger) == before  # No denied reservation or TTL reset.
    await ledger.store.reconcile("first", Decimal("0.12"), ledger.targets)
    retry = await request(ledger)
    assert retry.status == 200 and retry.app_invoked
    assert (await ledger.store.snapshot(ledger.flow)).total_usd == Decimal("14.04")


@pytest.mark.parametrize("actual", [Decimal(12), Decimal(25)])
async def test_receipt_can_turn_temporary_contention_into_real_exhaustion(ledger, actual):
    assert (await request(ledger, request_id="first")).status == 200
    assert (await request(ledger)).status == 429
    await ledger.store.reconcile("first", actual, ledger.targets)
    stopped = await request(ledger)
    assert stopped.status == 402 and not stopped.app_invoked
    assert stopped.body["error"] == "budget_exceeded"


@pytest.mark.parametrize("failure", ["unknown", "expired", "lost"])
async def test_unknown_receipt_or_meter_loss_stays_unavailable(ledger, failure):
    assert (await request(ledger, request_id="first")).status == 200
    if failure == "unknown":
        await ledger.store.mark_unknown("first", ledger.flow)
    elif failure == "expired":
        ledger.clock[0] += 3661
    else:
        await ledger.client.delete(ledger.flow.key())
    stopped = await request(ledger)
    assert stopped.status == 503 and not stopped.app_invoked
    assert stopped.body["error"] == "budget_check_unavailable"


async def test_unaffordable_single_quote_is_not_retryable_even_with_pending_usage(ledger):
    assert (await request(ledger, request_id="first")).status == 200
    stopped = await request(ledger, quote=Decimal(26))
    assert stopped.status == 402 and not stopped.app_invoked


async def test_settled_exhaustion_elsewhere_takes_precedence_over_pending_run(ledger):
    assert (await request(ledger, request_id="first")).status == 200
    # Unrelated settled chain usage cannot be discounted by the run's marker.
    assert (await ledger.store.reserve("settled-chain", Decimal(90), [ledger.chain])).admitted is False
    await ledger.store.reconcile("first", Decimal("0.12"), [ledger.chain])
    assert (await ledger.store.reserve("settled-chain", Decimal(90), [ledger.chain])).admitted
    stopped = await request(ledger)
    assert stopped.status == 402 and not stopped.app_invoked
    assert stopped.body["details"]["scope"] == "chain"


async def test_non_policy_denial_keeps_legacy_402(ledger):
    assert (await request(ledger, request_id="first", targets=[ledger.run], strict=False)).status == 200
    stopped = await request(ledger, targets=[ledger.run], strict=False)
    assert stopped.status == 402 and not stopped.app_invoked


async def test_distinct_concurrent_calls_still_cannot_exceed_live_cap(ledger):
    results = await asyncio.gather(*(request(ledger, request_id=f"request-{i}") for i in range(8)))
    assert [row.status for row in results].count(200) == 1
    assert [row.status for row in results].count(429) == 7
    assert (await ledger.store.snapshot(ledger.flow)).total_usd == QUOTE


async def test_pending_marker_cannot_discount_a_different_settled_request(ledger):
    assert (await ledger.store.reserve("spent", Decimal(12), [ledger.run])).admitted
    assert (await ledger.store.reserve("pending-other-run", Decimal(1), [ledger.flow])).admitted
    stopped = await request(ledger)
    assert stopped.status == 402 and not stopped.app_invoked


async def test_target_order_does_not_change_contention_classification(ledger):
    assert (await request(ledger, request_id="first")).status == 200
    waiting = await request(ledger, targets=list(reversed(ledger.targets)))
    assert waiting.status == 429 and not waiting.app_invoked


async def test_reenabling_a_legacy_cap_waits_for_off_mode_usage(ledger):
    target = ledger.targets[1]
    assert not target.require_initialization
    assert await ledger.store.observe("uncapped-call", [target])
    denied = await request(ledger, targets=[target], strict=False)
    assert denied.status == 503 and not denied.app_invoked
    await ledger.store.reconcile("uncapped-call", Decimal("0.12"), [target])
    assert (await request(ledger, targets=[target], strict=False)).status == 200


async def test_explicit_failed_bound_preserves_amount_and_cap_until_trusted_receipt(ledger):
    assert (await request(ledger, request_id="first")).status == 200
    before = await snapshot(ledger)
    await ledger.store.mark_unknown("first", ledger.flow)
    assert await ledger.store.snapshot(ledger.flow) is None
    assert await ledger.store.retain_failed_bound("first", ledger.flow)
    assert await ledger.store.retain_failed_bound("first", ledger.flow)  # idempotent recovery
    after = await snapshot(ledger)
    for target in ledger.targets:
        assert after[target.key()][0]["first"] == before[target.key()][0]["first"]
    assert after[ledger.flow.key()][0]["__initialized__"] == before[ledger.flow.key()][0]["__initialized__"]
    assert "bounded:first" in after[ledger.flow.key()][0]
    assert "pending:first" not in after[ledger.flow.key()][0]
    assert (await ledger.store.snapshot(ledger.flow)).total_usd == QUOTE
    # The full bound still occupies the run cap: an ordinary full quote cannot fit.
    assert (await request(ledger)).status == 402
    assert (await request(ledger, quote=Decimal(1))).status == 200
    await ledger.store.reconcile("first", Decimal("0.12"), ledger.targets)
    assert "bounded:first" not in await ledger.client.hgetall(ledger.flow.key())
    assert (await ledger.store.snapshot(ledger.flow)).total_usd == Decimal("1.12")
    # A repeated failure observation cannot overwrite the later actual receipt.
    assert await ledger.store.retain_failed_bound("first", ledger.flow)
    assert (await ledger.store.snapshot(ledger.flow)).total_usd == Decimal("1.12")


@pytest.mark.parametrize("invalid", ["missing", "expired", "anchor", "unbounded"])
async def test_retaining_failure_never_repairs_missing_or_unbounded_accounting(ledger, invalid):
    assert (await request(ledger, request_id="first")).status == 200
    await ledger.store.mark_unknown("first", ledger.flow)
    if invalid == "missing":
        await ledger.client.hdel(ledger.flow.key(), "first")
    elif invalid == "expired":
        ledger.clock[0] += 86401
    elif invalid == "anchor":
        await ledger.client.hdel(ledger.flow.key(), "__initialized__")
    else:
        await ledger.client.hset(ledger.flow.key(), "unbounded:first", "0:1000")
    before = await ledger.client.hgetall(ledger.flow.key())
    assert not await ledger.store.retain_failed_bound("first", ledger.flow)
    assert await ledger.client.hgetall(ledger.flow.key()) == before
    assert await ledger.store.snapshot(ledger.flow) is None


async def test_bounded_failure_does_not_clear_an_unresolved_sibling(ledger):
    assert (await ledger.store.reserve("first", Decimal(2), [ledger.flow])).admitted
    assert (await ledger.store.reserve("sibling", Decimal(2), [ledger.flow])).admitted
    await ledger.store.mark_unknown("first", ledger.flow)
    await ledger.store.mark_unknown("sibling", ledger.flow)
    before = await ledger.client.hget(ledger.flow.key(), "pending:sibling")
    assert await ledger.store.retain_failed_bound("first", ledger.flow)
    assert await ledger.client.hget(ledger.flow.key(), "pending:sibling") == before
    assert await ledger.store.snapshot(ledger.flow) is None


@pytest.mark.parametrize("bounded", [False, True])
async def test_enforcement_retains_full_quote_only_for_explicit_server_failure(ledger, bounded):
    from tests.orchestration.test_flow_meter import _confirmable_quote

    assert (await request(ledger, request_id="first")).status == 200
    context = _context()
    context._policy_quote = await _confirmable_quote()
    context._policy_flow_target = ledger.flow
    context._budget_admission_targets = ledger.targets
    before = await snapshot(ledger)
    await ledger.service.reconcile_reservation(
        context,
        "first",
        "openai.gpt-6-astra",
        0,
        0,
        actual_cost_usd=Decimal(0),
        usage_known=False,
        retain_failed_bound=bounded,
    )
    after = await snapshot(ledger)
    for target in ledger.targets:
        assert after[target.key()][0]["first"] == before[target.key()][0]["first"]
    observed = await ledger.store.snapshot(ledger.flow)
    if bounded:
        assert observed.total_usd == QUOTE
        assert (await request(ledger, quote=Decimal(1))).status == 200
    else:
        assert observed is None
        assert (await request(ledger, quote=Decimal(1))).status == 503


@pytest.mark.parametrize("stream", [False, True])
async def test_http_500_to_real_ledger_allows_only_an_affordable_retry(ledger, monkeypatch, stream):
    from unittest.mock import MagicMock

    import httpx

    from src.budget import enforcement_service
    from src.proxy import mantle_service
    from tests.orchestration.test_flow_meter import _confirmable_quote

    monkeypatch.setattr("boto3.client", MagicMock())
    monkeypatch.setattr(enforcement_service, "budget_enforcement_service", ledger.service)
    monkeypatch.setattr(mantle_service, "get_session_factory", lambda: lambda: AsyncMock())
    usage = SimpleNamespace(log_request=AsyncMock())
    monkeypatch.setattr(mantle_service, "UsageService", lambda _: usage)
    monkeypatch.setattr(mantle_service, "resolve_routing_decision", AsyncMock(return_value=mantle_service.RoutingDecision()))
    assert (await request(ledger, request_id="first")).status == 200
    context = _context()
    context._policy_quote = await _confirmable_quote()
    context._policy_flow_target = ledger.flow
    context._budget_admission_targets = ledger.targets
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(500, json={"error": {"type": "server_error"}}, headers={"x-amzn-requestid": "provider-failure"})
        )
    )
    auth = MagicMock()
    auth.sign.return_value = {}
    service = mantle_service.MantlePassthroughService(auth, "https://bedrock-runtime.us-east-1.amazonaws.com", http_client=client)
    async with client:
        call = service.create_response(
            b'{"model":"openai.gpt-6-astra","input":"hello"}', context, stream=stream, model="openai.gpt-6-astra", request_id="first"
        )
        if stream:
            with pytest.raises(mantle_service.MantleUpstreamError):
                await call
        else:
            assert (await call).status_code == 500
    assert usage.log_request.await_args.kwargs["provider_request_id"] == "provider-failure"
    assert usage.log_request.await_args.kwargs["pricing_decision"] is None
    assert (await ledger.store.snapshot(ledger.flow)).total_usd == QUOTE
    assert (await request(ledger)).status == 402
    assert (await request(ledger, quote=Decimal(1))).status == 200
