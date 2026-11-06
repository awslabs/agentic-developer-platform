"""Reservation display stays read-only and keeps tenant/run scope."""

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import fakeredis.aioredis

from src.activity import budget_usage
from src.budget.reservations import ReservationStore, ReservationTarget


async def test_recorded_charges_are_separate_from_outstanding_reservations(monkeypatch):
    async with fakeredis.aioredis.FakeRedis(decode_responses=True) as client:
        store = ReservationStore(None, 86400, client=client, clock=lambda: 1000)
        target = ReservationTarget("tenant", "run", "reviewer", "run", "lifetime", Decimal(100))
        assert (await store.reserve("settled", Decimal("40.947188"), [target])).admitted
        assert (await store.reserve("pending", Decimal("30.226680"), [target])).admitted
        monkeypatch.setattr(budget_usage.budget_enforcement_service, "_get_reservations", lambda: store)
        db = AsyncMock()
        db.execute.return_value = MagicMock(all=lambda: [("settled", Decimal("40.947188"))])
        item = SimpleNamespace(invocation_id="reviewer", channel="github", budget_usage=None)
        before = await client.hgetall(target.key())
        await budget_usage.enrich_budget_usage(db, item, tenant_id="tenant")
        assert item.budget_usage["outstanding_reservations_usd"] == 30.22668
        assert item.budget_usage["counter_total_usd"] == 71.173868
        assert await client.hgetall(target.key()) == before
        parameters = db.execute.await_args.args[0].compile().params
        assert parameters["org_id_1"] == "tenant" and parameters["agent_run_id_1"] == "reviewer"


async def test_unavailable_reservations_are_not_displayed_as_zero(monkeypatch):
    store = SimpleNamespace(enabled=True, live_request_amounts=AsyncMock(side_effect=RuntimeError("unavailable")))
    monkeypatch.setattr(budget_usage.budget_enforcement_service, "_get_reservations", lambda: store)
    item = SimpleNamespace(invocation_id="reviewer", channel="github", budget_usage=None)
    assert await budget_usage.enrich_budget_usage(AsyncMock(), item, tenant_id="tenant") is item
    assert item.budget_usage is None
