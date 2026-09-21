"""Adversarial real-PostgreSQL probes for dispatch, RPC and queued cancellation."""

import asyncio

import pytest

from harness_jobs.execution import CallOutcome, OperationExecutor, ProviderCallRefused
from harness_jobs.execution_rpc import (
    ExecutionGrant,
    ExecutionRPCServer,
    admitted_steps,
    step_key,
)
from harness_jobs.leases import LeaseRefused, acquire, read_lease, release
from harness_jobs.recovery import sweep_expired_leases

from .conftest import cancellation_principal, requires_postgres
from .test_execution_service import rpc_prepared
from .test_executor_recovery_boundaries import expire, prepared
from .test_facade_postgres import FixedResolver, facade

pytestmark = requires_postgres


async def test_expired_inflight_provider_blocks_successor_and_recovery(pool):
    async with pool.acquire() as connection:
        _, lease = await prepared(connection)
    entered, finish = asyncio.Event(), asyncio.Event()
    effects = []

    async def provider(call):
        entered.set()
        await asyncio.wait_for(finish.wait(), 5)
        effects.append(call.idempotency_key)
        return CallOutcome.SUCCEEDED, None, "resource"

    executor = OperationExecutor(lease, connect=pool.acquire, provider_call=provider)
    task = asyncio.create_task(
        executor.execute_provider(
            idempotency_key="inflight",
            provider="test",
            operation_kind="create",
            target="target",
        )
    )
    try:
        await asyncio.wait_for(entered.wait(), 3)
        async with pool.acquire() as successor:
            await expire(successor, lease.operation_id)
            with pytest.raises(LeaseRefused):
                await acquire(
                    successor,
                    operation_id=lease.operation_id,
                    holder="successor",
                    attempt_id="next",
                )
            observed = []

            async def observer(*args):
                observed.append(args)
                return CallOutcome.ABSENT, None, None

            result = await sweep_expired_leases(successor, observe_call=observer)
            assert result.skipped == 1 and not observed and not effects
            assert (
                await read_lease(successor, operation_id=lease.operation_id)
            ).holder == lease.holder
        finish.set()
        with pytest.raises(ProviderCallRefused):
            await asyncio.wait_for(task, 3)
        assert effects == ["inflight"]
    finally:
        finish.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize(
    "outcome", [None, CallOutcome.SUCCEEDED, CallOutcome.UNKNOWN, CallOutcome.ABSENT]
)
async def test_effect_history_prevents_release_and_legacy_unheld_retry(pool, outcome):
    async with pool.acquire() as connection:
        _, lease = await prepared(connection, outcomes=(outcome,))
        assert not await release(connection, lease)
        executor = OperationExecutor(lease, connect=pool.acquire)
        assert not await executor.release()
        # Repair defense for a row written by the old unsafe release implementation.
        await connection.execute(
            "UPDATE harness_operation_leases SET holder=NULL, expires_at=NULL, "
            "acquired_at=NULL, runtime_deadline=NULL, attempt_id=NULL "
            "WHERE operation_id=$1",
            lease.operation_id,
        )
        with pytest.raises(LeaseRefused):
            await acquire(
                connection,
                operation_id=lease.operation_id,
                holder="next",
                attempt_id="next",
            )


@pytest.mark.parametrize(
    "method,args",
    [
        (
            "record_intent",
            {
                "idempotency_key": "fake",
                "provider": "aws",
                "operation_kind": "create",
                "target": "foreign",
            },
        ),
        ("observe", {"idempotency_key": "fake", "outcome": "succeeded"}),
        ("settle", {"state": "succeeded"}),
        (
            "execute_provider",
            {
                "idempotency_key": "fake",
                "provider": "aws",
                "operation_kind": "create",
                "target": "foreign",
            },
        ),
        ("execute_step", {"step_id": "foreign"}),
        *[
            ("execute_step", {"step_id": "create", key: "foreign"})
            for key in ("provider", "operation_kind", "target", "idempotency_key")
        ],
    ],
)
async def test_worker_cannot_change_plan_or_fabricate_success(pool, method, args):
    async with pool.acquire() as connection:
        _, lease = await rpc_prepared(connection)
    effects = []

    async def authenticate(_):
        return ExecutionGrant(cancellation_principal(lease.holder), lease)

    async def provider(call):
        effects.append(call)
        return CallOutcome.SUCCEEDED, None, "resource"

    server = ExecutionRPCServer(
        connect=pool.acquire, provider_call=provider, authenticate=authenticate
    )
    with pytest.raises(ProviderCallRefused):
        await server.dispatch(dict(token="scoped", method=method, arguments=args))
    assert not effects
    async with pool.acquire() as connection:
        assert (
            await connection.fetchval("SELECT state FROM harness_operations")
            == "pending"
        )
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM harness_provider_call_intent"
            )
            == 0
        )


async def test_only_complete_ordered_admitted_steps_can_succeed(pool):
    steps = [
        dict(
            step_id=str(i),
            provider="test",
            operation_kind="create",
            target=f"target-{i}",
        )
        for i in range(2)
    ]
    async with pool.acquire() as connection:
        record, lease = await rpc_prepared(connection, steps=steps)
    effects = []

    async def authenticate(_):
        return ExecutionGrant(cancellation_principal(lease.holder), lease)

    async def provider(call):
        effects.append((call.target, call.idempotency_key))
        return CallOutcome.SUCCEEDED, None, "resource"

    server = ExecutionRPCServer(
        connect=pool.acquire, provider_call=provider, authenticate=authenticate
    )

    async def step(value):
        return await server.dispatch(
            dict(token="scoped", method="execute_step", arguments={"step_id": value})
        )

    with pytest.raises(ProviderCallRefused):
        await step("1")
    await step("0")
    async with pool.acquire() as connection:
        assert (
            await connection.fetchval("SELECT state FROM harness_operations")
            == "pending"
        )
    await step("1")
    assert effects == [(s.target, step_key(record, s)) for s in admitted_steps(record)]
    async with pool.acquire() as connection:
        assert (
            await connection.fetchval("SELECT state FROM harness_operations")
            == "succeeded"
        )
    with pytest.raises(ProviderCallRefused):
        await step("1")


@pytest.mark.parametrize("claimed", [False, True])
async def test_unheld_cancellation_is_terminal_and_budget_follows_dispatch_evidence(
    pool, claimed
):
    from harness_jobs import OperationStore

    from .conftest import admit_paid
    from .test_admission_postgres import principal, request

    async with pool.acquire() as connection:
        admitted = await admit_paid(
            OperationStore(), connection, principal(), request("cancel-queued")
        )
        operation_id = admitted.record.operation_id
        if claimed:
            await connection.execute(
                "UPDATE harness_dispatch_outbox SET attempts=1 WHERE operation_id=$1",
                operation_id,
            )
    service = facade(pool.acquire, FixedResolver(cancellation_principal("user:alice")))
    assert await service.cancel_operation(operation_id)
    async with pool.acquire() as connection:
        assert (
            await connection.fetchval("SELECT state FROM harness_operations")
            == "cancelled"
        )
        assert await connection.fetchval(
            "SELECT reservation_state FROM harness_approval_consumption"
        ) == ("retained" if claimed else "released")
        assert await connection.fetchval(
            "SELECT count(*) FROM harness_dispatch_outbox"
        ) == int(claimed)
        with pytest.raises(LeaseRefused):
            await acquire(
                connection,
                operation_id=operation_id,
                holder="worker",
                attempt_id="attempt",
            )
        assert not (await sweep_expired_leases(connection)).results
    assert bool(service.ledger.retained) == claimed
    assert bool(service.ledger.released) != claimed


async def test_cancellation_ledger_failure_preserves_fence_and_retries_same_reservation(
    pool,
):
    from harness_jobs import OperationStore

    from .conftest import admit_paid
    from .test_admission_postgres import principal, request

    async with pool.acquire() as connection:
        admitted = await admit_paid(
            OperationStore(), connection, principal(), request("cancel-ledger")
        )
    service = facade(pool.acquire, FixedResolver(cancellation_principal("user:alice")))
    real_release = service.ledger.release
    seen = []

    async def fail_after_durable_fence(*, reservation, reason):
        async with pool.acquire() as reader:
            assert (
                await reader.fetchval("SELECT state FROM harness_operations")
                == "cancelled"
            )
            assert (
                await reader.fetchval("SELECT count(*) FROM harness_dispatch_outbox")
                == 0
            )
            assert await reader.fetchval(
                "SELECT closed_at IS NOT NULL FROM harness_operation_leases"
            )
        seen.append(reservation)
        raise ConnectionError("ledger unavailable")

    from harness_jobs.facade import OperationUnavailable

    service.ledger.release = fail_after_durable_fence
    with pytest.raises(OperationUnavailable):
        await service.cancel_operation(admitted.record.operation_id)
    service.ledger.release = real_release
    assert await service.cancel_operation(admitted.record.operation_id)
    assert service.ledger.released == [seen[0].reservation_id]
    async with pool.acquire() as connection:
        assert (
            await connection.fetchval(
                "SELECT reservation_state FROM harness_approval_consumption"
            )
            == "released"
        )


@pytest.mark.parametrize("broken", ["absent-plan", "changed-payload"])
async def test_rpc_requires_original_approved_step_payload(pool, broken):
    from harness_jobs.identity import ContractViolation

    async with pool.acquire() as connection:
        if broken == "absent-plan":
            _, lease = await prepared(connection)
        else:
            _, lease = await rpc_prepared(connection)
            await connection.execute(
                "UPDATE harness_operations SET "
                "request_payload=replace(request_payload,'target','foreign')"
            )
    effects = []

    async def authenticate(_):
        return ExecutionGrant(cancellation_principal(lease.holder), lease)

    async def provider(call):
        effects.append(call)
        return CallOutcome.SUCCEEDED, None, None

    server = ExecutionRPCServer(
        connect=pool.acquire, provider_call=provider, authenticate=authenticate
    )
    with pytest.raises((ProviderCallRefused, ContractViolation)):
        await server.dispatch(
            dict(token="scoped", method="execute_step", arguments={"step_id": "create"})
        )
    assert not effects
