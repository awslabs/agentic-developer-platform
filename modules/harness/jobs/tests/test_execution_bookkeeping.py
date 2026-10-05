"""Trusted bookkeeping must run with committed provider evidence and a live lease."""

import asyncio

import pytest

from harness_jobs.execution import CallOutcome, CancellationPending, ProviderCallRefused
from harness_jobs.execution_plan import PlanProgress, confirmed_plan_progress
from harness_jobs.execution_rpc import ExecutionGrant, ExecutionRPCServer
from harness_jobs.leases import lock_lease

from .conftest import cancellation_principal, requires_postgres
from .test_execution_service import rpc_prepared

pytestmark = requires_postgres


@pytest.mark.parametrize("failure", [False, True])
@pytest.mark.parametrize("detail", [None, "provider confirmed: exact resource"])
async def test_final_bookkeeping_precedes_closure_and_cannot_change_result(
    pool, failure, detail
):
    async with pool.acquire() as connection:
        record, lease = await rpc_prepared(connection)
    grant = ExecutionGrant(cancellation_principal(lease.holder), lease)
    events = []

    async def authenticate(token):
        return grant

    async def provider(call):
        events.append("provider")
        return CallOutcome.SUCCEEDED, detail, "provider-resource"

    async def after_step(verified, result):
        assert verified is grant
        async with pool.acquire() as connection, connection.transaction():
            assert await lock_lease(connection, lease)
            assert (
                await confirmed_plan_progress(connection, record.operation_id)
                == PlanProgress.COMPLETE
            )
        assert result[0].provider_ref == "provider-resource"
        events.append("bookkeeping")
        if failure:
            raise RuntimeError("bookkeeping storage unavailable")

    service = ExecutionRPCServer(
        connect=pool.acquire,
        provider_call=provider,
        authenticate=authenticate,
        after_step=after_step,
    )
    request = {
        "token": "scoped",
        "method": "execute_step",
        "arguments": {"step_id": "create"},
    }
    if failure:
        with pytest.raises(RuntimeError):
            await service.dispatch(request)
    else:
        result = await service.dispatch(request)
        assert result[1] == "settle"
    assert events == ["provider", "bookkeeping"]
    async with pool.acquire() as connection:
        assert await connection.fetchval(
            "SELECT closed_at IS NOT NULL FROM harness_operation_leases"
        ) is (not failure)
        assert await connection.fetchval(
            "SELECT outcome FROM harness_provider_call_intent"
        ) == ("succeeded" if detail is None else "succeeded: " + detail)
    if failure:
        # A new request resumes bookkeeping from the durable provider result.
        failure = False
        assert (await service.dispatch(request))[1] == "settle"
        assert events == ["provider", "bookkeeping", "bookkeeping"]
        async with pool.acquire() as connection:
            assert await connection.fetchval(
                "SELECT closed_at IS NOT NULL FROM harness_operation_leases"
            )


async def test_bookkeeping_serializes_repeated_steps_and_recovery(pool):
    async with pool.acquire() as connection:
        _, lease = await rpc_prepared(connection)
    grant = ExecutionGrant(cancellation_principal(lease.holder), lease)
    started, resume = asyncio.Event(), asyncio.Event()
    calls = []

    async def authenticate(token):
        return grant

    async def provider(call):
        calls.append(call.idempotency_key)
        return CallOutcome.SUCCEEDED, "created", "resource"

    async def after_step(verified, result):
        started.set()
        await resume.wait()

    service = ExecutionRPCServer(
        connect=pool.acquire,
        provider_call=provider,
        authenticate=authenticate,
        after_step=after_step,
    )
    request = {
        "token": "scoped",
        "method": "execute_step",
        "arguments": {"step_id": "create"},
    }
    first = asyncio.create_task(service.dispatch(request))
    try:
        await asyncio.wait_for(started.wait(), 5)
        with pytest.raises(ProviderCallRefused, match="already in flight"):
            await service.dispatch(request)
        async with pool.acquire() as connection:
            # This is the same operation lock used by shared recovery.
            assert not await connection.fetchval(
                "SELECT pg_try_advisory_lock(hashtextextended($1, 0))",
                f"harness-provider-dispatch:{lease.operation_id}",
            )
            assert not await connection.fetchval(
                "SELECT closed_at IS NOT NULL FROM harness_operation_leases"
            )
    finally:
        resume.set()
        await asyncio.wait_for(first, 5)
    assert len(calls) == 1


async def test_cancel_during_provider_still_accounts_for_committed_effect(pool):
    async with pool.acquire() as connection:
        _, lease = await rpc_prepared(connection)
    grant = ExecutionGrant(cancellation_principal(lease.holder), lease)
    accounted = []

    async def authenticate(token):
        return grant

    async def provider(call):
        async with pool.acquire() as connection:
            await connection.execute(
                "UPDATE harness_operations SET cancel_requested_at=now() "
                "WHERE operation_id=$1",
                lease.operation_id,
            )
        return CallOutcome.SUCCEEDED, "created before cancellation", "resource"

    async def after_step(verified, result):
        accounted.append(result)
        async with pool.acquire() as connection, connection.transaction():
            assert await lock_lease(connection, lease)

    service = ExecutionRPCServer(
        connect=pool.acquire,
        provider_call=provider,
        authenticate=authenticate,
        after_step=after_step,
    )
    with pytest.raises(CancellationPending):
        await service.dispatch(
            {
                "token": "scoped",
                "method": "execute_step",
                "arguments": {"step_id": "create"},
            }
        )
    assert len(accounted) == 1
    assert accounted[0][0].provider_ref == "resource"
    async with pool.acquire() as connection:
        assert not await connection.fetchval(
            "SELECT closed_at IS NOT NULL FROM harness_operation_leases"
        )
