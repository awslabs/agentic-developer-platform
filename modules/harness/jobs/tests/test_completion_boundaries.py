"""Completion attestation, pre-dispatch cancellation, and durable lease history."""

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
from harness_jobs.leases import LeaseRefused, acquire
from harness_jobs.recovery import request_cancellation, sweep_expired_leases

from .conftest import cancellation_principal, requires_postgres
from .test_executor_recovery_boundaries import expire, prepared

pytestmark = requires_postgres


@pytest.mark.parametrize(
    "state",
    [
        OperationState.SUCCEEDED,
        OperationState.FAILED,
        OperationState.CANCELLED,
        OperationState.UNKNOWN,
    ],
)
@pytest.mark.parametrize("outcome", [None, *CallOutcome])
@pytest.mark.parametrize("cleanup", [False, True])
async def test_terminal_completion_requires_resolved_effects(
    pool, state, outcome, cleanup
):
    async with pool.acquire() as connection:
        _, lease = await prepared(connection, outcomes=(outcome,))
        await connection.execute(
            "UPDATE harness_operations SET cleanup_required=$2 WHERE operation_id=$1",
            lease.operation_id,
            cleanup,
        )
    executor = OperationExecutor(lease, connect=pool.acquire)
    expected = state is OperationState.UNKNOWN or (
        not cleanup
        and outcome in (CallOutcome.ABSENT, CallOutcome.FAILED, CallOutcome.SUCCEEDED)
        and not (state is OperationState.CANCELLED and outcome is CallOutcome.SUCCEEDED)
    )
    assert await executor.settle(state=state) is expected
    async with pool.acquire() as connection:
        row = await connection.fetchrow(
            "SELECT o.state, l.closed_at IS NOT NULL AS closed "
            "FROM harness_operations o "
            "JOIN harness_operation_leases l USING(operation_id) WHERE operation_id=$1",
            lease.operation_id,
        )
        assert row["state"] == (state.value if expected else "pending")
        assert row["closed"] is expected
        saved = await read_call(connection, idempotency_key="boundary-0")
        assert saved.outcome is outcome


@pytest.mark.parametrize("previous", [(), (CallOutcome.SUCCEEDED,), (None,)])
async def test_cancel_after_intent_before_provider_has_durable_absence(
    pool, monkeypatch, previous
):
    async with pool.acquire() as connection:
        _, lease = await prepared(connection, outcomes=previous)
    effects = []

    async def provider(call):
        effects.append(call.idempotency_key)
        return CallOutcome.SUCCEEDED, None, "must-not-exist"

    executor = OperationExecutor(lease, connect=pool.acquire, provider_call=provider)
    record = OperationExecutor._record

    async def cancel_after_record(runtime, **kwargs):
        call = await record(runtime, **kwargs)
        async with pool.acquire() as connection:
            await request_cancellation(
                connection,
                operation_id=lease.operation_id,
                principal=cancellation_principal("authorized-user"),
            )
        return call

    monkeypatch.setattr(OperationExecutor, "_record", cancel_after_record)
    with pytest.raises(CancellationPending) as caught:
        await executor.execute_provider(
            idempotency_key="never-dispatched",
            provider="test",
            operation_kind="create",
            target="target",
        )
    assert effects == []
    assert caught.value.disposition is BudgetDisposition.RELEASE
    async with pool.acquire() as connection:
        call = await read_call(connection, idempotency_key="never-dispatched")
        assert call.stage is CallStage.OBSERVED and call.outcome is CallOutcome.ABSENT
        row = await connection.fetchrow(
            "SELECT state, cleanup_required FROM harness_operations "
            "WHERE operation_id=$1",
            lease.operation_id,
        )
        assert row["state"] == ("pending" if previous else "cancelled")
        assert row["cleanup_required"] is bool(previous)
        events = await read_audit(
            connection,
            operation_id=lease.operation_id,
            org_id=lease.org_id,
            workspace_id=lease.workspace_id,
        )
        assert any(
            e["event"] == "provider.cancelled_before_dispatch"
            and e["allowed"]
            and e["detail"] == "release"
            for e in events
        )


async def test_crash_between_successful_steps_cannot_attest_workflow_completion(
    connection,
):
    _, lease = await prepared(connection, outcomes=(CallOutcome.SUCCEEDED,))
    # Step one is committed. Crash before the driver records the second required step.
    await expire(connection, lease.operation_id)
    report = await sweep_expired_leases(connection)
    assert report.results[0].action == "unknown"
    assert report.results[0].budget_disposition == "settle"
    assert "workflow completion unconfirmed" in report.results[0].detail
    assert (
        await connection.fetchval(
            "SELECT state FROM harness_operations WHERE operation_id=$1",
            lease.operation_id,
        )
        == "unknown"
    )


async def test_grant_history_survives_immediate_crash_and_takeover(pool):
    async with pool.acquire() as connection:
        _, lease = await prepared(connection)
        await expire(connection, lease.operation_id)
        from harness_jobs.recovery import sweep_expired_leases

        await sweep_expired_leases(connection)
        successor = await acquire(
            connection,
            operation_id=lease.operation_id,
            holder="successor",
            attempt_id="next-attempt",
        )
    async with pool.acquire() as restarted:
        events = await read_audit(
            restarted,
            operation_id=lease.operation_id,
            org_id=lease.org_id,
            workspace_id=lease.workspace_id,
        )
        grants = [e for e in events if e["event"] == "lease.acquire" and e["allowed"]]
        assert [(e["actor"], e["attempt_id"], e["fence_token"]) for e in grants] == [
            (lease.holder, lease.attempt_id, lease.fence_token),
            (successor.holder, successor.attempt_id, successor.fence_token),
        ]
        assert (
            await read_audit(
                restarted,
                operation_id=lease.operation_id,
                org_id="foreign",
                workspace_id=lease.workspace_id,
            )
            == ()
        )


@pytest.mark.parametrize(
    "reason",
    [
        "held",
        "terminal",
        "cancel_requested",
        "not_admitted",
        "attempts_exhausted",
        "closed",
        "tenant_at_capacity",
    ],
)
async def test_acquisition_refusals_are_attributable_and_committed(pool, reason):
    async with pool.acquire() as connection:
        _, lease = await prepared(connection)
        maximum = 5
        if reason == "terminal":
            await connection.execute("UPDATE harness_operations SET state='failed'")
        elif reason == "cancel_requested":
            await request_cancellation(
                connection,
                operation_id=lease.operation_id,
                principal=cancellation_principal("authorized-user"),
            )
        elif reason == "not_admitted":
            await connection.execute(
                "UPDATE harness_approval_consumption SET reservation_state='released'"
            )
        elif reason == "attempts_exhausted":
            await expire(connection, lease.operation_id)
            maximum = 1
            await connection.execute(
                "UPDATE harness_operation_leases SET max_attempts=1"
            )
        elif reason == "closed":
            await connection.execute(
                "UPDATE harness_operation_leases SET closed_at=now()"
            )
        elif reason == "tenant_at_capacity":
            # Another operation in the same tenant occupies the sole slot.
            _, lease = await prepared(connection, key="other-operation")
        with pytest.raises(LeaseRefused) as caught:
            await acquire(
                connection,
                operation_id=lease.operation_id,
                holder="refused-worker",
                attempt_id="refused-attempt",
                max_attempts=maximum,
                max_concurrent=1,
            )
        assert caught.value.reason.value == reason
    async with pool.acquire() as restarted:
        events = await read_audit(
            restarted,
            operation_id=lease.operation_id,
            org_id=lease.org_id,
            workspace_id=lease.workspace_id,
        )
        refused = [
            e for e in events if e["event"] == "lease.acquire" and not e["allowed"]
        ]
        assert len(refused) == 1
        assert refused[0]["actor"] == "refused-worker"
        assert refused[0]["attempt_id"] == "refused-attempt"
        assert refused[0]["fence_token"] is None
        assert refused[0]["detail"] == reason


async def test_failed_grant_audit_rolls_back_authority(connection, monkeypatch):
    from harness_jobs import execution

    _, lease = await prepared(connection)
    await expire(connection, lease.operation_id)

    async def broken_audit(*args, **kwargs):
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr(execution, "audit", broken_audit)
    with pytest.raises(RuntimeError, match="audit unavailable"):
        await acquire(
            connection,
            operation_id=lease.operation_id,
            holder="successor",
            attempt_id="next-attempt",
        )
    row = await connection.fetchrow(
        "SELECT holder, fence_token FROM harness_operation_leases "
        "WHERE operation_id=$1",
        lease.operation_id,
    )
    assert row["holder"] == lease.holder and row["fence_token"] == lease.fence_token


async def test_uncertain_expired_provider_cannot_change_successor_cleanup(pool):
    from harness_jobs.execution import ProviderCallRefused

    async with pool.acquire() as connection:
        _, lease = await prepared(connection)

    async def provider(call):
        async with pool.acquire() as connection:
            await expire(connection, lease.operation_id)
            await acquire(
                connection,
                operation_id=lease.operation_id,
                holder="successor",
                attempt_id="successor-attempt",
            )
            await request_cancellation(
                connection,
                operation_id=lease.operation_id,
                principal=cancellation_principal("authorized-user"),
            )
        raise TimeoutError("reply lost")

    executor = OperationExecutor(lease, connect=pool.acquire, provider_call=provider)
    with pytest.raises(ProviderCallRefused):
        await executor.execute_provider(
            idempotency_key="stale-uncertain",
            provider="test",
            operation_kind="create",
            target="target",
        )
    async with pool.acquire() as connection:
        assert not await connection.fetchval(
            "SELECT cleanup_required FROM harness_operations WHERE operation_id=$1",
            lease.operation_id,
        )
        events = await read_audit(
            connection,
            operation_id=lease.operation_id,
            org_id=lease.org_id,
            workspace_id=lease.workspace_id,
        )
        assert any(
            e["event"] == "provider.uncertain" and not e["allowed"] for e in events
        )


async def test_recovery_preserves_preexisting_cleanup_requirement(connection):
    _, lease = await prepared(connection, outcomes=(CallOutcome.ABSENT,))
    await connection.execute(
        "UPDATE harness_operations SET cleanup_required=true WHERE operation_id=$1",
        lease.operation_id,
    )
    await expire(connection, lease.operation_id)
    report = await sweep_expired_leases(connection)
    assert report.results[0].action == "unknown"


async def test_unknown_acquisition_creates_no_cross_tenant_audit(connection):
    with pytest.raises(LeaseRefused):
        await acquire(
            connection,
            operation_id="nonexistent",
            holder="worker",
            attempt_id="attempt",
        )
    assert (
        await connection.fetchval("SELECT count(*) FROM harness_execution_audit") == 0
    )
