"""Uncertain provider replies retain their recovery handle under the live fence."""

import pytest

from harness_jobs.execution import (
    BudgetDisposition,
    CallOutcome,
    CallStage,
    CancellationPending,
    OperationExecutor,
    ProviderCallRefused,
    disposition_for,
    read_call,
    unresolved_calls,
)

from .conftest import requires_postgres
from .test_executor_recovery_boundaries import expire, prepared

pytestmark = requires_postgres


async def dispatch(executor):
    return await executor.execute_provider(
        idempotency_key="pending-request",
        provider="test",
        operation_kind="create",
        target="target",
    )


@pytest.mark.parametrize("cancelled", [False, True])
async def test_pending_handle_is_durable_recoverable_and_retains_budget(
    pool, cancelled
):
    async with pool.acquire() as connection:
        _, lease = await prepared(connection)

    async def hook(call):
        if cancelled:
            await executor.cancel()
        return CallOutcome.UNKNOWN, "not a terminal observation", "request-123"

    executor = OperationExecutor(lease, connect=pool.acquire, provider_call=hook)
    if cancelled:
        with pytest.raises(CancellationPending) as caught:
            await dispatch(executor)
        call, disposition = caught.value.call, caught.value.disposition
    else:
        call, disposition = await dispatch(executor)
    assert call.provider_ref == "request-123"
    assert call.stage is CallStage.INTENDED
    assert disposition is BudgetDisposition.RETAIN
    async with pool.acquire() as connection:
        persisted = await read_call(connection, idempotency_key=call.idempotency_key)
        assert persisted == call
        assert disposition_for(persisted) is BudgetDisposition.RETAIN
        assert persisted in await unresolved_calls(connection)
        if cancelled:
            assert await connection.fetchval(
                "SELECT cleanup_required FROM harness_operations WHERE operation_id=$1",
                lease.operation_id,
            )


@pytest.mark.parametrize("runtime", [False, True])
async def test_expired_writer_cannot_persist_a_pending_handle(pool, runtime):
    async with pool.acquire() as connection:
        _, lease = await prepared(connection)

    async def hook(call):
        async with pool.acquire() as connection:
            await expire(connection, lease.operation_id, runtime=runtime)
        return CallOutcome.UNKNOWN, None, "stale-request"

    executor = OperationExecutor(lease, connect=pool.acquire, provider_call=hook)
    with pytest.raises(ProviderCallRefused):
        await dispatch(executor)
    async with pool.acquire() as connection:
        row = await read_call(connection, idempotency_key="pending-request")
        assert row.provider_ref is None
        assert row.stage is CallStage.INTENDED
        assert disposition_for(row) is BudgetDisposition.RETAIN


@pytest.mark.parametrize("invalid_reply", [False, True])
async def test_failed_or_invalid_hook_does_not_record_untrusted_detail(
    pool, invalid_reply
):
    async with pool.acquire() as connection:
        _, lease = await prepared(connection)

    async def hook(call):
        if invalid_reply:
            return "unknown", "sensitive detail", "invalid-reference"
        raise RuntimeError("sensitive exception")

    executor = OperationExecutor(lease, connect=pool.acquire, provider_call=hook)
    call, disposition = await dispatch(executor)
    assert call.provider_ref is None
    assert call.outcome is None
    assert call.stage is CallStage.INTENDED
    assert disposition is BudgetDisposition.RETAIN
