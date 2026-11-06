"""Recovered usage changes only live, explicitly attributed counters."""

from dataclasses import replace
from decimal import Decimal

import fakeredis.aioredis
import pytest

from src.budget.reservations import ReservationStore, ReservationTarget


@pytest.mark.parametrize("kind", ["run", "chain"])
async def test_receipt_preserves_deadline_ttl_and_tenant_isolation(kind):
    async with fakeredis.aioredis.FakeRedis(decode_responses=True) as redis:
        store = ReservationStore(None, 86400, client=redis, clock=lambda: 1000)
        target = ReservationTarget("tenant", kind, "reviewer", "run", "lifetime", Decimal(100), ttl_seconds=86400)
        foreign = replace(target, org_id="other")
        assert (await store.reserve("request", Decimal(30), [target, foreign])).admitted
        before = await redis.hget(target.key(), "request")
        ttl = await redis.ttl(target.key())
        assert await store.reconcile_receipt("request", Decimal("0.27"), target, lifetime_receipt=True)
        assert await redis.hget(target.key(), "request") == "0.27:" + before.split(":")[1]
        assert ttl - 1 <= await redis.ttl(target.key()) <= ttl
        assert await redis.hget(foreign.key(), "request") == before
        assert not await store.reconcile_receipt("missing", Decimal("0.27"), target, lifetime_receipt=True)
        expired = ReservationStore(None, 86400, client=redis, clock=lambda: 1000 + 86401)
        assert not await expired.reconcile_receipt("request", Decimal(0), target, lifetime_receipt=True)
        assert await redis.hget(target.key(), "request") == "0.27:" + before.split(":")[1]


async def test_unknown_cost_promotion_does_not_renew_or_recreate_counters():
    async with fakeredis.aioredis.FakeRedis(decode_responses=True) as redis:
        store = ReservationStore(None, 86400, client=redis, clock=lambda: 1000)
        target = ReservationTarget("tenant", "run", "reviewer", "run", "lifetime", Decimal(100), ttl_seconds=86400)
        missing = replace(target, entity_id="missing")
        assert (await store.reserve("request", Decimal(1), [target])).admitted
        before = await redis.hget(target.key(), "request")
        ttl = await redis.ttl(target.key())
        await store.retain_maximum("request", Decimal(30), [target, missing])
        assert await redis.hget(target.key(), "request") == "30:" + before.split(":")[1]
        assert ttl - 1 <= await redis.ttl(target.key()) <= ttl
        assert not await redis.exists(missing.key())
