"""Original dispatch may retain returned evidence without reviving an expired fence."""

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
@pytest.mark.parametrize("outcome", [CallOutcome.UNKNOWN, CallOutcome.SUCCEEDED])
async def test_expired_original_dispatch_preserves_only_returned_handle(
    pool, runtime, outcome
):
    async with pool.acquire() as connection:
        _, lease = await prepared(connection)

    async def hook(call):
        async with pool.acquire() as connection:
            await expire(connection, lease.operation_id, runtime=runtime)
        return outcome, None, "returned-request"

    executor = OperationExecutor(lease, connect=pool.acquire, provider_call=hook)
    with pytest.raises(ProviderCallRefused):
        await dispatch(executor)
    async with pool.acquire() as connection:
        row = await read_call(connection, idempotency_key="pending-request")
        assert row.provider_ref == "returned-request"
        assert row.stage is CallStage.INTENDED
        assert row.outcome is None
        assert disposition_for(row) is BudgetDisposition.RETAIN
        assert await executor.status() is None
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM harness_execution_audit "
                "WHERE operation_id=$1 "
                "AND event='provider.reference_evidence' AND allowed",
                lease.operation_id,
            )
            == 1
        )
    with pytest.raises(ProviderCallRefused):
        await dispatch(executor)


@pytest.mark.parametrize("change", ["new-fence", "call-fence", "reference", "settled"])
async def test_returned_handle_cannot_replace_other_claim_or_evidence(pool, change):
    async with pool.acquire() as connection:
        _, lease = await prepared(connection)

    async def hook(call):
        async with pool.acquire() as connection:
            if change == "new-fence":
                await connection.execute(
                    "UPDATE harness_operation_leases SET fence_token=fence_token+1, "
                    "holder='successor',attempt_id='successor-attempt' "
                    "WHERE operation_id=$1",
                    lease.operation_id,
                )
            elif change == "call-fence":
                await connection.execute(
                    "UPDATE harness_provider_call_intent SET fence_token=fence_token+1 "
                    "WHERE idempotency_key=$1",
                    call.idempotency_key,
                )
            elif change == "reference":
                await connection.execute(
                    "UPDATE harness_provider_call_intent "
                    "SET provider_ref='first-reference' "
                    "WHERE idempotency_key=$1",
                    call.idempotency_key,
                )
            else:
                from harness_jobs.execution import observe

                await observe(
                    connection,
                    lease,
                    idempotency_key=call.idempotency_key,
                    outcome=CallOutcome.UNKNOWN,
                    provider_ref="operator-retained-reference",
                )
        return CallOutcome.UNKNOWN, None, "conflicting-returned-request"

    executor = OperationExecutor(lease, connect=pool.acquire, provider_call=hook)
    with pytest.raises(ProviderCallRefused):
        await dispatch(executor)
    async with pool.acquire() as connection:
        call = await read_call(connection, idempotency_key="pending-request")
        assert call.provider_ref == {
            "reference": "first-reference",
            "settled": "operator-retained-reference",
        }.get(change)
        assert call.stage is (
            CallStage.UNRESOLVED if change == "settled" else CallStage.INTENDED
        )
        assert disposition_for(call) is BudgetDisposition.RETAIN
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM harness_execution_audit "
                "WHERE operation_id=$1 "
                "AND event='provider.reference_evidence' AND NOT allowed",
                lease.operation_id,
            )
            == 1
        )


async def test_reference_evidence_requires_the_original_dispatch_connection_lock(pool):
    from harness_jobs.execution import record_intent

    async with pool.acquire() as connection:
        _, lease = await prepared(connection)
        call = await record_intent(
            connection,
            lease,
            idempotency_key="pending-request",
            provider="test",
            operation_kind="create",
            target="target",
        )
    executor = OperationExecutor(lease, connect=pool.acquire)
    with pytest.raises(ProviderCallRefused):
        await executor._remember_provider_reference(call, "unowned-reference")
    async with pool.acquire() as connection:
        assert (
            await read_call(connection, idempotency_key=call.idempotency_key)
        ).provider_ref is None


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
