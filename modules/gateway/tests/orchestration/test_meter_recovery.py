"""Missing-meter recovery must not manufacture a single dollar of headroom."""

import asyncio
from dataclasses import replace
from decimal import Decimal

import fakeredis.aioredis
import pytest

from src.budget.reservations import ReservationStore, ReservationTarget
from src.orchestration.meter_recovery import exhausted_inventory, release_recovery_hold, restore_exhausted_flow_meter

RECOVERY = "1d1022cc-445f-4f94-a9ac-ed6ce52c1350"


@pytest.fixture
async def meter():
    async with fakeredis.aioredis.FakeRedis(decode_responses=True) as client:
        store = ReservationStore(None, 120, client=client, clock=lambda: 1000)
        target = ReservationTarget("org", "flow", "flow:example:models", "run", "lifetime", Decimal(1500), 31 * 86400, True)
        yield client, store, target


async def test_restoration_preserves_receipts_and_reserves_all_remaining_exposure(meter):
    client, store, target = meter
    report = await restore_exhausted_flow_meter(store, target, receipts={"request": Decimal("502.943945")}, recovery_id=RECOVERY)
    assert report["recorded_spend_usd"] == "502.943945"
    assert report["unreconciled_hold_usd"] == "997.056055"
    assert (await store.snapshot(target)).total_usd == Decimal(1500)
    assert await client.hget(target.key(), "request") == "502.943945:2679400"
    assert not (await store.reserve("new", Decimal("0.000001"), [target])).admitted
    assert await client.ttl(target.key()) >= 31 * 86400 - 1


@pytest.mark.parametrize("partial", [True, False])
async def test_existing_meter_or_second_apply_never_overwrites(meter, partial):
    client, store, target = meter
    if partial:
        await client.hset(target.key(), "retained-charge", "20:2000")
    else:
        await restore_exhausted_flow_meter(store, target, receipts={}, recovery_id=RECOVERY)
    before = await client.hgetall(target.key())
    with pytest.raises(ValueError, match="Meter exists"):
        await restore_exhausted_flow_meter(store, target, receipts={}, recovery_id=RECOVERY)
    assert await client.hgetall(target.key()) == before


async def test_concurrent_recovery_installs_one_inventory_only(meter):
    _, store, target = meter
    results = await asyncio.gather(
        *(restore_exhausted_flow_meter(store, target, receipts={"request": Decimal(5)}, recovery_id=RECOVERY) for _ in range(2)),
        return_exceptions=True,
    )
    assert sum(isinstance(r, dict) for r in results) == 1
    assert sum(isinstance(r, ValueError) for r in results) == 1
    assert (await store.snapshot(target)).total_usd == Decimal(1500)


async def test_new_allowance_requires_an_independently_increased_target(meter):
    _, store, target = meter
    await restore_exhausted_flow_meter(store, target, receipts={}, recovery_id=RECOVERY)
    assert not (await store.reserve("next", Decimal(300), [target])).admitted
    # The caller must obtain this target from the existing human-approved budget
    # path. Recovery itself never constructs or persists an increased policy.
    increased = replace(target, headroom_usd=Decimal(2500))
    assert (await store.reserve("next", Decimal(300), [increased])).admitted
    assert (await store.snapshot(target)).total_usd == Decimal(1800)


@pytest.mark.parametrize(
    "receipts,ceiling",
    [
        ({"request": Decimal(1501)}, Decimal(1500)),
        ({"request": Decimal(-1)}, Decimal(1500)),
        ({"request": Decimal("NaN")}, Decimal(1500)),
        ({"__initialized__": Decimal(0)}, Decimal(1500)),
        ({"pending:request": Decimal(0)}, Decimal(1500)),
        ({}, Decimal("Infinity")),
        ({}, Decimal(0)),
    ],
)
def test_invalid_inventory_or_ceiling_refused(receipts, ceiling):
    with pytest.raises(ValueError):
        exhausted_inventory(receipts=receipts, ceiling=ceiling, recovery_id=RECOVERY, deadline=2000)


async def test_nonflow_or_foreign_target_cannot_be_mutated(meter):
    client, store, target = meter
    for foreign in (replace(target, entity_type="user"), replace(target, require_initialization=False)):
        with pytest.raises(ValueError):
            await restore_exhausted_flow_meter(store, foreign, receipts={}, recovery_id=RECOVERY)
        assert not await client.exists(foreign.key())
    other = replace(target, org_id="other")
    await client.hset(other.key(), "__initialized__", "0:2000")
    await restore_exhausted_flow_meter(store, target, receipts={}, recovery_id=RECOVERY)
    assert await client.hgetall(other.key()) == {"__initialized__": "0:2000"}


async def test_owner_resolution_preserves_receipts_and_other_requests_and_replays_once(meter):
    client, store, target = meter
    decision = "00000000-0000-4000-8000-000000000001"
    await restore_exhausted_flow_meter(store, target, receipts={"paid": Decimal(500)}, recovery_id=RECOVERY)
    # Other request fields, including their exact deadlines, must survive.
    await client.hset(target.key(), mapping={"pending-call": "12:2000", "pending:pending-call": "0:1900"})
    before = await client.hgetall(target.key())
    args = dict(recovery_id=RECOVERY, expected_usd=Decimal(1000), decision_id=decision)
    assert await release_recovery_hold(store, target, **args)
    after = await client.hgetall(target.key())
    assert after["paid"] == before["paid"]
    assert after["pending-call"] == before["pending-call"]
    assert after["pending:pending-call"] == before["pending:pending-call"]
    assert after["recovery-" + RECOVERY].startswith("0:")
    assert "bounded:recovery-" + RECOVERY not in after
    assert await release_recovery_hold(store, target, **args)
    assert await client.hgetall(target.key()) == after
    assert not await release_recovery_hold(store, target, **{**args, "decision_id": "00000000-0000-4000-8000-000000000002"})


@pytest.mark.parametrize("change", ["amount", "missing", "pending", "unbounded", "retained", "ordinary"])
async def test_resolution_refuses_changed_or_nonrecovery_exposure(meter, change):
    client, store, target = meter
    await restore_exhausted_flow_meter(store, target, receipts={"paid": Decimal(500)}, recovery_id=RECOVERY)
    key = "recovery-" + RECOVERY
    if change == "amount":
        await client.hset(target.key(), key, "999:2000")
    elif change == "missing":
        await client.delete(target.key())
    elif change == "ordinary":
        await client.hdel(target.key(), "bounded:" + key)
    else:
        await client.hset(target.key(), change + ":" + key, "0:2000")
    before = await client.hgetall(target.key())
    assert not await release_recovery_hold(
        store, target, recovery_id=RECOVERY, expected_usd=Decimal(1000), decision_id="00000000-0000-4000-8000-000000000001"
    )
    assert await client.hgetall(target.key()) == before
