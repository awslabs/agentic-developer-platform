"""Positive pending reads retain original effects beyond the error retry ceiling."""

from dataclasses import replace

import pytest

from harness_jobs import OperationStore
from harness_jobs.execution import CallOutcome, CallStage, read_call, record_intent
from harness_jobs.recovery import PendingObservation, sweep_scoped_expired_leases

from .conftest import requires_postgres
from .test_recovery_postgres import _derive_step_key, _plan_operation
from .test_scoped_recovery_postgres import principal

pytestmark = requires_postgres


async def pending_call(connection):
    record, lease = await _plan_operation(OperationStore(), connection)
    key = await _derive_step_key(record, "step-1")
    await record_intent(
        connection,
        lease,
        idempotency_key=key,
        provider="prov",
        operation_kind="create",
        target="t1",
    )
    # Seed the same durable accepted-handle crash window covered separately by
    # test_unknown_provider_handles; the recovery observer cannot write this ID.
    await connection.execute(
        "UPDATE harness_provider_call_intent SET provider_ref='request=car-original' "
        "WHERE idempotency_key=$1",
        key,
    )
    return record, await read_call(connection, idempotency_key=key)


async def expire(connection, operation_id, *, due=True):
    await connection.execute(
        "UPDATE harness_operation_leases "
        "SET expires_at=clock_timestamp()-interval '1 second' "
        "WHERE operation_id=$1",
        operation_id,
    )
    if due:
        await connection.execute(
            "UPDATE harness_provider_call_intent SET reconcile_after=NULL "
            "WHERE operation_id=$1",
            operation_id,
        )


def pending(call):
    return PendingObservation(
        call.idempotency_key,
        call.provider,
        call.operation_kind,
        call.target,
        call.provider_ref,
    )


@pytest.mark.parametrize("cancelled", [False, True])
async def test_pending_retains_intent_backoff_and_original_accounting(
    connection, cancelled
):
    record, original = await pending_call(connection)
    accounting = await connection.fetchrow(
        "SELECT * FROM harness_approval_consumption WHERE operation_id=$1",
        record.operation_id,
    )
    if cancelled:
        await connection.execute(
            "UPDATE harness_operations SET cancel_requested_at=clock_timestamp() "
            "WHERE operation_id=$1",
            record.operation_id,
        )
    observations = []

    async def observe(lease, *_):
        observations.append(lease)
        return pending(original)

    for _ in range(5):
        await expire(connection, record.operation_id)
        report = await sweep_scoped_expired_leases(
            connection,
            principal=principal(),
            observe_claim=observe,
            max_reconcile_attempts=1,
        )
        assert report.deferred == 1
        assert report.results[0].budget_disposition == "retain"
        assert (
            await read_call(connection, idempotency_key=original.idempotency_key)
            == original
        )
        row = await connection.fetchrow(
            "SELECT reconcile_attempts, reconcile_after>clock_timestamp() AS delayed "
            "FROM harness_provider_call_intent WHERE idempotency_key=$1",
            original.idempotency_key,
        )
        assert row["reconcile_attempts"] == 0 and row["delayed"]
        # Expiry alone cannot bypass the already reserved observation backoff.
        await expire(connection, record.operation_id, due=False)
        before = len(observations)
        report = await sweep_scoped_expired_leases(
            connection,
            principal=principal(),
            observe_claim=observe,
        )
        assert report.deferred == 1 and len(observations) == before
    assert len(observations) == 5
    assert len({claim.fence_token for claim in observations}) == 5
    assert (
        await connection.fetchval(
            "SELECT cleanup_required FROM harness_operations WHERE operation_id=$1",
            record.operation_id,
        )
        is cancelled
    )
    assert (
        await connection.fetchrow(
            "SELECT * FROM harness_approval_consumption WHERE operation_id=$1",
            record.operation_id,
        )
        == accounting
    )


@pytest.mark.parametrize("outcome", [CallOutcome.SUCCEEDED, CallOutcome.FAILED])
async def test_pending_later_settles_only_the_original_call(connection, outcome):
    record, original = await pending_call(connection)

    async def observe(lease, *_):
        return pending(original)

    await expire(connection, record.operation_id)
    assert (
        await sweep_scoped_expired_leases(
            connection,
            principal=principal(),
            observe_claim=observe,
        )
    ).deferred == 1

    async def finished(lease, *_):
        return outcome, "provider finished original request", original.provider_ref

    await expire(connection, record.operation_id)
    report = await sweep_scoped_expired_leases(
        connection,
        principal=principal(),
        observe_claim=finished,
    )
    assert report.deferred == 0
    current = await read_call(connection, idempotency_key=original.idempotency_key)
    assert current.outcome is outcome
    assert current.fence_token == original.fence_token
    assert current.provider_ref == original.provider_ref
    assert (
        await connection.fetchval(
            "SELECT count(*) FROM harness_provider_call_intent WHERE operation_id=$1",
            record.operation_id,
        )
        == 1
    )


@pytest.mark.parametrize(
    "failure", ["reference", "key", "unscoped", "revoked", "error", "unknown-reference"]
)
async def test_pending_cannot_mask_errors_or_changed_authority(connection, failure):
    record, original = await pending_call(connection)
    await expire(connection, record.operation_id)

    async def observe(*_):
        if failure == "unknown-reference":
            return CallOutcome.UNKNOWN, "inconclusive", "request=car-other"
        if failure == "error":
            raise TimeoutError("no positive provider answer")
        if failure == "revoked":
            await connection.execute(
                "DELETE FROM harness_recovery_claim_bindings WHERE operation_id=$1",
                record.operation_id,
            )
        result = pending(original)
        if failure == "reference":
            result = replace(result, provider_ref="request=car-other")
        if failure == "key":
            result = replace(result, idempotency_key="another-call")
        return result

    await sweep_scoped_expired_leases(
        connection,
        principal=principal(),
        max_reconcile_attempts=1,
        **(
            {"observe_call": observe}
            if failure == "unscoped"
            else {"observe_claim": observe}
        ),
    )
    current = await read_call(connection, idempotency_key=original.idempotency_key)
    assert current.stage is (
        CallStage.INTENDED if failure == "revoked" else CallStage.UNRESOLVED
    )
    assert current.provider_ref == original.provider_ref
    assert (
        await connection.fetchval(
            "SELECT reconcile_attempts FROM harness_provider_call_intent "
            "WHERE idempotency_key=$1",
            original.idempotency_key,
        )
        == 1
    )
