"""The real facade and RPC resume a large approved plan without repeating work."""

from dataclasses import replace

import pytest

from harness_jobs import OperationRefused, OperationStore
from harness_jobs.execution import CallOutcome, ProviderCallRefused
from harness_jobs.execution_plan import (
    PlanProgress,
    admitted_steps,
    confirmed_plan_progress,
    encode_execution_steps,
    step_key,
)
from harness_jobs.execution_rpc import ExecutionGrant, ExecutionRPCServer
from harness_jobs.leases import acquire
from harness_jobs.recovery import sweep_expired_leases

from .conftest import cancellation_principal, requires_postgres
from .test_execution_descriptors import lifecycle_steps
from .test_executor_recovery_boundaries import expire
from .test_facade_postgres import FixedResolver, facade, open_default, principal

pytestmark = requires_postgres


async def test_facade_large_plan_preserves_prefix_order_and_identity_across_restart(
    pool,
):
    steps = lifecycle_steps()
    parameters = {
        "execution_steps": encode_execution_steps(steps),
        "idempotency_key": "lifecycle",
    }
    service = facade(pool.acquire, FixedResolver(principal()))
    progress = await open_default(service, parameters=parameters)
    assert (
        await open_default(service, parameters=parameters)
    ).operation_id == progress.operation_id
    with pytest.raises(OperationRefused):
        await open_default(
            service,
            parameters={
                **parameters,
                "execution_steps": encode_execution_steps(
                    (*steps[:-1], replace(steps[-1], target="other"))
                ),
            },
        )
    async with pool.acquire() as connection:
        record = await OperationStore().get(
            connection, principal(), progress.operation_id
        )
        lease = await acquire(
            connection,
            operation_id=progress.operation_id,
            holder="worker",
            attempt_id="attempt-1",
        )
    effects = []

    async def authenticate(token):
        if token != "scoped":
            raise OperationRefused("invalid token")
        return ExecutionGrant(cancellation_principal(lease.holder), lease)

    async def provider(call):
        async with pool.acquire() as independent:
            assert (
                await independent.fetchval(
                    "SELECT stage FROM harness_provider_call_intent "
                    "WHERE idempotency_key=$1",
                    call.idempotency_key,
                )
                == "intended"
            )
        effects.append((call.target, call.idempotency_key))
        return CallOutcome.SUCCEEDED, None, call.target

    def server():
        return ExecutionRPCServer(
            connect=pool.acquire, provider_call=provider, authenticate=authenticate
        )

    runtime = server()

    async def step(index):
        return await runtime.dispatch(
            {
                "token": "scoped",
                "method": "execute_step",
                "arguments": {"step_id": steps[index].step_id},
            }
        )

    with pytest.raises(ProviderCallRefused, match="Preceding"):
        await step(20)
    for index in range(20):
        await step(index)
    async with pool.acquire() as connection:
        assert (
            await confirmed_plan_progress(connection, progress.operation_id)
            is PlanProgress.PREFIX
        )
        await expire(connection, progress.operation_id)
        report = await sweep_expired_leases(connection)
        assert report.results[0].action == "retried"
        lease = await acquire(
            connection,
            operation_id=progress.operation_id,
            holder="successor",
            attempt_id="attempt-2",
        )
    # A new server and lease read the original plan; replay of an acknowledged
    # prefix reuses its durable records rather than calling providers again.
    runtime = server()
    for index in range(len(steps)):
        await step(index)
    assert effects == [(s.target, step_key(record, s)) for s in admitted_steps(record)]
    async with pool.acquire() as connection:
        assert (
            await confirmed_plan_progress(connection, progress.operation_id)
            is PlanProgress.COMPLETE
        )
        assert (
            await connection.fetchval(
                "SELECT state FROM harness_operations WHERE operation_id=$1",
                progress.operation_id,
            )
            == "succeeded"
        )
        assert await connection.fetchval("SELECT count(*) FROM harness_operations") == 1
