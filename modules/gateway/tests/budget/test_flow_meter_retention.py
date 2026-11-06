"""Old flow deadlines must not erase a live delivery's only denominator."""

import asyncio
from dataclasses import replace
from decimal import Decimal

import fakeredis.aioredis
import pytest

from src.budget.reservations import ReservationStore, ReservationTarget


@pytest.fixture
async def meter():
    async with fakeredis.aioredis.FakeRedis(decode_responses=True) as client:
        clock = [200000.0]
        store = ReservationStore(None, 120, client=client, clock=lambda: clock[0])
        target = ReservationTarget("tenant", "flow", "flow:id:models", "run", "lifetime", Decimal(100), 31 * 86400, True)
        yield client, store, target, clock


async def seed(client, target, **extra):
    await client.hset(target.key(), mapping={"__initialized__": "0:172800", "settled": "17.125000:172800", **extra})
    await client.expire(target.key(), 100)


async def test_existing_expired_charges_retain_exact_amounts_and_enforce_same_cap(meter):
    client, store, target, _ = meter
    await seed(client, target, failed="5.50:172800", **{"bounded:failed": "0:172800"})
    assert await store.extend_flow_retention(target)
    entries = await client.hgetall(target.key())
    assert entries["settled"].split(":")[0] == "17.125000"
    assert entries["failed"].split(":")[0] == "5.50"
    assert set(entries) == {"__initialized__", "settled", "failed", "bounded:failed"}
    assert (await store.snapshot(target)).total_usd == Decimal("22.625")
    assert await client.ttl(target.key()) >= 31 * 86400 - 1
    assert not (await store.reserve("too-much", Decimal("77.376"), [target])).admitted
    assert (await store.reserve("fits", Decimal("77.375"), [target])).admitted


async def test_actual_spending_boundary_upgrades_old_charges_before_cap_check(meter):
    client, store, target, _ = meter
    await seed(client, target)
    result = await store.reserve("next", Decimal(90), [target])
    assert result is not None and not result.admitted
    assert (await store.snapshot(target)).total_usd == Decimal("17.125")
    assert not await client.hexists(target.key(), "next")


async def test_pending_deadline_does_not_move_or_become_available(meter):
    client, store, target, _ = meter
    await seed(client, target, running="4.00:172800", **{"pending:running": "0:190000"})
    assert await store.extend_flow_retention(target)
    assert await client.hget(target.key(), "pending:running") == "0:190000"
    assert await store.snapshot(target) is None
    assert await store.reserve("next", Decimal(1), [target]) is None
    assert await store.reconcile_receipt("running", Decimal("1.5"), target)
    assert (await store.snapshot(target)).total_usd == Decimal("18.625")


async def test_unbounded_and_retained_markers_stay_unknown_and_persistent(meter):
    client, store, target, _ = meter
    await seed(client, target, native="0:172800", **{"unbounded:native": "0:1000", "retained:native": "0:1000"})
    await client.persist(target.key())
    assert await store.extend_flow_retention(target)
    assert await client.hget(target.key(), "unbounded:native") == "0:1000"
    assert await client.hget(target.key(), "retained:native") == "0:1000"
    assert await client.ttl(target.key()) == -1
    assert await store.snapshot(target) is None


async def test_missing_meter_stays_missing(meter):
    client, store, target, _ = meter
    assert not await store.extend_flow_retention(target)
    assert await store.reserve("next", Decimal(1), [target]) is None
    assert not await client.exists(target.key())


@pytest.mark.parametrize("change", ["amount", "add", "delete", "missing"])
async def test_operator_compare_and_swap_refuses_changed_inventory(meter, change):
    client, store, target, _ = meter
    await seed(client, target)
    expected = await client.hgetall(target.key())
    if change == "amount":
        await client.hset(target.key(), "settled", "18:172800")
    elif change == "add":
        await client.hset(target.key(), "new", "1:172800")
    elif change == "delete":
        await client.hdel(target.key(), "settled")
    else:
        await client.delete(target.key())
    before = await client.hgetall(target.key())
    assert not await store.extend_flow_retention(target, expected_entries=expected)
    assert await client.hgetall(target.key()) == before


@pytest.mark.parametrize(
    "bad",
    [
        {"settled": "NaN:123"},
        {"settled": "Infinity:123"},
        {"settled": "-1:123"},
        {"settled": "1:nan"},
        {"settled": "1:Infinity"},
        {"settled": "broken"},
        {"__initialized__": "1:123"},
        {"pending:orphan": "0:123"},
        {"bounded:settled": "1:123"},
    ],
)
async def test_malformed_hash_refused_without_partial_mutation(meter, bad):
    client, store, target, _ = meter
    await seed(client, target, **bad)
    before = await client.hgetall(target.key())
    ttl = await client.ttl(target.key())
    assert not await store.extend_flow_retention(target)
    assert await client.hgetall(target.key()) == before
    assert ttl - 1 <= await client.ttl(target.key()) <= ttl


async def test_maintenance_does_not_overwrite_a_concurrent_finalizer(meter):
    client, store, target, _ = meter
    await seed(client, target, live="4.0:300000", **{"pending:live": "0:203000"})
    await asyncio.gather(store.extend_flow_retention(target), store.reconcile_receipt("live", Decimal("0.25"), target))
    assert (await store.snapshot(target)).total_usd == Decimal("17.375")
    assert not await client.hexists(target.key(), "pending:live")


async def test_repeated_maintenance_coalesces_field_writes_and_preserves_longer_ttl(meter):
    client, store, target, clock = meter
    await seed(client, target)
    assert await store.extend_flow_retention(target)
    before = await client.hgetall(target.key())
    await client.expire(target.key(), 40 * 86400)
    clock[0] += 60
    assert await store.extend_flow_retention(target)
    assert await client.hgetall(target.key()) == before
    assert await client.ttl(target.key()) >= 40 * 86400 - 1


async def test_other_tenant_and_hierarchy_are_untouched(meter):
    client, store, target, _ = meter
    foreign = replace(target, org_id="other")
    hierarchy = replace(target, entity_type="user", require_initialization=False)
    for item in (target, foreign, hierarchy):
        await seed(client, item)
    before = await client.hgetall(foreign.key())
    assert await store.extend_flow_retention(target)
    assert not await store.extend_flow_retention(hierarchy)
    assert await client.hgetall(foreign.key()) == before
    assert await client.hgetall(hierarchy.key()) == before


async def test_late_finalizer_cannot_delete_strict_charge(meter):
    client, store, target, _ = meter
    await seed(client, target)
    before = await client.hget(target.key(), "settled")
    await store.reconcile("settled", Decimal(0), [target])
    assert await client.hget(target.key(), "settled") == before
    # The ordinary hierarchy accumulator still uses its settlement-lag TTL.
    hierarchy = replace(target, entity_type="user", require_initialization=False)
    await client.hset(hierarchy.key(), "settled", "17.125:172800")
    await store.reconcile("settled", Decimal(0), [hierarchy])
    assert not await client.hexists(hierarchy.key(), "settled")
