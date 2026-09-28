"""Recovery sweep and cancellation: abandoned work is settled, not lost.

Issue #5527 (w6-04), EPIC #4910, Wave 6.

Tests in this file require a real database because the properties under test are
PostgreSQL properties: the UPDATE that writes cancel_requested_at must be conditional
on the current state in the row, the sweep must re-acquire a lease (which requires the
prior one to be genuinely expired), and `check_cancel_requested` must join across two
tables with a liveness predicate.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from harness_jobs import OperationStore
from harness_jobs.execution import (
    CallOutcome,
    derive_idempotency_key,
    record_intent,
)
from harness_jobs.leases import (
    acquire,
    close,
    fence_expired_lease,
    release,
)
from harness_jobs.recovery import (
    CancellationRecord,
    RecoveryReport,
    check_cancel_requested,
    request_cancellation,
    sweep_expired_leases,
    sweep_unresolved_calls,
)

from .conftest import admit_paid, cancellation_principal, requires_postgres
from .test_admission_postgres import principal, request

pytestmark = requires_postgres


@pytest.mark.parametrize("orphan", [False, True])
async def test_confirmed_prefix_resumes_through_scoped_rpc(pool, orphan):
    from dataclasses import replace

    from harness_jobs.execution_rpc import ExecutionGrant, ExecutionRPCServer
    from harness_jobs.identity import OperationState

    steps = [
        dict(step_id="step-1", provider="prov", operation_kind="create", target="t1"),
        dict(step_id="step-2", provider="prov", operation_kind="create", target="t2"),
    ]
    async with pool.acquire() as connection:
        record, old = await _plan_operation(OperationStore(), connection, steps=steps)
        key = await _derive_step_key(record, "step-1")
        await record_intent(
            connection,
            old,
            idempotency_key=key,
            provider="prov",
            operation_kind="create",
            target="t1",
        )
        await connection.execute(
            "UPDATE harness_operation_leases "
            "SET expires_at=clock_timestamp()-interval '1 second' "
            "WHERE operation_id=$1",
            record.operation_id,
        )

        async def observer(*_):
            return CallOutcome.SUCCEEDED, None, "existing-t1"

        if orphan:
            assert await sweep_unresolved_calls(connection, observe_call=observer) == 1
        else:
            report = await sweep_expired_leases(connection, observe_call=observer)
            assert report.retried == 1
        successor = await acquire(
            connection,
            operation_id=record.operation_id,
            holder="successor",
            attempt_id="attempt-2",
        )
        assert successor.fence_token > old.fence_token
        assert successor.attempts == old.attempts + 1

    invoked = []

    async def provider(call):
        invoked.append(call.target)
        return CallOutcome.SUCCEEDED, None, "created-t2"

    async def authenticate(token):
        return ExecutionGrant(replace(principal(), subject="successor"), successor)

    rpc = ExecutionRPCServer(
        connect=pool.acquire, provider_call=provider, authenticate=authenticate
    )
    for step_id in ("step-1", "step-2"):
        await rpc.dispatch(
            dict(
                token="test-grant",
                method="execute_step",
                arguments=dict(step_id=step_id),
            )
        )
    assert invoked == ["t2"]
    async with pool.acquire() as connection:
        final = await OperationStore().get(connection, principal(), record.operation_id)
        assert final.state is OperationState.SUCCEEDED


@pytest.mark.parametrize(
    "outcome, expected",
    [(CallOutcome.ABSENT, "cancelled"), (CallOutcome.SUCCEEDED, "unknown")],
)
async def test_orphan_recovery_honors_cancellation(connection, outcome, expected):
    record, lease = await _plan_operation(OperationStore(), connection)
    await record_intent(
        connection,
        lease,
        idempotency_key=await _derive_step_key(record, "step-1"),
        provider="prov",
        operation_kind="create",
        target="t1",
    )
    await request_cancellation(
        connection,
        operation_id=record.operation_id,
        principal=cancellation_principal("user:alice"),
    )
    await close(
        connection,
        operation_id=record.operation_id,
        reason="lost worker",
        fence_token=lease.fence_token,
        holder=lease.holder,
    )

    async def observer(*_):
        return outcome, None, None

    assert await sweep_unresolved_calls(connection, observe_call=observer) == 1
    row = await connection.fetchrow(
        "SELECT state, cleanup_required FROM harness_operations WHERE operation_id=$1",
        record.operation_id,
    )
    assert row["state"] == expected
    assert row["cleanup_required"] is (outcome is CallOutcome.SUCCEEDED)


@pytest.mark.parametrize("variant", ["duplicate", "wrong_target", "not_prefix"])
async def test_recovery_does_not_confirm_invalid_plan_history(connection, variant):
    steps = [
        dict(step_id="step-1", provider="prov", operation_kind="create", target="t1")
    ]
    if variant == "duplicate":
        steps.append(dict(steps[0]))
    elif variant == "not_prefix":
        steps.append(
            dict(
                step_id="step-2", provider="prov", operation_kind="create", target="t2"
            )
        )
    record, lease = await _plan_operation(OperationStore(), connection, steps=steps)
    step = steps[-1]
    await record_intent(
        connection,
        lease,
        idempotency_key=await _derive_step_key(record, step["step_id"]),
        provider="prov",
        operation_kind="create",
        target="wrong" if variant == "wrong_target" else step["target"],
    )
    await connection.execute(
        "UPDATE harness_operation_leases "
        "SET expires_at=clock_timestamp()-interval '1 second' "
        "WHERE operation_id=$1",
        record.operation_id,
    )

    async def observer(*_):
        return CallOutcome.SUCCEEDED, None, None

    report = await sweep_expired_leases(connection, observe_call=observer)
    assert report.results[0].action == "unknown"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def leased_operation(store, connection, key="key-1", org="org-a", ws="ws-1"):
    """An admitted, paid-for operation with an active lease."""
    admitted = await admit_paid(
        store, connection, principal(org=org, workspace=ws), request(key)
    )
    record = admitted.record
    lease = await acquire(
        connection,
        operation_id=record.operation_id,
        holder="worker-1",
        attempt_id="attempt-1",
        duration=timedelta(seconds=60),
    )
    return record, lease


# ---------------------------------------------------------------------------
# request_cancellation
# ---------------------------------------------------------------------------


async def test_request_cancellation_writes_the_record(connection):
    """Cancel request is written on an active operation."""
    store = OperationStore()
    record, _ = await leased_operation(store, connection)

    wrote = await request_cancellation(
        connection,
        operation_id=record.operation_id,
        principal=cancellation_principal("user:alice"),
        reason="user changed their mind",
    )
    assert wrote, "request_cancellation should return True on first write"

    row = await connection.fetchrow(
        "SELECT cancel_requested_by, cancel_reason FROM harness_operations "
        "WHERE operation_id = $1",
        record.operation_id,
    )
    assert row["cancel_requested_by"] == "user:alice"
    assert row["cancel_reason"] == "user changed their mind"


async def test_request_cancellation_is_idempotent_first_wins(connection):
    """A second cancellation request does not overwrite the first."""
    store = OperationStore()
    record, _ = await leased_operation(store, connection)

    await request_cancellation(
        connection,
        operation_id=record.operation_id,
        principal=cancellation_principal("user:alice"),
        reason="first",
    )
    wrote = await request_cancellation(
        connection,
        operation_id=record.operation_id,
        principal=cancellation_principal("user:bob"),
        reason="second",
    )
    assert not wrote, "second cancellation request should return False"

    row = await connection.fetchrow(
        "SELECT cancel_requested_by FROM harness_operations WHERE operation_id = $1",
        record.operation_id,
    )
    assert row["cancel_requested_by"] == "user:alice", "first writer wins"


async def test_request_cancellation_returns_false_for_unknown_operation(connection):
    """No row to update => False returned, no error."""
    wrote = await request_cancellation(
        connection,
        operation_id="no-such-op",
        principal=cancellation_principal("user:alice"),
    )
    assert not wrote


async def test_request_cancellation_rejects_empty_requested_by(connection):
    """Empty requested_by is rejected as a contract violation."""
    store = OperationStore()
    record, _ = await leased_operation(store, connection)

    with pytest.raises(Exception):
        await request_cancellation(
            connection,
            operation_id=record.operation_id,
            principal=cancellation_principal(""),
        )


# ---------------------------------------------------------------------------
# check_cancel_requested
# ---------------------------------------------------------------------------


async def test_check_cancel_returns_none_when_not_requested(connection):
    """Returns None when no cancellation is pending."""
    store = OperationStore()
    record, lease = await leased_operation(store, connection)

    result = await check_cancel_requested(connection, lease)
    assert result is None


async def test_check_cancel_returns_record_when_requested(connection):
    """Returns the cancellation record once a request is written."""
    store = OperationStore()
    record, lease = await leased_operation(store, connection)

    await request_cancellation(
        connection,
        operation_id=record.operation_id,
        principal=cancellation_principal("user:carol"),
        reason="shutting down",
    )
    result = await check_cancel_requested(connection, lease)
    assert isinstance(result, CancellationRecord)
    assert result.operation_id == record.operation_id
    assert result.requested_by == "user:carol"
    assert result.reason == "shutting down"


async def test_check_cancel_requires_live_lease(connection):
    """Returns None when the lease has been released (caller is no longer entitled)."""
    store = OperationStore()
    record, lease = await leased_operation(store, connection)

    await request_cancellation(
        connection,
        operation_id=record.operation_id,
        principal=cancellation_principal("user:dave"),
    )
    # Cancellation cannot release into an unrecoverable unheld state.
    assert not await release(connection, lease)
    await connection.execute(
        "UPDATE harness_operation_leases SET "
        "expires_at=clock_timestamp()-interval '1 second' "
        "WHERE operation_id=$1",
        lease.operation_id,
    )

    result = await check_cancel_requested(connection, lease)
    # The stale holder gets no answer: it is no longer the executor.
    assert result is None


async def test_check_cancel_rejects_non_lease_argument(connection):
    """Passing something other than ExecutionLease raises ContractViolation."""
    with pytest.raises(Exception):
        await check_cancel_requested(connection, "not-a-lease")


# ---------------------------------------------------------------------------
# sweep_expired_leases: basic cases
# ---------------------------------------------------------------------------


async def test_sweep_with_no_expired_leases_returns_empty_report(connection):
    """When no leases have expired, the sweep does nothing."""
    report = await sweep_expired_leases(connection)
    assert isinstance(report, RecoveryReport)
    assert report.retried == 0
    assert report.retained == 0
    assert report.skipped == 0
    assert report.results == ()


async def test_sweep_skips_active_holder(connection):
    """A lease that has not yet expired is not touched by the sweep."""
    store = OperationStore()
    record, lease = await leased_operation(store, connection)

    # The lease is still live (60-second duration). The sweep should skip it.
    report = await sweep_expired_leases(connection)
    assert report.skipped == 0
    assert report.retried == 0


async def test_sweep_retries_expired_lease_with_no_provider_calls(connection):
    """An expired lease with no provider calls is offered back for execution."""
    store = OperationStore()
    record, _ = await leased_operation(store, connection)

    # Expire the lease directly so the sweep can pick it up.
    await connection.execute(
        """
        UPDATE harness_operation_leases
           SET expires_at = clock_timestamp() - '1 second'::interval
         WHERE operation_id = $1
        """,
        record.operation_id,
    )

    report = await sweep_expired_leases(connection)
    assert report.retried == 1
    assert report.retained == 0
    assert len(report.results) == 1
    assert report.results[0].action == "retried"


async def test_sweep_marks_unknown_when_no_observer_and_provider_call_exists(
    connection,
):
    """Unresolved provider calls without an observer are marked UNKNOWN."""
    store = OperationStore()
    record, lease = await leased_operation(store, connection)

    # Record a provider call intent (but never observe it).
    ik = derive_idempotency_key(
        operation_id=record.operation_id,
        attempt_id="attempt-1",
        step="step-1",
    )
    await record_intent(
        connection,
        lease,
        idempotency_key=ik,
        provider="test-provider",
        operation_kind="create",
        target="resource-a",
    )

    # Expire the lease.
    await connection.execute(
        """
        UPDATE harness_operation_leases
           SET expires_at = clock_timestamp() - '1 second'::interval
         WHERE operation_id = $1
        """,
        record.operation_id,
    )

    report = await sweep_expired_leases(
        connection, observe_call=None, max_reconcile_attempts=1
    )
    assert report.retained == 1
    assert report.results[0].action == "unknown"


async def test_sweep_calls_observer_and_marks_settled_when_call_succeeded(
    connection,
):
    """When the observer reports SUCCESS, the provider call is reconciled."""
    store = OperationStore()
    record, lease = await leased_operation(store, connection)

    ik = derive_idempotency_key(
        operation_id=record.operation_id,
        attempt_id="attempt-1",
        step="step-1",
    )
    await record_intent(
        connection,
        lease,
        idempotency_key=ik,
        provider="test-provider",
        operation_kind="create",
        target="resource-b",
    )

    # Expire the lease.
    await connection.execute(
        """
        UPDATE harness_operation_leases
           SET expires_at = clock_timestamp() - '1 second'::interval
         WHERE operation_id = $1
        """,
        record.operation_id,
    )

    observed = []

    async def observer(idempotency_key, provider, operation_kind, target):
        observed.append(idempotency_key)
        return CallOutcome.SUCCEEDED, "confirmed by provider", "prov-ref-001"

    report = await sweep_expired_leases(connection, observe_call=observer)
    # Observer confirmed SUCCEEDED: recovery propagates this accurately.
    # Spend is confirmed, but completion of the workflow is not.
    assert report.retried == 0
    assert report.results[0].action == "unknown"
    assert report.results[0].detail == (
        "budget settled; workflow completion unconfirmed"
    )
    assert ik in observed


# ---------------------------------------------------------------------------
# sweep_unresolved_calls: orphaned intents
# ---------------------------------------------------------------------------


async def test_sweep_unresolved_calls_settles_orphaned_intents(connection):
    """Orphaned `intended` rows (no live lease) are settled by the sweep."""
    store = OperationStore()
    record, lease = await leased_operation(store, connection)

    ik = derive_idempotency_key(
        operation_id=record.operation_id,
        attempt_id="attempt-1",
        step="step-1",
    )
    await record_intent(
        connection,
        lease,
        idempotency_key=ik,
        provider="test-provider",
        operation_kind="create",
        target="resource-c",
    )

    # Close the lease so the intent is now orphaned.
    await close(
        connection,
        operation_id=record.operation_id,
        reason="test: close for orphan sweep",
        fence_token=lease.fence_token,
        holder=lease.holder,
    )

    async def observer(idempotency_key, provider, operation_kind, target):
        return CallOutcome.ABSENT, "nothing was created", None

    settled = await sweep_unresolved_calls(connection, observe_call=observer)
    assert settled == 1


async def test_sweep_unresolved_calls_skips_intents_with_live_lease(connection):
    """An intent whose lease is still live is not swept (the holder owns it)."""
    store = OperationStore()
    record, lease = await leased_operation(store, connection)

    ik = derive_idempotency_key(
        operation_id=record.operation_id,
        attempt_id="attempt-1",
        step="step-1",
    )
    await record_intent(
        connection,
        lease,
        idempotency_key=ik,
        provider="test-provider",
        operation_kind="create",
        target="resource-d",
    )

    call_count = 0

    async def observer(idempotency_key, provider, operation_kind, target):
        nonlocal call_count
        call_count += 1
        return CallOutcome.UNKNOWN, None, None

    settled = await sweep_unresolved_calls(connection, observe_call=observer)
    assert settled == 0, "live-lease intent must not be swept"
    assert call_count == 0


# ---------------------------------------------------------------------------
# fence_expired_lease: recovery-only fence advance without attempt increment.
# ---------------------------------------------------------------------------


async def test_fence_expired_lease_returns_none_when_lease_is_live(connection):
    """A lease that has not yet expired must not be taken over by recovery.

    Probe-reproduced defect: the old code used `acquire()` for recovery, which
    would succeed even on non-expired leases, incorrectly stealing the lease from
    its rightful holder. `fence_expired_lease` returns None when the lease is
    still live.
    """
    store = OperationStore()
    record, _ = await leased_operation(store, connection)

    result = await fence_expired_lease(connection, operation_id=record.operation_id)
    assert result is None, (
        "fence_expired_lease must return None when the lease has not expired"
    )


async def test_fence_expired_lease_advances_token_and_does_not_increment_attempts(
    connection,
):
    """Takeover advances the fence token but does NOT consume another attempt.

    Using `acquire()` for recovery incremented `attempts`, which meant each
    sweep tick counted as a failed attempt. `fence_expired_lease` is the
    correct recovery primitive: it fences the stale holder without consuming
    an attempt slot.
    """
    store = OperationStore()
    record, lease = await leased_operation(store, connection)

    # Expire the lease.
    await connection.execute(
        """
        UPDATE harness_operation_leases
           SET expires_at = clock_timestamp() - '1 second'::interval
         WHERE operation_id = $1
        """,
        record.operation_id,
    )

    takeover = await fence_expired_lease(connection, operation_id=record.operation_id)
    assert takeover is not None
    assert takeover.fence_token == lease.fence_token + 1, (
        "fence token must advance so the stale holder's writes match zero rows"
    )
    assert takeover.attempts == lease.attempts, (
        "attempts must NOT be incremented; only explicit retries consume attempt slots"
    )


async def test_fence_expired_lease_returns_none_when_already_closed(connection):
    """A closed lease has no holder to fence; the function must return None."""
    store = OperationStore()
    record, lease = await leased_operation(store, connection)

    await close(
        connection,
        operation_id=record.operation_id,
        reason="test close",
        fence_token=lease.fence_token,
        holder=lease.holder,
    )

    result = await fence_expired_lease(connection, operation_id=record.operation_id)
    assert result is None, (
        "fence_expired_lease must return None when the lease is already closed"
    )


# ---------------------------------------------------------------------------
# Recovery state transitions: retry reacquirability, cancel, exhaustion.
# ---------------------------------------------------------------------------


async def test_sweep_retried_operation_is_reacquirable(connection):
    """After recovery marks an operation as retried, a new worker can acquire it.

    This is the core guarantee: a crashed operation is not permanently lost.
    The retry must leave the lease in a state where `acquire()` succeeds for
    the next worker.
    """
    store = OperationStore()
    record, _ = await leased_operation(store, connection)

    # Expire the lease.
    await connection.execute(
        """
        UPDATE harness_operation_leases
           SET expires_at = clock_timestamp() - '1 second'::interval
         WHERE operation_id = $1
        """,
        record.operation_id,
    )

    report = await sweep_expired_leases(connection)
    assert report.retried == 1, "precondition: sweep must have reported a retry"

    # A new worker must be able to acquire the lease.
    new_lease = await acquire(
        connection,
        operation_id=record.operation_id,
        holder="worker-next",
        attempt_id="attempt-next",
    )
    assert new_lease is not None, (
        "a retried operation must be acquirable by the next worker"
    )
    assert new_lease.fence_token > 1, (
        "the fence token must have advanced so the old worker is fenced out"
    )


async def test_sweep_settles_cancelled_expired_lease(connection):
    """An expired lease on a cancel-requested operation is settled as CANCELLED.

    Recovery must honour a cancellation request even when the executing worker
    was lost: the resource must not be provisioned on behalf of a cancelled job.
    """
    store = OperationStore()
    record, _ = await leased_operation(store, connection)

    # Request cancellation before the lease expires.
    await request_cancellation(
        connection,
        operation_id=record.operation_id,
        principal=cancellation_principal("user:cancel-test"),
    )

    # Expire the lease.
    await connection.execute(
        """
        UPDATE harness_operation_leases
           SET expires_at = clock_timestamp() - '1 second'::interval
         WHERE operation_id = $1
        """,
        record.operation_id,
    )

    report = await sweep_expired_leases(connection)
    assert report.retained == 1
    assert report.results[0].action == "cancelled"

    row = await connection.fetchrow(
        "SELECT state FROM harness_operations WHERE operation_id = $1",
        record.operation_id,
    )
    assert row["state"] == "cancelled", (
        "recovery must settle a cancel-requested operation to CANCELLED"
    )


async def test_sweep_settles_attempts_exhausted_operation_as_failed(connection):
    """When all attempt slots are consumed, recovery settles the operation as FAILED.

    Probe-reproduced defect: the old code re-acquired a lease (incrementing
    attempts), meaning a 3-attempt operation would eventually exceed max_attempts
    and be refused with `ATTEMPTS_EXHAUSTED`, but the operation would never
    transition to FAILED. The new `fence_expired_lease` path checks attempts
    against the ceiling and calls `_settle_operation(FAILED)` when exhausted.
    """
    store = OperationStore()
    record, lease = await leased_operation(store, connection)

    # Set attempts to the ceiling (default max_attempts=5) and expire the lease.
    await connection.execute(
        """
        UPDATE harness_operation_leases
           SET attempts = 5,
               expires_at = clock_timestamp() - '1 second'::interval
         WHERE operation_id = $1
        """,
        record.operation_id,
    )

    report = await sweep_expired_leases(connection)
    assert report.retained == 1
    assert report.results[0].action == "failed"

    row = await connection.fetchrow(
        "SELECT state FROM harness_operations WHERE operation_id = $1",
        record.operation_id,
    )
    assert row["state"] == "failed", (
        "recovery must settle an attempts-exhausted operation to FAILED"
    )


async def test_sweep_is_idempotent_for_already_terminal_operation(connection):
    """Running the sweep twice against an already-settled operation is safe.

    Recovery must never double-settle or produce spurious results when the
    operation is already terminal (e.g. because a prior sweep run or another
    writer settled it).
    """
    store = OperationStore()
    record, _ = await leased_operation(store, connection)

    # Expire the lease.
    await connection.execute(
        """
        UPDATE harness_operation_leases
           SET expires_at = clock_timestamp() - '1 second'::interval
         WHERE operation_id = $1
        """,
        record.operation_id,
    )

    # First sweep: settles as failed (attempts==1 of default max 5, but we
    # override attempts here to force exhaustion).
    await connection.execute(
        """
        UPDATE harness_operation_leases SET attempts = 5
         WHERE operation_id = $1
        """,
        record.operation_id,
    )
    first = await sweep_expired_leases(connection)
    assert first.retained == 1

    # Second sweep: operation is already terminal; nothing more to do.
    second = await sweep_expired_leases(connection)
    assert second.retried == 0
    assert second.retained == 0
    assert second.skipped == 0, (
        "a terminal operation with a closed lease has no expired holder "
        "to sweep; the second run must touch nothing"
    )


# ---------------------------------------------------------------------------
# Repair 1: sweep_unresolved_calls holds advisory lock across provider I/O
# ---------------------------------------------------------------------------


async def test_sweep_unresolved_calls_holds_advisory_lock_across_io(pool):
    """sweep_unresolved_calls must not interleave with a concurrent executor.

    Two-connection race: connection A starts sweep_unresolved_calls (which acquires
    the operation's advisory lock before I/O); connection B attempts to acquire the
    same advisory lock using pg_try_advisory_lock while A's observer is in flight.
    B must fail to acquire the lock, proving recovery serialises with execution.

    After A's observer returns and commits, B must succeed in acquiring the lock.
    """
    import asyncio

    store = OperationStore()
    async with pool.acquire() as setup:
        record, lease = await leased_operation(store, setup)
        ik = derive_idempotency_key(record.operation_id, "attempt-1", "step-1")
        await record_intent(
            setup,
            lease,
            idempotency_key=ik,
            provider="test-provider",
            operation_kind="create",
            target="resource-x",
        )
        await close(
            setup,
            operation_id=record.operation_id,
            reason="test: close for orphan sweep",
            fence_token=lease.fence_token,
            holder=lease.holder,
        )

    observer_entered = asyncio.Event()
    observer_proceed = asyncio.Event()
    lock_key = f"harness-provider-dispatch:{record.operation_id}"

    async def observer(idempotency_key, provider, operation_kind, target):
        observer_entered.set()
        await asyncio.wait_for(observer_proceed.wait(), 5)
        return CallOutcome.ABSENT, "nothing created", None

    async with pool.acquire() as sweep_conn, pool.acquire() as probe_conn:
        task = asyncio.create_task(
            sweep_unresolved_calls(sweep_conn, observe_call=observer)
        )
        try:
            await asyncio.wait_for(observer_entered.wait(), 3)

            # While the observer is in flight, try to acquire the same advisory lock.
            got_lock = await probe_conn.fetchval(
                "SELECT pg_try_advisory_lock(hashtextextended($1, 0))", lock_key
            )
            assert not got_lock, (
                "probe acquired the advisory lock while sweep_unresolved_calls was "
                "mid-observer; the lock must be held across I/O to prevent interleaving"
            )

            observer_proceed.set()
            await asyncio.wait_for(task, 3)

            # After sweep completes (lock released), probe must now succeed.
            got_lock_after = await probe_conn.fetchval(
                "SELECT pg_try_advisory_lock(hashtextextended($1, 0))", lock_key
            )
            assert got_lock_after, (
                "lock must be released after sweep_unresolved_calls completes"
            )
            await probe_conn.execute(
                "SELECT pg_advisory_unlock(hashtextextended($1, 0))", lock_key
            )
        finally:
            observer_proceed.set()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)


async def test_sweep_unresolved_calls_skips_operation_with_active_dispatch_lock(pool):
    """If the advisory lock is held by an executor, sweep skips the intent.

    Reproduces the race where sweep_unresolved_calls would have processed an
    intent while a live executor was mid-provider-call for the same operation.
    The lock prevents this: sweep skips and the call is settled by the executor.
    """
    store = OperationStore()
    async with pool.acquire() as setup:
        record, lease = await leased_operation(store, setup)
        ik = derive_idempotency_key(record.operation_id, "attempt-1", "step-1")
        await record_intent(
            setup,
            lease,
            idempotency_key=ik,
            provider="test-provider",
            operation_kind="create",
            target="resource-y",
        )
        # Close the lease so the intent appears orphaned to sweep_unresolved_calls
        # (no live holder), while we separately hold the advisory lock to simulate
        # an executor that is mid-provider-call.
        await close(
            setup,
            operation_id=record.operation_id,
            reason="test: close for advisory-lock skip test",
            fence_token=lease.fence_token,
            holder=lease.holder,
        )

    call_count = 0

    async def observer(*_):
        nonlocal call_count
        call_count += 1
        return CallOutcome.ABSENT, None, None

    async with pool.acquire() as sweep_conn, pool.acquire() as lock_holder:
        lock_key = f"harness-provider-dispatch:{record.operation_id}"
        await lock_holder.fetchval(
            "SELECT pg_advisory_lock(hashtextextended($1, 0))", lock_key
        )
        try:
            settled = await sweep_unresolved_calls(sweep_conn, observe_call=observer)
            assert settled == 0, "sweep must skip when advisory lock is held"
            assert call_count == 0, "observer must not be called"
        finally:
            await lock_holder.execute(
                "SELECT pg_advisory_unlock(hashtextextended($1, 0))", lock_key
            )


# ---------------------------------------------------------------------------
# Repair 2: orphan recovery durably finalises the owning operation
# ---------------------------------------------------------------------------


async def test_orphan_recovery_settles_operation_with_release_disposition(pool):
    """After reconciling an orphaned ABSENT call, the operation is settled FAILED.

    Updating the call row alone must not strand the operation in a pending state
    behind a closed/unheld lease. Persisted operation state and ledger disposition
    must match the reconciled call outcome.
    """
    store = OperationStore()
    async with pool.acquire() as setup:
        record, lease = await leased_operation(store, setup)
        ik = derive_idempotency_key(record.operation_id, "attempt-1", "step-1")
        await record_intent(
            setup,
            lease,
            idempotency_key=ik,
            provider="test-provider",
            operation_kind="create",
            target="resource-z",
        )
        await close(
            setup,
            operation_id=record.operation_id,
            reason="test: close for orphan finalise",
            fence_token=lease.fence_token,
            holder=lease.holder,
        )

    async def observer(idempotency_key, provider, operation_kind, target):
        return CallOutcome.ABSENT, "nothing was created", None

    async with pool.acquire() as conn:
        settled = await sweep_unresolved_calls(conn, observe_call=observer)

    assert settled == 1
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT state FROM harness_operations WHERE operation_id=$1",
            record.operation_id,
        )
        assert row["state"] == "failed", (
            "ABSENT outcome must settle the operation as FAILED (budget released)"
        )
        # Verify the lease is closed.
        lease_row = await conn.fetchrow(
            "SELECT closed_at FROM harness_operation_leases WHERE operation_id=$1",
            record.operation_id,
        )
        assert lease_row is not None and lease_row["closed_at"] is not None, (
            "lease must be closed after orphan recovery settles the operation"
        )


async def test_orphan_recovery_retains_budget_for_unknown_outcome(pool):
    """An UNKNOWN outcome leaves the operation non-terminal until reconciled.

    When the observer cannot establish what happened, the operation must NOT be
    settled as failed. Budget must be retained.
    """
    store = OperationStore()
    async with pool.acquire() as setup:
        record, lease = await leased_operation(store, setup)
        ik = derive_idempotency_key(record.operation_id, "attempt-1", "step-1")
        await record_intent(
            setup,
            lease,
            idempotency_key=ik,
            provider="test-provider",
            operation_kind="create",
            target="resource-w",
        )
        await close(
            setup,
            operation_id=record.operation_id,
            reason="test: close for unknown outcome",
            fence_token=lease.fence_token,
            holder=lease.holder,
        )

    async def observer(idempotency_key, provider, operation_kind, target):
        return CallOutcome.UNKNOWN, None, None

    async with pool.acquire() as conn:
        # One attempt: max_reconcile_attempts=1 means unknown at attempt 1 is
        # treated as exhausted and written as UNRESOLVED.
        settled = await sweep_unresolved_calls(
            conn, observe_call=observer, max_reconcile_attempts=1
        )

    assert settled == 1
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT state FROM harness_operations WHERE operation_id=$1",
            record.operation_id,
        )
        # UNKNOWN outcome → operation becomes 'unknown' (not 'failed'), budget retained.
        assert row["state"] == "unknown", (
            "UNKNOWN outcome must settle the operation as 'unknown', not 'failed'"
        )


async def test_orphan_recovery_settle_does_not_overwrite_terminal_operation(pool):
    """If the operation was already settled by another path, orphan recovery must not
    overwrite it. _settle_operation is conditioned on non-terminal states.
    """
    store = OperationStore()
    async with pool.acquire() as setup:
        record, lease = await leased_operation(store, setup)
        ik = derive_idempotency_key(record.operation_id, "attempt-1", "step-1")
        await record_intent(
            setup,
            lease,
            idempotency_key=ik,
            provider="test-provider",
            operation_kind="create",
            target="resource-v",
        )
        await close(
            setup,
            operation_id=record.operation_id,
            reason="test: pre-closed for idempotency test",
            fence_token=lease.fence_token,
            holder=lease.holder,
        )
        # Manually settle as succeeded before the sweep runs.
        await setup.execute(
            "UPDATE harness_operations SET state='succeeded', version=version+1 "
            "WHERE operation_id=$1",
            record.operation_id,
        )

    async def observer(idempotency_key, provider, operation_kind, target):
        return CallOutcome.ABSENT, "nothing", None

    async with pool.acquire() as conn:
        await sweep_unresolved_calls(conn, observe_call=observer)

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT state FROM harness_operations WHERE operation_id=$1",
            record.operation_id,
        )
        assert row["state"] == "succeeded", (
            "orphan recovery must not overwrite a terminal operation state"
        )


# ---------------------------------------------------------------------------
# Repair 3: execution plan verification during recovery
# ---------------------------------------------------------------------------


async def _plan_operation(
    store, connection, key="plan-op", org="org-a", ws="ws-1", steps=None
):
    """Admit an operation that has an admitted execution plan."""
    import json
    from dataclasses import replace as dc_replace

    from .test_admission_postgres import principal, request

    steps = steps or [
        dict(step_id="step-1", provider="prov", operation_kind="create", target="t1"),
    ]
    req = dc_replace(request(key), parameters={"execution_steps": json.dumps(steps)})
    admitted = await admit_paid(
        store, connection, principal(org=org, workspace=ws), req
    )
    record = admitted.record
    lease = await acquire(
        connection,
        operation_id=record.operation_id,
        holder="worker-plan",
        attempt_id="attempt-plan",
        duration=timedelta(seconds=60),
    )
    return record, lease


async def _derive_step_key(record, step_id):
    """Derive the stable idempotency key for a plan step (mirrors step_key)."""
    import hashlib
    import json

    material = json.dumps(
        [
            record.org_id,
            record.workspace_id,
            record.operation_id,
            record.plan_digest,
            step_id,
        ],
        separators=(",", ":"),
    )
    return "operation-step:" + hashlib.sha256(material.encode()).hexdigest()


async def test_recovery_settles_succeeded_when_all_plan_steps_confirmed(connection):
    """All admitted plan steps confirmed SUCCEEDED must settle the operation SUCCEEDED.

    Recovery currently returns UNKNOWN for 'all calls succeeded', treating them as
    a possible prefix. This repair checks the admitted plan: if every planned step
    has a recorded SUCCEEDED call, the operation is durably SUCCEEDED.
    """
    store = OperationStore()
    steps = [
        dict(step_id="step-1", provider="prov", operation_kind="create", target="t1"),
    ]
    record, lease = await _plan_operation(store, connection, steps=steps)

    step_ik = await _derive_step_key(record, "step-1")
    await record_intent(
        connection,
        lease,
        idempotency_key=step_ik,
        provider="prov",
        operation_kind="create",
        target="t1",
    )
    from harness_jobs.execution import observe

    await observe(
        connection, lease, idempotency_key=step_ik, outcome=CallOutcome.SUCCEEDED
    )

    # Expire the lease so sweep_expired_leases can recover it.
    await connection.execute(
        "UPDATE harness_operation_leases "
        "SET expires_at=clock_timestamp()-interval '1 second' "
        "WHERE operation_id=$1",
        record.operation_id,
    )

    report = await sweep_expired_leases(connection)
    assert report.retained == 1
    assert report.results[0].action == "succeeded", (
        "when every planned step is confirmed SUCCEEDED, recovery must settle SUCCEEDED"
    )

    row = await connection.fetchrow(
        "SELECT state FROM harness_operations WHERE operation_id=$1",
        record.operation_id,
    )
    assert row["state"] == "succeeded"


async def test_recovery_defers_for_partial_plan_prefix_and_preserves_succeeded_keys(
    connection,
):
    """Recovery releases its claim for bounded continuation of a confirmed prefix."""
    store = OperationStore()
    steps = [
        dict(step_id="step-1", provider="prov", operation_kind="create", target="t1"),
        dict(step_id="step-2", provider="prov", operation_kind="create", target="t2"),
    ]
    record, lease = await _plan_operation(
        store, connection, key="partial-plan", steps=steps
    )

    # Record and observe only step-1 (not step-2).
    step1_ik = await _derive_step_key(record, "step-1")
    await record_intent(
        connection,
        lease,
        idempotency_key=step1_ik,
        provider="prov",
        operation_kind="create",
        target="t1",
    )
    from harness_jobs.execution import CallStage, observe, read_call

    await observe(
        connection, lease, idempotency_key=step1_ik, outcome=CallOutcome.SUCCEEDED
    )

    # Expire and sweep.
    await connection.execute(
        "UPDATE harness_operation_leases "
        "SET expires_at=clock_timestamp()-interval '1 second' "
        "WHERE operation_id=$1",
        record.operation_id,
    )

    report = await sweep_expired_leases(connection)
    assert report.retried == 1
    assert report.results[0].action == "retried"

    # The operation must not have been settled as terminal.
    row = await connection.fetchrow(
        "SELECT state FROM harness_operations WHERE operation_id=$1",
        record.operation_id,
    )
    assert row["state"] == "pending", (
        "operation with partial succeeded plan must remain pending for continuation"
    )

    # Step-1's call must remain observed so the next executor does not reissue it.
    step1_call = await read_call(connection, idempotency_key=step1_ik)
    assert step1_call.stage is CallStage.OBSERVED, (
        "already-succeeded step must remain observed; stable key must not be reissued"
    )


async def test_recovery_unknown_preserved_when_plan_absent(connection):
    """Without an admitted execution plan, all-succeeded calls fall back to UNKNOWN."""
    store = OperationStore()
    record, lease = await leased_operation(store, connection, key="no-plan")

    ik = derive_idempotency_key(record.operation_id, "attempt-1", "step-1")
    await record_intent(
        connection,
        lease,
        idempotency_key=ik,
        provider="test-provider",
        operation_kind="create",
        target="t",
    )
    from harness_jobs.execution import observe

    await observe(connection, lease, idempotency_key=ik, outcome=CallOutcome.SUCCEEDED)

    await connection.execute(
        "UPDATE harness_operation_leases "
        "SET expires_at=clock_timestamp()-interval '1 second' "
        "WHERE operation_id=$1",
        record.operation_id,
    )
    report = await sweep_expired_leases(connection)
    assert report.results[0].action == "unknown", (
        "without a plan, all-succeeded calls must remain UNKNOWN "
        "(the calls may be only a subset of required steps)"
    )


async def test_recovery_ownership_loss_negatives(pool):
    """A successor that takes the lease during recovery is not closed by the old pass.

    After the observer returns, the old recovery pass loses ownership (fence token
    advanced by a second fence_expired_lease). Its final settle/close attempts must
    be refused, and the operation must remain in its pre-settled state.
    """
    import asyncio

    from harness_jobs.leases import fence_expired_lease as _fence

    store = OperationStore()
    async with pool.acquire() as setup:
        record, lease = await leased_operation(store, setup)
        ik = derive_idempotency_key(record.operation_id, "attempt-1", "s1")
        await record_intent(
            setup,
            lease,
            idempotency_key=ik,
            provider="p",
            operation_kind="c",
            target="t",
        )
        await setup.execute(
            "UPDATE harness_operation_leases "
            "SET expires_at=clock_timestamp()-interval '1 second' "
            "WHERE operation_id=$1",
            record.operation_id,
        )

    entered, proceed = asyncio.Event(), asyncio.Event()

    async def observer(*_):
        entered.set()
        await asyncio.wait_for(proceed.wait(), 5)
        return CallOutcome.ABSENT, "confirmed absent", None

    async with pool.acquire() as recovery_conn, pool.acquire() as successor_conn:
        task = asyncio.create_task(
            sweep_expired_leases(recovery_conn, observe_call=observer)
        )
        try:
            await asyncio.wait_for(entered.wait(), 3)
            # Advance fence token from a second connection while observer is in flight.
            await successor_conn.execute(
                "UPDATE harness_operation_leases "
                "SET expires_at=clock_timestamp()-interval '1 second' "
                "WHERE operation_id=$1",
                record.operation_id,
            )
            await _fence(successor_conn, operation_id=record.operation_id)
            proceed.set()
            report = await asyncio.wait_for(task, 3)
            # Recovery pass must report skipped (lost claim), not retained.
            assert report.skipped == 1, (
                "old recovery pass must skip when it loses the lease to a successor"
            )
            # Operation must not have been settled.
            row = await successor_conn.fetchrow(
                "SELECT state FROM harness_operations WHERE operation_id=$1",
                record.operation_id,
            )
            assert row["state"] == "pending", (
                "a recovery pass that lost ownership must not settle the operation"
            )
        finally:
            proceed.set()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
