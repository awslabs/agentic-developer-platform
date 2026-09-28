"""Historical usage is settled at acceptance; unknown provider usage stays held."""

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import fakeredis.aioredis
import pytest

from src.budget.config import budget_config
from src.budget.reservations import ReservationStore
from src.orchestration import flow_budget, shared_policy
from src.orchestration.flow_meter import meter_target


@pytest.fixture
async def meter(monkeypatch):
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    clock = [1000.0]
    store = ReservationStore(redis_url=None, ttl_seconds=120, clock=lambda: clock[0], client=client)
    monkeypatch.setattr(flow_budget, "_reservations", store)
    monkeypatch.setattr(budget_config, "budget_reservation_enabled", True)
    policy = SimpleNamespace(limits=SimpleNamespace(max_spend_usd=Decimal("50")))
    target = meter_target(org_id="org-baseline", flow_id="flow-baseline", policy=policy)
    try:
        yield SimpleNamespace(client=client, clock=clock, store=store, policy=policy, target=target)
    finally:
        await store.close()


async def initialize(meter, baseline):
    return await shared_policy.initialize_shared_meter(
        org_id="org-baseline", flow_id="flow-baseline", policy=meter.policy, marker={"prior_spend_usd": str(baseline)}
    )


@pytest.mark.parametrize("baseline", [Decimal("0"), Decimal("12.75")])
async def test_historical_baseline_remains_available_after_provider_receipt_window(meter, baseline):
    assert await initialize(meter, baseline)
    meter.clock[0] += 62 * 60
    snapshot = await meter.store.snapshot(meter.target)
    assert snapshot is not None
    assert snapshot.total_usd == baseline
    assert not snapshot.has_pending
    assert set(await meter.client.hkeys(meter.target.key())) == {"__initialized__", "__historical_spend__"}
    # The actual seeded dollars still consume the same allowance after 62 min.
    denied = await meter.store.reserve("over-cap", Decimal("50.01") - baseline, [meter.target])
    assert denied is not None and not denied.admitted
    admitted = await meter.store.reserve("within-cap", Decimal("50") - baseline, [meter.target])
    assert admitted is not None and admitted.admitted


async def test_actual_unknown_provider_call_still_blocks_after_receipt_window(meter):
    assert await initialize(meter, Decimal("12.75"))
    assert (await meter.store.reserve("provider-call", Decimal("2.50"), [meter.target])).admitted
    snapshot = await meter.store.snapshot(meter.target)
    assert snapshot.total_usd == Decimal("15.25") and snapshot.has_pending
    meter.clock[0] += 62 * 60
    assert await meter.store.snapshot(meter.target) is None
    assert await meter.store.reserve("next-call", Decimal("1"), [meter.target]) is None
    entries = await meter.client.hgetall(meter.target.key())
    assert "pending:provider-call" in entries
    assert Decimal(entries["__historical_spend__"].split(":")[0]) == Decimal("12.75")


async def test_acceptance_refuses_unacknowledged_baseline_settlement(meter, monkeypatch):
    # ReservationStore.reconcile intentionally swallows transport failures. The
    # initializer must verify settlement through its actual snapshot afterward.
    monkeypatch.setattr(meter.store, "reconcile", AsyncMock(return_value=None))
    assert not await initialize(meter, Decimal("12.75"))
    snapshot = await meter.store.snapshot(meter.target)
    assert snapshot.total_usd == Decimal("12.75") and snapshot.has_pending
