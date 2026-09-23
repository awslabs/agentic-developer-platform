"""Public dispatch and real v6 writers must share the v7 allocation fence."""

import asyncio

import pytest

from harness_jobs import apply, current_version, downgrade
from harness_jobs.allocation import lock_allocation
from harness_jobs.execution import OperationExecutor
from harness_jobs.identity import ContractViolation, OperationRefused
from harness_jobs.schema import UPGRADES

from .conftest import requires_postgres
from .test_inventory_postgres import (
    ALLOCATION,
    STEP,
    absent,
    authority,
    complete_the_plan,
    finish_allocation,
    leased,
    resource,
    spy_provider,
)

pytestmark = requires_postgres


@pytest.mark.parametrize(
    "allocation", ["", " \t\n", "\u2003\u00a0", "x" * 256, "a\x00b"]
)
async def test_malformed_allocation_never_persists_intent_or_calls_provider(
    pool, allocation
):
    _, lease = await leased(pool, allocation=allocation)
    invoked = []
    executor = OperationExecutor(
        lease, connect=pool.acquire, provider_call=spy_provider(invoked)
    )
    with pytest.raises(ContractViolation, match="allocation_id"):
        await executor.execute_provider(
            idempotency_key="invalid",
            **{k: STEP[k] for k in ("provider", "operation_kind", "target")},
        )
    assert invoked == []
    async with pool.acquire() as c:
        assert (
            await c.fetchval("SELECT count(*) FROM harness_provider_call_intent") == 0
        )


async def legacy_insert(c, operation_id, key="legacy"):
    # Exact v6 column set: the old runtime does not know allocation_id exists.
    await c.execute(
        """
        INSERT INTO harness_provider_call_intent
            (idempotency_key, operation_id, org_id, workspace_id, job_id,
             attempt_id, fence_token, provider, operation_kind, target, stage)
        SELECT $2, operation_id, org_id, workspace_id, job_id, attempt_id,
               1, 'aws', 'create_disk', 'account/111122223333', 'intended'
          FROM harness_operations WHERE operation_id=$1
    """,
        operation_id,
        key,
    )


async def ready_to_seal(pool):
    record, lease = await leased(pool, key="sealer", holder="sealer")
    service = authority(pool, lease)
    members = (resource("cluster-1"),)
    async with pool.acquire() as c:
        await service.enumerate_resources(c, lease, resources=members)
    await complete_the_plan(pool, record, lease, provider_ref="cluster-1-handle")
    async with pool.acquire() as c:
        attempt = await service.begin_provider_enumeration(c, lease, provider="aws")
        await service.record_provider_enumeration(
            c,
            lease,
            attempt=attempt,
            provider="aws",
            provider_references=frozenset({"cluster-1-handle"}),
        )
    return service, lease, members


@pytest.mark.parametrize("stage", ["intended", "unresolved", "observed"])
async def test_v6_inflight_call_blocks_new_operation_seal_until_accounted(pool, stage):
    old, _ = await leased(pool, key="old", holder="old")
    async with pool.acquire() as c:
        await downgrade(c, target=6)
        await legacy_insert(c, old.operation_id)
        if stage != "intended":
            await c.execute(
                "UPDATE harness_provider_call_intent SET stage=$1, outcome='unknown'",
                stage,
            )
        assert await apply(c) == 7
        assert (
            await c.fetchval("SELECT allocation_id FROM harness_provider_call_intent")
            == ALLOCATION
        )
    service, lease, members = await ready_to_seal(pool)
    async with pool.acquire() as c:
        with pytest.raises(OperationRefused, match="legacy"):
            await service.seal_allocation(c, lease)
        before, _ = await service._epoch(c, lease, ALLOCATION)
        # A v6 worker settles without ever writing the new allocation column.
        await c.execute(
            "UPDATE harness_provider_call_intent SET stage='observed', "
            "outcome='succeeded: created disk', provider_ref='disk-1-handle' "
            "WHERE idempotency_key='legacy'"
        )
        after, _ = await service._epoch(c, lease, ALLOCATION)
        assert after > before
        with pytest.raises(OperationRefused):
            await service.seal_allocation(c, lease)
        assert await c.fetchval("SELECT count(*) FROM harness_allocation_seal") == 0
        # Even a fresh listing cannot account for the successful call by omitting
        # its handle, including when the durable outcome carries provider detail.
        listing = await service.begin_provider_enumeration(c, lease, provider="aws")
        await service.record_provider_enumeration(
            c,
            lease,
            attempt=listing,
            provider="aws",
            provider_references=frozenset({"cluster-1-handle"}),
        )
        with pytest.raises(OperationRefused, match="legacy"):
            await service.seal_allocation(c, lease)
        members += (resource("disk-1", kind="storage"),)
        await service.enumerate_resources(c, lease, resources=members)
    assert await finish_allocation(pool, service, lease, members)
    # Complete accounting permits recovery, not an irreversible migration hold.
    from .test_inventory_postgres import ReleaseState, _publish

    observations = {item.resource_id: absent(item.resource_id) for item in members}
    async with pool.acquire() as c:
        await _publish(service, c, lease, observations=observations)
    result = await service.assess_cleanup(
        executor_id=lease.holder,
        workspace_id=lease.workspace_id,
        allocation_id=ALLOCATION,
        operation_authority="authority-token",
        observations=observations,
    )
    assert result.state is ReleaseState.RELEASED


@pytest.mark.parametrize("first", ["seal", "legacy"])
async def test_rolling_v6_insert_and_v7_seal_serialize(pool, first):
    old, _ = await leased(pool, key="old", holder="old")
    service, lease, _ = await ready_to_seal(pool)
    import asyncpg

    async def insert():
        async with pool.acquire() as c:
            await legacy_insert(c, old.operation_id)

    async def seal():
        async with pool.acquire() as c:
            return await service.seal_allocation(c, lease)

    async with pool.acquire() as c:
        async with c.transaction():
            await lock_allocation(
                c,
                org_id=lease.org_id,
                workspace_id=lease.workspace_id,
                allocation_id=ALLOCATION,
            )
            if first == "seal":
                task = asyncio.create_task(insert())
                await service.seal_allocation(c, lease)
            else:
                await legacy_insert(c, old.operation_id)
                task = asyncio.create_task(seal())
            await asyncio.sleep(0.05)
            assert not task.done()
        with pytest.raises((OperationRefused, asyncpg.RaiseError)):
            await asyncio.wait_for(task, 3)
        assert await c.fetchval("SELECT count(*) FROM harness_allocation_seal") == (
            first == "seal"
        )
        assert await c.fetchval(
            "SELECT count(*) FROM harness_provider_call_intent "
            "WHERE idempotency_key='legacy'"
        ) == (first == "legacy")


@pytest.mark.parametrize("prefix", range(len(UPGRADES[7]) + 1))
async def test_interrupted_upgrade_binds_existing_and_interleaved_v6_calls(
    pool, prefix
):
    old, _ = await leased(
        pool, key="old", holder="old", allocation="allocation-\U0001f680\u00e9"
    )
    async with pool.acquire() as c:
        await downgrade(c, target=6)
        await legacy_insert(c, old.operation_id, "before")
        for sql in UPGRADES[7][:prefix]:
            await c.execute(sql)
        await legacy_insert(c, old.operation_id, "during")
        assert await current_version(c) == 6
        assert await apply(c) == 7
        assert (
            await c.fetchval(
                "SELECT count(*) FROM harness_provider_call_intent "
                "WHERE allocation_id='allocation-\U0001f680\u00e9'"
            )
            == 2
        )
        before = await c.fetchval("SELECT generation FROM harness_allocation_epoch")
        await c.execute(
            "UPDATE harness_provider_call_intent SET stage='observed', "
            "outcome='unknown' WHERE idempotency_key='during'"
        )
        assert (
            await c.fetchval("SELECT generation FROM harness_allocation_epoch") > before
        )


@pytest.mark.parametrize("allocation", ["", "\u2003\u00a0", "x" * 256, "a\x00b"])
async def test_malformed_legacy_allocation_prevents_v7_activation(pool, allocation):
    old, _ = await leased(pool, allocation=allocation)
    import asyncpg

    async with pool.acquire() as c:
        await downgrade(c, target=6)
        await legacy_insert(c, old.operation_id)
        with pytest.raises(asyncpg.PostgresError):
            await apply(c)
        assert await current_version(c) == 6


async def test_corrupt_legacy_payload_prevents_activation_and_rolling_insert(pool):
    old, _ = await leased(pool)
    import asyncpg

    async with pool.acquire() as c:
        await downgrade(c, target=6)
        await legacy_insert(c, old.operation_id)
        await c.execute(
            "UPDATE harness_operations SET "
            "request_payload=replace(request_payload, 'alloc-1', 'alloc-2')"
        )
        with pytest.raises(asyncpg.RaiseError, match="digest"):
            await apply(c)
        assert await current_version(c) == 6
        # The compatibility trigger was installed before the failed backfill.
        with pytest.raises(asyncpg.RaiseError, match="digest"):
            await legacy_insert(c, old.operation_id, "after")
