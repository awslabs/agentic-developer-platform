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
