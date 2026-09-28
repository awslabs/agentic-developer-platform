"""Provider restart, in-flight cancellation and recovery audit evidence."""

import asyncio

import pytest

from harness_jobs.execution import (
    BudgetDisposition,
    CallOutcome,
    CallStage,
    CancellationPending,
    OperationExecutor,
    read_audit,
    read_call,
)
from harness_jobs.identity import OperationState
from harness_jobs.recovery import (
    request_cancellation,
    sweep_expired_leases,
    sweep_unresolved_calls,
)

from .conftest import cancellation_principal, requires_postgres
from .test_executor_recovery_boundaries import expire, prepared

pytestmark = requires_postgres
CALL = dict(
    idempotency_key="lifecycle",
    provider="test",
    operation_kind="create",
    target="target",
)


async def test_timeout_restart_reconciles_without_repeating_mutation(
    pool,
):
    async with pool.acquire() as connection:
        _, lease = await prepared(connection)
    effects = []

    async def provider(call):
        effects.append(call.idempotency_key)
        raise TimeoutError("reply lost after mutation")

    executor = OperationExecutor(lease, connect=pool.acquire, provider_call=provider)
    call, disposition = await executor.execute_provider(**CALL)
    assert call.stage is CallStage.INTENDED and disposition is BudgetDisposition.RETAIN

    async def observer(key, *_):
        assert key in effects
        return CallOutcome.SUCCEEDED, None, "real-resource"

    # New connection/recovery object represents a restarted executor process.
    async with pool.acquire() as restarted:
        await expire(restarted, lease.operation_id)
        result = await sweep_expired_leases(restarted, observe_call=observer)
        saved = await read_call(restarted, idempotency_key=CALL["idempotency_key"])
        assert (
            saved.stage is CallStage.RECONCILED
            and saved.provider_ref == "real-resource"
        )
        assert result.results[0].action == "unknown"
        assert result.results[0].budget_disposition == "settle"
        events = await read_audit(
            restarted,
            operation_id=lease.operation_id,
            org_id=lease.org_id,
            workspace_id=lease.workspace_id,
        )
    assert effects == ["lifecycle"]
    assert {r["event"] for r in events} >= {
        "provider.uncertain",
        "recovery.claim",
        "recovery.observe_started",
        "recovery.reconciled",
        "recovery.settled",
    }
    assert all("reply lost" not in (r["detail"] or "") for r in events)


@pytest.mark.parametrize("orphan", [False, True])
async def test_reconciliation_backoff_is_persisted_and_exhaustion_is_bounded(
    connection, orphan
):
    record, _ = await prepared(connection, outcomes=(None,))
    await expire(connection, record.operation_id)
    attempts = []

    async def observer(*_):
        attempts.append(1)
        raise TimeoutError("not yet observable")

    async def sweep():
        await connection.execute(
            "UPDATE harness_operation_leases SET "
            "expires_at=clock_timestamp()-interval '1 second' "
            "WHERE operation_id=$1 AND holder IS NOT NULL",
            record.operation_id,
        )
        if orphan:
            return await sweep_unresolved_calls(connection, observe_call=observer)
        return await sweep_expired_leases(connection, observe_call=observer)

    await sweep()
    row = await connection.fetchrow(
        "SELECT reconcile_attempts, reconcile_after > clock_timestamp() AS delayed "
        "FROM harness_provider_call_intent WHERE idempotency_key='boundary-0'",
    )
    assert row["reconcile_attempts"] == 1 and row["delayed"]
    await sweep()
    assert len(attempts) == 1, "a restart/sweep must not bypass persisted backoff"
    for _ in range(2):
        await connection.execute(
            "UPDATE harness_provider_call_intent SET "
            "reconcile_after=clock_timestamp()-interval '1 second'",
        )
        await sweep()
    call = await read_call(connection, idempotency_key="boundary-0")
    assert len(attempts) == 3 and call.stage is CallStage.UNRESOLVED
    await sweep()
    assert len(attempts) == 3, (
        "exhaustion requires operator resolution, not infinite retries"
    )


@pytest.mark.parametrize("timeout", [False, True])
async def test_cancellation_during_provider_io_preserves_effect_and_requires_cleanup(
    pool,
    timeout,
):
    async with pool.acquire() as connection:
        _, lease = await prepared(connection)
    entered, finish = asyncio.Event(), asyncio.Event()

    async def provider(call):
        entered.set()
        await asyncio.wait_for(finish.wait(), 3)
        if timeout:
            raise TimeoutError("provider effect exists but reply was lost")
        return CallOutcome.SUCCEEDED, None, "created-resource"

    executor = OperationExecutor(lease, connect=pool.acquire, provider_call=provider)
    task = asyncio.create_task(executor.execute_provider(**CALL))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        async with pool.acquire() as canceller:
            assert await request_cancellation(
                canceller,
                operation_id=lease.operation_id,
                principal=cancellation_principal("authorized-user"),
            )
        finish.set()
        with pytest.raises(CancellationPending) as caught:
            await task
        if timeout:
            assert caught.value.call.stage is CallStage.INTENDED
            assert caught.value.disposition is BudgetDisposition.RETAIN
        else:
            assert caught.value.call.provider_ref == "created-resource"
            assert caught.value.disposition is BudgetDisposition.SETTLE
        assert (await executor.status()).cleanup_required
        assert not await executor.settle(state=OperationState.SUCCEEDED)
        assert not await executor.settle(state=OperationState.CANCELLED)
        async with pool.acquire() as recovery:
            await expire(recovery, lease.operation_id)

            async def observer(*_):
                return CallOutcome.SUCCEEDED, None, "created-resource"

            result = await sweep_expired_leases(recovery, observe_call=observer)
            assert result.results[0].action == "unknown"
            assert "requires cleanup" in result.results[0].detail
            row = await recovery.fetchrow(
                "SELECT cleanup_required, state FROM harness_operations "
                "WHERE operation_id=$1",
                lease.operation_id,
            )
            assert row["cleanup_required"] and row["state"] == "unknown"
    finally:
        finish.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_cancel_after_provider_reply_still_refuses_success_at_settlement(pool):
    async with pool.acquire() as connection:
        _, lease = await prepared(connection)

    async def provider(_):
        return CallOutcome.SUCCEEDED, None, "resource"

    executor = OperationExecutor(lease, connect=pool.acquire, provider_call=provider)
    await executor.execute_provider(**CALL)
    assert await executor.cancel()
    assert not await executor.settle(state=OperationState.SUCCEEDED)
    assert not await executor.settle(state=OperationState.CANCELLED)
    assert (await executor.status()).cleanup_required


async def test_schema_five_upgrade_preserves_intent_and_adds_retry_evidence(connection):
    from harness_jobs.schema import apply, current_version, downgrade

    record, _ = await prepared(connection, outcomes=(None,))
    await downgrade(connection, target=4)
    assert await current_version(connection) == 4
    await apply(connection)
    row = await connection.fetchrow(
        "SELECT reconcile_attempts, reconcile_after FROM harness_provider_call_intent "
        "WHERE idempotency_key='boundary-0'",
    )
    assert row["reconcile_attempts"] == 0 and row["reconcile_after"] is None
    assert not await connection.fetchval(
        "SELECT cleanup_required FROM harness_operations WHERE operation_id=$1",
        record.operation_id,
    )
