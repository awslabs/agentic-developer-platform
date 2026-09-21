"""Real PostgreSQL evidence for durable dispatch and stale recovery isolation."""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace

import pytest

from harness_jobs import OperationStore
from harness_jobs.execution import (
    BudgetDisposition,
    CallOutcome,
    CallStage,
    OperationExecutor,
    ProviderCallRefused,
    observe,
    read_call,
    record_intent,
)
from harness_jobs.identity import ContractViolation
from harness_jobs.leases import (
    LeaseRefused,
    acquire,
    close,
    fence_expired_lease,
    read_lease,
)
from harness_jobs.recovery import sweep_expired_leases, sweep_unresolved_calls

from .conftest import requires_postgres
from .test_recovery_postgres import leased_operation

pytestmark = requires_postgres


async def prepared(connection, key="boundary", outcomes=()):
    record, lease = await leased_operation(OperationStore(), connection, key=key)
    for i, outcome in enumerate(outcomes):
        call = await record_intent(
            connection,
            lease,
            idempotency_key=f"{key}-{i}",
            provider="test",
            operation_kind="create",
            target="target",
        )
        if outcome is not None:
            await observe(
                connection, lease, idempotency_key=call.idempotency_key, outcome=outcome
            )
    return record, lease


async def expire(connection, operation_id, runtime=False):
    field = "runtime_deadline" if runtime else "expires_at"
    await connection.execute(
        f"UPDATE harness_operation_leases SET {field}="
        "clock_timestamp()-interval '1 second' "
        "WHERE operation_id=$1",
        operation_id,
    )


@pytest.mark.parametrize("runtime", [False, True])
async def test_recovery_claim_blocks_executor_and_fences_slow_observer(pool, runtime):
    async with pool.acquire() as setup:
        record, lease = await prepared(setup, outcomes=(None,))
        await expire(setup, record.operation_id, runtime=runtime)
    entered, finish = asyncio.Event(), asyncio.Event()

    async def observer(*_):
        entered.set()
        await asyncio.wait_for(finish.wait(), 5)
        return CallOutcome.SUCCEEDED, None, "provider-1"

    async with pool.acquire() as recovery, pool.acquire() as successor:
        task = asyncio.create_task(
            sweep_expired_leases(recovery, observe_call=observer)
        )
        try:
            await asyncio.wait_for(entered.wait(), 3)
            with pytest.raises(LeaseRefused):
                await acquire(
                    successor,
                    operation_id=record.operation_id,
                    holder="next-worker",
                    attempt_id="next-attempt",
                )
            claim = await read_lease(successor, operation_id=record.operation_id)
            assert claim.holder.startswith("recovery:")
            assert claim.attempts == lease.attempts
            await expire(successor, record.operation_id)
            next_claim = await fence_expired_lease(
                successor, operation_id=record.operation_id
            )
            finish.set()
            report = await asyncio.wait_for(task, 3)
            assert report.skipped == 1
            from harness_jobs.execution import read_audit

            events = await read_audit(
                successor,
                operation_id=record.operation_id,
                org_id=lease.org_id,
                workspace_id=lease.workspace_id,
            )
            assert any(
                e["event"] == "recovery.lost_claim"
                and not e["allowed"]
                and e["fence_token"] == claim.fence_token
                for e in events
            )
            current = await read_lease(successor, operation_id=record.operation_id)
            assert current.holder == next_claim.lease.holder
            assert current.fence_token == next_claim.fence_token
            assert (
                await read_call(successor, idempotency_key="boundary-0")
            ).stage is CallStage.INTENDED
            assert (
                await successor.fetchval(
                    "SELECT state FROM harness_operations WHERE operation_id=$1",
                    record.operation_id,
                )
                == "pending"
            )
        finally:
            finish.set()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize(
    "outcomes,expected,action",
    [
        ((CallOutcome.SUCCEEDED,), "settle", "unknown"),
        ((CallOutcome.ABSENT,), "release", "failed"),
        ((CallOutcome.UNKNOWN,), "retain", "unknown"),
        ((CallOutcome.SUCCEEDED, CallOutcome.ABSENT), "mixed", "unknown"),
    ],
)
async def test_recovery_includes_observed_calls_and_exact_budget_dispositions(
    connection, outcomes, expected, action
):
    record, _ = await prepared(connection, outcomes=outcomes)
    await expire(connection, record.operation_id, runtime=True)
    report = await sweep_expired_leases(connection)
    result = report.results[0]
    assert result.action == action
    assert result.budget_disposition == expected
    assert len(result.call_dispositions) == len(outcomes)
    if CallOutcome.SUCCEEDED in outcomes:
        assert BudgetDisposition.SETTLE in dict(result.call_dispositions).values()
        assert result.detail != "budget released"


async def test_runtime_expiry_retries_and_stale_executor_stops(pool):
    async with pool.acquire() as connection:
        record, lease = await prepared(connection)
        await expire(connection, record.operation_id, runtime=True)
    executor = OperationExecutor(lease, connect=pool.acquire)
    assert await executor.status() is None
    with pytest.raises(ProviderCallRefused):
        await executor.cancel_requested()
    async with pool.acquire() as connection:
        report = await sweep_expired_leases(connection)
        assert report.retried == 1
        from harness_jobs.execution import read_audit

        events = await read_audit(
            connection,
            operation_id=record.operation_id,
            org_id=lease.org_id,
            workspace_id=lease.workspace_id,
        )
        assert any(e["event"] == "recovery.retry" and e["allowed"] for e in events)
        next_lease = await acquire(
            connection,
            operation_id=record.operation_id,
            holder="next-worker",
            attempt_id="next-attempt",
        )
        assert next_lease.fence_token > lease.fence_token


async def test_close_requires_current_holder_and_unheld_close_cannot_kill_worker(
    connection,
):
    record, lease = await prepared(connection)
    with pytest.raises(ContractViolation):
        await close(
            connection,
            operation_id=record.operation_id,
            reason="test",
            fence_token=lease.fence_token,
        )
    assert not await close(
        connection,
        operation_id=record.operation_id,
        reason="test",
        fence_token=lease.fence_token,
        holder="wrong-holder",
    )
    assert not await close(
        connection, operation_id=record.operation_id, reason="test", fence_token=None
    )
    assert await read_lease(connection, operation_id=record.operation_id) == lease


async def test_provider_hook_sees_committed_intent_from_another_session(pool):
    async with pool.acquire() as connection:
        _, lease = await prepared(connection)
    called = []

    async def provider(call):
        async with pool.acquire() as other:
            saved = await read_call(other, idempotency_key=call.idempotency_key)
            assert saved is not None and saved.stage is CallStage.INTENDED
        called.append(call.idempotency_key)
        return CallOutcome.SUCCEEDED, None, "resource-1"

    executor = OperationExecutor(lease, connect=pool.acquire, provider_call=provider)
    result, disposition = await executor.execute_provider(
        idempotency_key="durable-key",
        provider="test",
        operation_kind="create",
        target="target",
    )
    assert (
        result.stage is CallStage.OBSERVED and disposition is BudgetDisposition.SETTLE
    )
    with pytest.raises(ProviderCallRefused):
        await executor.execute_provider(
            idempotency_key="durable-key",
            provider="test",
            operation_kind="create",
            target="target",
        )
    assert called == ["durable-key"]


async def test_executor_refuses_ambient_transaction_before_provider_or_intent(pool):
    async with pool.acquire() as connection:
        _, lease = await prepared(connection)
    called = []

    @asynccontextmanager
    async def bad_factory():
        async with pool.acquire() as connection, connection.transaction():
            yield connection

    async def provider(call):
        called.append(call)
        return CallOutcome.SUCCEEDED, None, None

    executor = OperationExecutor(lease, connect=bad_factory, provider_call=provider)
    with pytest.raises(ContractViolation, match="open transaction"):
        await executor.execute_provider(
            idempotency_key="never-called",
            provider="test",
            operation_kind="create",
            target="target",
        )
    assert not called
    async with pool.acquire() as connection:
        assert await read_call(connection, idempotency_key="never-called") is None


async def test_forged_tenant_cannot_read_cancel_or_commit_intent(pool):
    async with pool.acquire() as connection:
        _, lease = await prepared(connection)
    executor = OperationExecutor(replace(lease, org_id="other"), connect=pool.acquire)
    assert await executor.status() is None
    with pytest.raises(ProviderCallRefused):
        await executor.cancel_requested()
    with pytest.raises(ProviderCallRefused):
        await executor.record_intent(
            idempotency_key="foreign",
            provider="test",
            operation_kind="create",
            target="target",
        )


async def test_orphan_sweep_cannot_write_after_a_successor_acquires(connection, pool):
    record, lease = await prepared(connection, outcomes=(None,))
    await expire(connection, record.operation_id)

    async def observer(*_):
        async with pool.acquire() as successor:
            await acquire(
                successor,
                operation_id=record.operation_id,
                holder="new-worker",
                attempt_id="new-attempt",
            )
        return CallOutcome.SUCCEEDED, None, "resource"

    assert await sweep_unresolved_calls(connection, observe_call=observer) == 0
    assert (
        await read_call(connection, idempotency_key="boundary-0")
    ).stage is CallStage.INTENDED


async def test_executor_cancel_uses_own_holder_and_refuses_new_provider_intent(pool):
    async with pool.acquire() as connection:
        _, lease = await prepared(connection)
    executor = OperationExecutor(lease, connect=pool.acquire)
    assert await executor.cancel(reason="stop")
    assert await executor.cancel_requested()
    assert not await executor.cancel(reason="later request")
    with pytest.raises(ProviderCallRefused):
        await executor.record_intent(
            idempotency_key="cancelled",
            provider="test",
            operation_kind="create",
            target="target",
        )
    async with pool.acquire() as connection:
        assert (
            await connection.fetchval(
                "SELECT cancel_requested_by FROM harness_operations "
                "WHERE operation_id=$1",
                lease.operation_id,
            )
            == lease.holder
        )


async def test_low_level_intent_refuses_forged_tenant_and_attempt(connection):
    _, lease = await prepared(connection)
    for forged in [replace(lease, org_id="other"), replace(lease, attempt_id="other")]:
        with pytest.raises(ProviderCallRefused):
            await record_intent(
                connection,
                forged,
                idempotency_key="forged",
                provider="test",
                operation_kind="create",
                target="target",
            )


async def test_executor_records_refused_cancel_renew_and_release(pool):
    from harness_jobs.execution import read_audit

    async with pool.acquire() as connection:
        _, lease = await prepared(connection)
        await expire(connection, lease.operation_id)
    executor = OperationExecutor(lease, connect=pool.acquire)
    with pytest.raises(ProviderCallRefused):
        await executor.cancel()
    with pytest.raises(ProviderCallRefused):
        await executor.renew()
    assert not await executor.release()
    async with pool.acquire() as connection:
        rows = await read_audit(
            connection,
            operation_id=lease.operation_id,
            org_id=lease.org_id,
            workspace_id=lease.workspace_id,
        )
    assert {r["event"] for r in rows if not r["allowed"]} >= {
        "cancel",
        "renew",
        "release",
    }
