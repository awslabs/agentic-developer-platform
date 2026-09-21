"""Provider-call intent: committed before the call, reconciled after uncertainty.

Issue #5527 (w6-04), EPIC #4910, Wave 6.

The property under test is durability across process loss, so these tests need a real
database: "the row survived the crash" is a claim about what is on disk, and a fake
would be asserting that a dict retains its keys.

The tests that carry the acceptance criteria are:

* `test_a_stale_worker_cannot_observe_*` -- AC-02, and the expensive half of it: a stale
  worker publishing terminal success for a call its successor is re-making.
* `test_an_unknown_outcome_*` -- AC-02's second clause, that uncertain cleanup is never
  reported as complete.
* the `test_*duplicate*` and `test_*crash*` cases -- AC-01's fault injection.
"""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from harness_jobs import OperationStore
from harness_jobs.execution import (
    BudgetDisposition,
    CallOutcome,
    CallStage,
    ProviderCall,
    ProviderCallRefused,
    audit,
    derive_idempotency_key,
    disposition_for,
    observe,
    read_audit,
    read_call,
    reconcile,
    record_intent,
    unresolved_calls,
)
from harness_jobs.identity import ContractViolation
from harness_jobs.leases import ExecutionLease, acquire

from .conftest import admit_paid, cancellation_principal, requires_postgres
from .test_admission_postgres import principal, request

pytestmark = requires_postgres

CALL = {
    "provider": "aws",
    "operation_kind": "create_vpc",
    "target": "account/111122223333",
}


async def leased(pool, key="key-1", holder="worker-1", attempt="attempt-1", **kwargs):
    """An admitted, paid-for operation with a live lease held by `holder`."""
    store = OperationStore()
    async with pool.acquire() as connection, connection.transaction():
        admitted = await admit_paid(store, connection, principal(), request(key))
        lease = await acquire(
            connection,
            operation_id=admitted.record.operation_id,
            holder=holder,
            attempt_id=attempt,
            **kwargs,
        )
    return lease


def call_in(stage, outcome):
    """A `ProviderCall` in a given settled state, to test the rule without a database.

    The disposition rule is pure, so it deserves tests that cannot fail for a reason
    involving PostgreSQL.
    """
    fixed = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    return ProviderCall(
        idempotency_key="k",
        operation_id="op-1",
        org_id="org-a",
        workspace_id="ws-1",
        job_id="job-1",
        attempt_id="attempt-1",
        fence_token=1,
        provider="aws",
        operation_kind="create_vpc",
        target="account/1",
        stage=stage,
        outcome=outcome,
        provider_ref=None,
        created_at=fixed,
        updated_at=fixed,
    )


# ---------------------------------------------------------------------------
# The key, and intent before effect
# ---------------------------------------------------------------------------


def test_the_idempotency_key_is_derived_from_durable_facts():
    """A recovering process must be able to recompute it from the operation alone.

    This is why it is derived and not random: after a crash the key is the only way to
    ask the provider about *that* call, and a generated key would be exactly the thing
    the crash destroyed.
    """
    first = derive_idempotency_key("op-1", "attempt-1", "vpc")
    assert first == derive_idempotency_key("op-1", "attempt-1", "vpc")
    assert first != derive_idempotency_key("op-1", "attempt-2", "vpc")
    assert first != derive_idempotency_key("op-1", "attempt-1", "subnet")


def test_a_separator_in_a_component_is_refused():
    """Otherwise two different calls derive one key and the provider cannot tell them
    apart -- `a/b` + `c` and `a` + `b/c` would collide.
    """
    with pytest.raises(ContractViolation):
        derive_idempotency_key("op/1", "attempt-1", "vpc")
    with pytest.raises(ContractViolation):
        derive_idempotency_key("op-1", "attempt/1", "vpc")


async def test_recording_an_intent_commits_before_any_call_is_made(pool):
    """The core durability claim: the row exists, at `intended`, with nothing observed.

    `intended` rather than a "pending" flag because the stage must be readable as "the
    provider may or may not have been called" -- the one honest description of the state
    a crashed worker leaves behind.
    """
    lease = await leased(pool)
    key = derive_idempotency_key(lease.operation_id, lease.attempt_id, "vpc")
    async with pool.acquire() as connection:
        async with connection.transaction():
            call = await record_intent(connection, lease, idempotency_key=key, **CALL)
        assert call.stage is CallStage.INTENDED
        assert call.outcome is None
        assert call.provider_ref is None
        assert call.fence_token == lease.fence_token
        assert call.attempt_id == lease.attempt_id
        assert call.may_have_happened is True
        assert call.is_settled is False

        # Committed, so it survives this connection going away.
        persisted = await read_call(connection, idempotency_key=key)
    assert persisted == call


async def test_the_intent_survives_the_worker_process_dying(pool):
    """AC-01: worker crash. The row is the only evidence the call may have happened.

    The connection is terminated after the intent commits and before any outcome is
    recorded, which is a process killed mid-call from the database's point of view. What
    must be true afterwards is that the row is still there and still says `intended` --
    if it said `failed`, a retry would duplicate a provision that may already exist.
    """
    lease = await leased(pool)
    key = derive_idempotency_key(lease.operation_id, lease.attempt_id, "vpc")

    worker = await pool.acquire()
    async with worker.transaction():
        await record_intent(worker, lease, idempotency_key=key, **CALL)
    # The provider is now "being called". The worker dies here.
    await worker.close()

    async with pool.acquire() as survivor:
        recovered = await read_call(survivor, idempotency_key=key)
    assert recovered is not None
    assert recovered.stage is CallStage.INTENDED
    assert recovered.may_have_happened is True


async def test_an_uncommitted_intent_leaves_nothing_which_is_why_ordering_matters(pool):
    """The negative control for the ordering rule, stated as a test.

    A worker that recorded the intent inside the same transaction as its call -- and so
    had not committed when it died -- leaves no row. This test does not assert that the
    package is wrong; it asserts *why* `record_intent` documents that it must commit
    before the provider is contacted. If this row survived, the ordering rule would be
    unnecessary.
    """
    lease = await leased(pool)
    key = derive_idempotency_key(lease.operation_id, lease.attempt_id, "vpc")
    async with pool.acquire() as connection:
        try:
            async with connection.transaction():
                await record_intent(connection, lease, idempotency_key=key, **CALL)
                raise RuntimeError("the worker dies before committing")
        except RuntimeError:
            pass
        assert await read_call(connection, idempotency_key=key) is None


async def test_a_fenced_out_worker_cannot_record_a_new_intent(pool):
    """Recording an intent is the first half of spending, so the fence applies to it.

    Otherwise a stale worker could commit a new provider call and then make it, and
    the call would be attributed to an attempt that no longer exists.
    """
    store = OperationStore()
    async with pool.acquire() as connection:
        async with connection.transaction():
            admitted = await admit_paid(
                store, connection, principal(), request("fenced")
            )
            stale = await acquire(
                connection,
                operation_id=admitted.record.operation_id,
                holder="worker-1",
                attempt_id="attempt-1",
                duration=timedelta(milliseconds=1),
            )
        await asyncio.sleep(0.05)
        from harness_jobs.recovery import sweep_expired_leases

        await sweep_expired_leases(connection)
        async with connection.transaction():
            await acquire(
                connection,
                operation_id=admitted.record.operation_id,
                holder="worker-2",
                attempt_id="attempt-2",
            )
        key = derive_idempotency_key(stale.operation_id, stale.attempt_id, "vpc")
        with pytest.raises(ProviderCallRefused):
            async with connection.transaction():
                await record_intent(connection, stale, idempotency_key=key, **CALL)
        assert await read_call(connection, idempotency_key=key) is None


async def test_a_hand_built_lease_is_refused(pool):
    """The fence must not be a field the caller fills in.

    A caller that could pass any object with `holder` and `fence_token` attributes would
    be asserting its own entitlement, which is the opposite of presenting one.
    """
    lease = await leased(pool)

    class Forged:
        operation_id = lease.operation_id
        holder = "worker-1"
        fence_token = 99
        attempt_id = "attempt-1"

    async with pool.acquire() as connection, connection.transaction():
        with pytest.raises(ContractViolation):
            await record_intent(
                connection, Forged(), idempotency_key="forged-key", **CALL
            )


# ---------------------------------------------------------------------------
# Duplicate delivery. AC-01.
# ---------------------------------------------------------------------------


async def test_the_same_attempt_recording_the_same_key_twice_makes_one_call(pool):
    """AC-01: duplicate queue delivery. The same envelope arriving twice.

    The second `record_intent` must return the existing row rather than create a second
    one or raise: two rows would be two provider calls, and raising would make a
    redelivery look like a failure and burn an attempt.
    """
    lease = await leased(pool)
    key = derive_idempotency_key(lease.operation_id, lease.attempt_id, "vpc")
    async with pool.acquire() as connection:
        async with connection.transaction():
            first = await record_intent(connection, lease, idempotency_key=key, **CALL)
        async with connection.transaction():
            second = await record_intent(connection, lease, idempotency_key=key, **CALL)
        assert second == first
        count = await connection.fetchval(
            "SELECT count(*) FROM harness_provider_call_intent WHERE operation_id = $1",
            lease.operation_id,
        )
    assert count == 1


async def test_a_redelivery_after_the_call_completed_does_not_reopen_it(pool):
    """A duplicate arriving late gets the settled row back, not a fresh `intended` one.

    If it reopened the call, the redelivered envelope would cause a second provider call
    for work already done -- the exact duplicate spend the idempotency key exists to
    prevent.
    """
    lease = await leased(pool)
    key = derive_idempotency_key(lease.operation_id, lease.attempt_id, "vpc")
    async with pool.acquire() as connection:
        async with connection.transaction():
            await record_intent(connection, lease, idempotency_key=key, **CALL)
        async with connection.transaction():
            await observe(
                connection,
                lease,
                idempotency_key=key,
                outcome=CallOutcome.SUCCEEDED,
                provider_ref="vpc-abc123",
            )
        async with connection.transaction():
            again = await record_intent(connection, lease, idempotency_key=key, **CALL)
    assert again.stage is CallStage.OBSERVED
    assert again.provider_ref == "vpc-abc123"


async def test_an_effect_bearing_attempt_cannot_release_and_retry(pool):
    from harness_jobs.leases import LeaseRefused, release

    lease = await leased(pool)
    key = derive_idempotency_key(lease.operation_id, lease.attempt_id, "vpc")
    async with pool.acquire() as connection:
        await record_intent(connection, lease, idempotency_key=key, **CALL)
        assert not await release(connection, lease)
        with pytest.raises(LeaseRefused):
            await acquire(
                connection,
                operation_id=lease.operation_id,
                holder="worker-2",
                attempt_id="attempt-2",
            )


async def test_concurrent_recordings_of_one_key_produce_one_row(pool):
    """The PRIMARY KEY under genuine concurrency, on separate connections.

    Two workers cannot both believe they are about to issue the same provider call.
    """
    lease = await leased(pool)
    key = derive_idempotency_key(lease.operation_id, lease.attempt_id, "vpc")

    async def contend():
        async with pool.acquire() as held:
            try:
                async with held.transaction():
                    return await record_intent(held, lease, idempotency_key=key, **CALL)
            except Exception:
                return None

    outcomes = await asyncio.gather(*(contend() for _ in range(5)))
    async with pool.acquire() as connection:
        count = await connection.fetchval(
            "SELECT count(*) FROM harness_provider_call_intent "
            "WHERE idempotency_key = $1",
            key,
        )
    assert count == 1
    assert any(result is not None for result in outcomes)


# ---------------------------------------------------------------------------
# Observing an outcome, and the fence. AC-02.
# ---------------------------------------------------------------------------


async def test_observing_success_settles_the_spend(pool):
    lease = await leased(pool)
    key = derive_idempotency_key(lease.operation_id, lease.attempt_id, "vpc")
    async with pool.acquire() as connection:
        async with connection.transaction():
            await record_intent(connection, lease, idempotency_key=key, **CALL)
        async with connection.transaction():
            call, owed = await observe(
                connection,
                lease,
                idempotency_key=key,
                outcome=CallOutcome.SUCCEEDED,
                provider_ref="vpc-abc123",
            )
    assert call.stage is CallStage.OBSERVED
    assert call.outcome is CallOutcome.SUCCEEDED
    assert call.provider_ref == "vpc-abc123"
    assert call.may_have_happened is False
    assert owed is BudgetDisposition.SETTLE


async def test_observing_a_refusal_releases_the_hold(pool):
    """`FAILED` means the provider established that nothing was created."""
    lease = await leased(pool)
    key = derive_idempotency_key(lease.operation_id, lease.attempt_id, "vpc")
    async with pool.acquire() as connection:
        async with connection.transaction():
            await record_intent(connection, lease, idempotency_key=key, **CALL)
        async with connection.transaction():
            call, owed = await observe(
                connection,
                lease,
                idempotency_key=key,
                outcome=CallOutcome.FAILED,
                detail="QuotaExceeded: no VPC capacity in this account",
            )
    assert owed is BudgetDisposition.RELEASE
    assert call.outcome is CallOutcome.FAILED
    assert call.may_have_happened is False


async def test_an_unknown_outcome_retains_budget_and_stays_uncertain(pool):
    """AC-02, second clause: uncertain cleanup is never reported as complete.

    `UNKNOWN` must land on `UNRESOLVED` rather than `OBSERVED` -- the worker observed
    nothing -- and must retain rather than release. Releasing here would give back the
    reservation for a resource that may be running, so the resource becomes both
    untracked and unfunded.
    """
    lease = await leased(pool)
    key = derive_idempotency_key(lease.operation_id, lease.attempt_id, "vpc")
    async with pool.acquire() as connection:
        async with connection.transaction():
            await record_intent(connection, lease, idempotency_key=key, **CALL)
        async with connection.transaction():
            call, owed = await observe(
                connection,
                lease,
                idempotency_key=key,
                outcome=CallOutcome.UNKNOWN,
                detail="socket timeout after 30s",
            )
    assert call.stage is CallStage.UNRESOLVED
    assert call.outcome is CallOutcome.UNKNOWN
    assert owed is BudgetDisposition.RETAIN
    assert call.may_have_happened is True, (
        "an unknown outcome must never read as 'nothing exists'; this is the AC-02 "
        "failure where uncertain cleanup is reported complete"
    )


async def test_a_stale_worker_cannot_observe_an_outcome(pool):
    """AC-02, the expensive case: a fenced-out worker publishing terminal success.

    Worker 1's lease lapses mid-call, worker 2 takes over, worker 1's reply arrives. If
    worker 1 could write `SUCCEEDED`, the operation would be reported complete while
    worker 2 is still making its own call -- so the platform would believe one resource
    exists where two may.
    """
    store = OperationStore()
    async with pool.acquire() as connection:
        async with connection.transaction():
            admitted = await admit_paid(store, connection, principal(), request("s"))
            stale = await acquire(
                connection,
                operation_id=admitted.record.operation_id,
                holder="worker-1",
                attempt_id="attempt-1",
                duration=timedelta(seconds=30),
            )
        key = derive_idempotency_key(stale.operation_id, stale.attempt_id, "vpc")
        async with connection.transaction():
            await record_intent(connection, stale, idempotency_key=key, **CALL)

        # The lease lapses and worker 2 takes over while worker 1 is mid-call.
        await connection.execute(
            "UPDATE harness_operation_leases SET expires_at = now() - interval '1s' "
            "WHERE operation_id = $1",
            stale.operation_id,
        )
        from harness_jobs.leases import fence_expired_lease

        assert await fence_expired_lease(connection, operation_id=stale.operation_id)

        with pytest.raises(ProviderCallRefused):
            async with connection.transaction():
                await observe(
                    connection,
                    stale,
                    idempotency_key=key,
                    outcome=CallOutcome.SUCCEEDED,
                    provider_ref="vpc-from-stale-worker",
                )

        unchanged = await read_call(connection, idempotency_key=key)
    assert unchanged is not None
    assert unchanged.stage is CallStage.INTENDED, (
        "a stale worker published a terminal outcome; this is the AC-02 failure"
    )
    assert unchanged.provider_ref is None


async def test_standalone_observe_is_atomic_with_concurrent_release(pool, monkeypatch):
    """Release cannot strand an intent while its observation is pending."""
    import harness_jobs.execution as execution
    from harness_jobs.leases import lock_lease, release

    lease = await leased(pool, key="observe-release-race")
    key = derive_idempotency_key(lease.operation_id, lease.attempt_id, "vpc")
    async with pool.acquire() as setup:
        await record_intent(setup, lease, idempotency_key=key, **CALL)

    observation_paused = asyncio.Event()
    lease_released = asyncio.Event()

    async def paused_lock(connection, candidate):
        observation_paused.set()
        await lease_released.wait()
        return await lock_lease(connection, candidate)

    monkeypatch.setattr(execution, "lock_lease", paused_lock)
    async with pool.acquire() as observer, pool.acquire() as releaser:
        observation = asyncio.create_task(
            observe(
                observer,
                lease,
                idempotency_key=key,
                outcome=CallOutcome.SUCCEEDED,
            )
        )
        await observation_paused.wait()
        assert not await release(releaser, lease)
        lease_released.set()
        await asyncio.wait_for(observation, 3)

        unchanged = await read_call(releaser, idempotency_key=key)

    assert unchanged is not None
    assert unchanged.stage is CallStage.OBSERVED
    assert unchanged.outcome is CallOutcome.SUCCEEDED


async def test_a_worker_cannot_observe_a_call_recorded_at_another_token(pool):
    """The token on the row, not just on the lease, has to match.

    A worker that legitimately holds the lease at token 2 must not settle a call
    recorded at token 1: that call was made by a different attempt, and its outcome
    belongs to that attempt's record.
    """
    lease = await leased(pool)
    key = derive_idempotency_key(lease.operation_id, lease.attempt_id, "vpc")
    async with pool.acquire() as connection:
        async with connection.transaction():
            await record_intent(connection, lease, idempotency_key=key, **CALL)
        # Same holder, same live lease, but the row was recorded at a lower token.
        await connection.execute(
            "UPDATE harness_operation_leases SET fence_token = fence_token + 1 "
            "WHERE operation_id = $1",
            lease.operation_id,
        )
        advanced = ExecutionLease(
            operation_id=lease.operation_id,
            org_id=lease.org_id,
            workspace_id=lease.workspace_id,
            holder=lease.holder,
            fence_token=lease.fence_token + 1,
            attempt_id=lease.attempt_id,
            expires_at=lease.expires_at,
            acquired_at=lease.acquired_at,
            runtime_deadline=lease.runtime_deadline,
            attempts=lease.attempts,
        )
        with pytest.raises(ProviderCallRefused):
            async with connection.transaction():
                await observe(
                    connection,
                    advanced,
                    idempotency_key=key,
                    outcome=CallOutcome.SUCCEEDED,
                )


async def test_an_outcome_cannot_be_recorded_twice(pool):
    """A settled call is settled. Re-observing would overwrite a recorded fact.

    This is also AC-01's "timeout after provider success": the worker timed out and
    reported `UNKNOWN`, and then its original reply arrived. The late reply must not
    overwrite the unresolved row, which has already been handed to a human or a sweep.
    """
    lease = await leased(pool)
    key = derive_idempotency_key(lease.operation_id, lease.attempt_id, "vpc")
    async with pool.acquire() as connection:
        async with connection.transaction():
            await record_intent(connection, lease, idempotency_key=key, **CALL)
        async with connection.transaction():
            await observe(
                connection, lease, idempotency_key=key, outcome=CallOutcome.UNKNOWN
            )
        with pytest.raises(ProviderCallRefused):
            async with connection.transaction():
                await observe(
                    connection,
                    lease,
                    idempotency_key=key,
                    outcome=CallOutcome.SUCCEEDED,
                    provider_ref="vpc-late-reply",
                )
        still = await read_call(connection, idempotency_key=key)
    assert still is not None
    assert still.stage is CallStage.UNRESOLVED
    assert still.provider_ref is None


# ---------------------------------------------------------------------------
# Reconciliation: asking the provider afterwards. AC-01 recovery.
# ---------------------------------------------------------------------------


async def test_reconciling_a_crashed_call_that_did_happen_settles_the_spend(pool):
    """AC-01: recovery after restart, where the provider says the resource exists.

    The budget must be settled, not released: something real was created and someone has
    to pay for it. The stage records that this fact came from asking rather than from
    observing, which is what an incident review needs to distinguish.
    """
    lease = await leased(pool)
    key = derive_idempotency_key(lease.operation_id, lease.attempt_id, "vpc")
    async with pool.acquire() as connection:
        async with connection.transaction():
            await record_intent(connection, lease, idempotency_key=key, **CALL)
        async with connection.transaction():
            call, owed = await reconcile(
                connection,
                idempotency_key=key,
                outcome=CallOutcome.SUCCEEDED,
                provider_ref="vpc-abc123",
            )
    assert call.stage is CallStage.RECONCILED
    assert owed is BudgetDisposition.SETTLE
    assert call.provider_ref == "vpc-abc123", (
        "the provider ref must be captured: a later teardown needs it to release what "
        "the crashed attempt left standing"
    )


async def test_reconciling_a_call_that_never_happened_releases_the_hold(pool):
    """`ABSENT` is the only outcome that establishes absence, so the only release."""
    lease = await leased(pool)
    key = derive_idempotency_key(lease.operation_id, lease.attempt_id, "vpc")
    async with pool.acquire() as connection:
        async with connection.transaction():
            await record_intent(connection, lease, idempotency_key=key, **CALL)
        async with connection.transaction():
            call, owed = await reconcile(
                connection, idempotency_key=key, outcome=CallOutcome.ABSENT
            )
    assert owed is BudgetDisposition.RELEASE
    assert call.may_have_happened is False


async def test_an_unreachable_provider_leaves_the_call_unresolved_and_the_budget_held(
    pool,
):
    """The uncertain case, which must not be resolved by assumption in either direction.

    `UNRESOLVED` is terminal for the sweep and is not a failure. The budget stays held
    because nothing has established that the resource is absent.
    """
    lease = await leased(pool)
    key = derive_idempotency_key(lease.operation_id, lease.attempt_id, "vpc")
    async with pool.acquire() as connection:
        async with connection.transaction():
            await record_intent(connection, lease, idempotency_key=key, **CALL)
        async with connection.transaction():
            call, owed = await reconcile(
                connection,
                idempotency_key=key,
                outcome=CallOutcome.UNKNOWN,
                detail="provider endpoint unreachable",
            )
    assert call.stage is CallStage.UNRESOLVED
    assert owed is BudgetDisposition.RETAIN
    assert call.is_settled is True, "the sweep must not keep retrying this row"
    assert call.may_have_happened is True


async def test_a_sweep_must_not_overwrite_an_unresolved_row(pool):
    """`unresolved` records a decision to involve a human; a timer must not override it.

    Without this, a scheduled sweep would re-decide a case an operator is investigating,
    every time it ran.
    """
    lease = await leased(pool)
    key = derive_idempotency_key(lease.operation_id, lease.attempt_id, "vpc")
    async with pool.acquire() as connection:
        async with connection.transaction():
            await record_intent(connection, lease, idempotency_key=key, **CALL)
        async with connection.transaction():
            await reconcile(
                connection, idempotency_key=key, outcome=CallOutcome.UNKNOWN
            )
        with pytest.raises(ProviderCallRefused):
            async with connection.transaction():
                await reconcile(
                    connection, idempotency_key=key, outcome=CallOutcome.ABSENT
                )


async def test_reconciling_an_already_observed_call_is_refused(pool):
    """The worker's own observation is authoritative; a sweep must not revise it."""
    lease = await leased(pool)
    key = derive_idempotency_key(lease.operation_id, lease.attempt_id, "vpc")
    async with pool.acquire() as connection:
        async with connection.transaction():
            await record_intent(connection, lease, idempotency_key=key, **CALL)
        async with connection.transaction():
            await observe(
                connection, lease, idempotency_key=key, outcome=CallOutcome.SUCCEEDED
            )
        with pytest.raises(ProviderCallRefused):
            async with connection.transaction():
                await reconcile(
                    connection, idempotency_key=key, outcome=CallOutcome.ABSENT
                )


async def test_reconciling_an_absent_call_is_refused_rather_than_inventing_one(pool):
    async with pool.acquire() as connection, connection.transaction():
        with pytest.raises(ProviderCallRefused):
            await reconcile(
                connection, idempotency_key="never-recorded", outcome=CallOutcome.ABSENT
            )


async def test_reconciliation_needs_no_lease_because_the_holder_is_gone(pool):
    """Stated as a test because it looks like a missing check.

    Requiring a lease here would make the unrecoverable case exactly the case that
    cannot be recovered: the worker is dead, so nobody holds the lease. The compensating
    control is that this is not reachable from a request path, and the audit names the
    sweep.
    """
    lease = await leased(pool)
    key = derive_idempotency_key(lease.operation_id, lease.attempt_id, "vpc")
    async with pool.acquire() as connection:
        async with connection.transaction():
            await record_intent(connection, lease, idempotency_key=key, **CALL)
        # The lease is closed entirely -- no holder exists anywhere.
        await connection.execute(
            "UPDATE harness_operation_leases SET holder = NULL, expires_at = NULL, "
            "acquired_at = NULL, runtime_deadline = NULL, attempt_id = NULL, "
            "closed_at = now(), closed_reason = 'worker lost' WHERE operation_id = $1",
            lease.operation_id,
        )
        async with connection.transaction():
            call, owed = await reconcile(
                connection, idempotency_key=key, outcome=CallOutcome.SUCCEEDED
            )
    assert call.stage is CallStage.RECONCILED
    assert owed is BudgetDisposition.SETTLE


# ---------------------------------------------------------------------------
# The enumerable set
# ---------------------------------------------------------------------------


async def test_unresolved_calls_lists_only_calls_still_in_flight(pool):
    """`intended` only: the enumerable set recovery iterates.

    `unresolved` is excluded on purpose -- see `test_a_sweep_must_not_overwrite_*`.
    """
    keys = {}
    for index, outcome in enumerate(
        [None, CallOutcome.SUCCEEDED, CallOutcome.UNKNOWN, None]
    ):
        lease = await leased(pool, key=f"enum-{index}", attempt=f"attempt-{index}")
        key = derive_idempotency_key(lease.operation_id, lease.attempt_id, "vpc")
        keys[key] = outcome
        async with pool.acquire() as connection:
            async with connection.transaction():
                await record_intent(connection, lease, idempotency_key=key, **CALL)
            if outcome is not None:
                async with connection.transaction():
                    await observe(
                        connection, lease, idempotency_key=key, outcome=outcome
                    )

    async with pool.acquire() as connection:
        pending = await unresolved_calls(connection)
    listed = {call.idempotency_key for call in pending}
    assert listed == {key for key, outcome in keys.items() if outcome is None}
    assert all(call.stage is CallStage.INTENDED for call in pending)


async def test_the_unresolved_listing_is_bounded(pool):
    """An unbounded list turns one sweep into an arbitrarily expensive read."""
    async with pool.acquire() as connection:
        assert await unresolved_calls(connection, limit=10_000) == ()
        assert await unresolved_calls(connection, limit=0) == ()


# ---------------------------------------------------------------------------
# The disposition rule, in isolation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("stage", "outcome", "expected"),
    [
        (CallStage.INTENDED, None, BudgetDisposition.RETAIN),
        (CallStage.OBSERVED, CallOutcome.SUCCEEDED, BudgetDisposition.SETTLE),
        (CallStage.OBSERVED, CallOutcome.FAILED, BudgetDisposition.RELEASE),
        (CallStage.RECONCILED, CallOutcome.SUCCEEDED, BudgetDisposition.SETTLE),
        (CallStage.RECONCILED, CallOutcome.ABSENT, BudgetDisposition.RELEASE),
        (CallStage.RECONCILED, CallOutcome.UNKNOWN, BudgetDisposition.RETAIN),
        (CallStage.UNRESOLVED, CallOutcome.UNKNOWN, BudgetDisposition.RETAIN),
    ],
)
def test_the_budget_disposition_is_a_total_function_of_the_settled_state(
    stage, outcome, expected
):
    """Every reachable combination, because the unsafe direction is silent.

    A missing case that fell through to `RELEASE` would give back the reservation for a
    resource that exists, and nothing would report an error -- the invoice would.
    """
    assert disposition_for(call_in(stage, outcome)) is expected


def test_no_outcome_ever_maps_to_release_except_established_absence():
    """The rule restated over the whole enum rather than a sampled table.

    This is the assertion that fails if a `CallOutcome` is added later and someone wires
    it optimistically -- the realistic way this control would regress.
    """
    releasing = {CallOutcome.FAILED, CallOutcome.ABSENT}
    for outcome in CallOutcome:
        owed = disposition_for(call_in(CallStage.RECONCILED, outcome))
        if owed is BudgetDisposition.RELEASE:
            assert outcome in releasing, (
                f"{outcome} must not release budget: it does not establish that "
                "nothing was created"
            )


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------


async def test_a_refused_action_is_recorded_even_though_it_changed_nothing(pool):
    """The audit's reason for existing: refusals are invisible in current state.

    A fenced-out worker's attempt to publish success changes no row anywhere. Without an
    append-only record, the single most security-relevant event in the system would have
    no trace at all.
    """
    lease = await leased(pool)
    async with pool.acquire() as connection:
        async with connection.transaction():
            await audit(
                connection,
                operation_id=lease.operation_id,
                org_id=lease.org_id,
                workspace_id=lease.workspace_id,
                attempt_id=lease.attempt_id,
                fence_token=lease.fence_token,
                event="observe_outcome",
                actor="worker-1",
                allowed=False,
                detail="fenced out: lease held at a higher token",
            )
        events = await read_audit(
            connection,
            operation_id=lease.operation_id,
            org_id=lease.org_id,
            workspace_id=lease.workspace_id,
        )
    assert [e["event"] for e in events] == ["lease.acquire", "observe_outcome"]
    assert events[1]["allowed"] is False
    assert events[1]["actor"] == "worker-1"
    assert events[1]["fence_token"] == lease.fence_token


async def test_the_audit_is_ordered_and_append_only(pool):
    """ "What did the system do, in order" is a question current state cannot answer."""
    lease = await leased(pool)
    async with pool.acquire() as connection:
        async with connection.transaction():
            for index, (event, allowed) in enumerate(
                [
                    ("lease_acquired", True),
                    ("intent_recorded", True),
                    ("observe_outcome", False),
                    ("lease_released", True),
                ]
            ):
                await audit(
                    connection,
                    operation_id=lease.operation_id,
                    org_id=lease.org_id,
                    workspace_id=lease.workspace_id,
                    event=event,
                    actor=f"worker-{index}",
                    allowed=allowed,
                )
        events = await read_audit(
            connection,
            operation_id=lease.operation_id,
            org_id=lease.org_id,
            workspace_id=lease.workspace_id,
        )
    assert [row["event"] for row in events] == [
        "lease.acquire",
        "lease_acquired",
        "intent_recorded",
        "observe_outcome",
        "lease_released",
    ]


async def test_the_audit_survives_the_operation_being_deleted(pool):
    """No foreign key, deliberately: the record must outlive its subject.

    A cascade would turn a row deletion into the destruction of the evidence of what was
    spent. This test is what stops someone "tidying up" the schema by adding the FK that
    looks missing.

    Deleting the operation means deleting what references it, and this test does that by
    hand: the lease and the intent cascade, but `harness_approval_consumption` (#5526)
    restricts on purpose, so a budget record cannot be silently dropped. The audit row
    is the only one of the four still standing at the end -- which is the point.
    """
    lease = await leased(pool)
    async with pool.acquire() as connection:
        async with connection.transaction():
            await audit(
                connection,
                operation_id=lease.operation_id,
                org_id=lease.org_id,
                workspace_id=lease.workspace_id,
                event="intent_recorded",
                actor="worker-1",
                allowed=True,
            )
        async with connection.transaction():
            await connection.execute(
                "DELETE FROM harness_approval_consumption WHERE operation_id = $1",
                lease.operation_id,
            )
            await connection.execute(
                "DELETE FROM harness_operations WHERE operation_id = $1",
                lease.operation_id,
            )
        remaining = await connection.fetchval(
            "SELECT count(*) FROM harness_operation_leases WHERE operation_id = $1",
            lease.operation_id,
        )
        assert remaining == 0, "the lease should have cascaded away with its operation"

        events = await read_audit(
            connection,
            operation_id=lease.operation_id,
            org_id=lease.org_id,
            workspace_id=lease.workspace_id,
        )
    assert len(events) == 2, "deleting an operation destroyed its audit trail"
    assert events[0]["event"] == "lease.acquire"


async def test_the_audit_is_tenant_scoped(pool):
    """Another tenant's history reads as empty, the same answer as a missing operation.

    A distinguishable "exists but forbidden" would confirm the operation exists and leak
    how many execution attempts it took.
    """
    lease = await leased(pool)
    async with pool.acquire() as connection:
        async with connection.transaction():
            await audit(
                connection,
                operation_id=lease.operation_id,
                org_id=lease.org_id,
                workspace_id=lease.workspace_id,
                event="intent_recorded",
                actor="worker-1",
                allowed=True,
            )
        assert (
            await read_audit(
                connection,
                operation_id=lease.operation_id,
                org_id="org-intruder",
                workspace_id=lease.workspace_id,
            )
            == ()
        )
        assert (
            await read_audit(
                connection,
                operation_id=lease.operation_id,
                org_id=lease.org_id,
                workspace_id="ws-intruder",
            )
            == ()
        )


async def test_an_audit_event_without_an_explicit_verdict_is_refused(pool):
    """`allowed` must be stated. A default would make every unstated event look allowed,
    and the refusals are the half of this table that matters.
    """
    lease = await leased(pool)
    async with pool.acquire() as connection, connection.transaction():
        with pytest.raises(ContractViolation):
            await audit(
                connection,
                operation_id=lease.operation_id,
                org_id=lease.org_id,
                workspace_id=lease.workspace_id,
                event="observe_outcome",
                actor="worker-1",
                allowed="yes",  # type: ignore[arg-type]
            )


async def test_an_audit_event_can_carry_no_token_at_all(pool):
    """Some events happen when the actor holds no lease: a refused acquisition, a user's
    cancellation request. Recording 0 would be indistinguishable from a real token.
    """
    lease = await leased(pool)
    async with pool.acquire() as connection:
        async with connection.transaction():
            await audit(
                connection,
                operation_id=lease.operation_id,
                org_id=lease.org_id,
                workspace_id=lease.workspace_id,
                event="lease_refused",
                actor="worker-9",
                allowed=False,
            )
        events = await read_audit(
            connection,
            operation_id=lease.operation_id,
            org_id=lease.org_id,
            workspace_id=lease.workspace_id,
        )
    assert events[-1]["event"] == "lease_refused"
    assert events[-1]["fence_token"] is None


# ---------------------------------------------------------------------------
# Two-connection regressions for call-binding defects found in the foreground probe.
# ---------------------------------------------------------------------------


async def test_observe_cannot_update_a_different_operations_intent(pool):
    """A lease for operation A cannot resolve operation B's provider call.

    Probe-reproduced defect: `observe` checks that SOME lease exists with the right
    holder/token, but never binds the lease's operation_id to the intent's operation_id.
    A lease for op A can therefore update op B's intent if both happen to carry
    fence_token=1 (which they do when each is acquired fresh).

    The fix: `observe` adds `i.operation_id = $7`, `i.attempt_id = $8`,
    `i.org_id = $9`, `i.workspace_id = $10` to the WHERE clause.
    """
    store = OperationStore()

    # Two operations in the same pool/schema but different tenants.
    async with pool.acquire() as setup, setup.transaction():
        from .conftest import admit_paid
        from .test_admission_postgres import principal, request

        admitted_a = await admit_paid(
            store,
            setup,
            principal(org="org-a", workspace="ws-1"),
            request("op-a-key"),
        )
        admitted_b = await admit_paid(
            store,
            setup,
            principal(org="org-b", workspace="ws-1"),
            request("op-b-key"),
        )

    key_b = "ik-op-b"
    async with pool.acquire() as connection:
        async with connection.transaction():
            lease_a = await acquire(
                connection,
                operation_id=admitted_a.record.operation_id,
                holder="worker-same-name",
                attempt_id="attempt-a",
            )
        async with connection.transaction():
            lease_b = await acquire(
                connection,
                operation_id=admitted_b.record.operation_id,
                holder="worker-same-name",
                attempt_id="attempt-b",
            )
            # Record an intent under lease_b's operation.
            await record_intent(connection, lease_b, idempotency_key=key_b, **CALL)

        # Both leases have fence_token=1. Attempt to resolve B's intent using lease_a.
        async with connection.transaction():
            with pytest.raises(ProviderCallRefused):
                await observe(
                    connection,
                    lease_a,
                    idempotency_key=key_b,
                    outcome=CallOutcome.SUCCEEDED,
                )

    # Verify the row was NOT updated by the cross-operation attempt.
    async with pool.acquire() as connection:
        still_intended = await read_call(connection, idempotency_key=key_b)
    assert still_intended is not None
    assert still_intended.stage is CallStage.INTENDED, (
        "observe with lease_a should not have updated op_b's intent; "
        "the operation/attempt/tenant binding in the WHERE clause must be enforced"
    )


async def test_record_intent_replay_with_different_binding_is_refused(pool):
    """Re-recording a key with different provider/kind/target is refused.

    Probe-reproduced defect: `record_intent` returned an existing row when
    `attempt_id` matched, before validating provider/kind/target equivalence. A
    released holder replaying the key with different values was accepted.

    The fix: when the same attempt re-records, the three immutable binding fields
    must match; a mismatch raises `ProviderCallRefused`.
    """
    lease = await leased(pool)

    key = "ik-binding-test"
    async with pool.acquire() as connection:
        async with connection.transaction():
            await record_intent(
                connection,
                lease,
                idempotency_key=key,
                provider="aws",
                operation_kind="create_vpc",
                target="account/111122223333",
            )
        async with connection.transaction():
            with pytest.raises(ProviderCallRefused):
                await record_intent(
                    connection,
                    lease,
                    idempotency_key=key,
                    provider="aws",
                    operation_kind="create_vpc",
                    target="account/DIFFERENT",
                )
            with pytest.raises(ProviderCallRefused):
                await record_intent(
                    connection,
                    lease,
                    idempotency_key=key,
                    provider="gcp",
                    operation_kind="create_vpc",
                    target="account/111122223333",
                )


async def test_record_intent_after_release_is_refused(pool):
    """An unused lease may release, after which its stale intent is refused."""
    from harness_jobs.leases import release

    lease = await leased(pool)
    async with pool.acquire() as connection:
        assert await release(connection, lease)
        with pytest.raises(ProviderCallRefused):
            await record_intent(connection, lease, idempotency_key="released", **CALL)


# ---------------------------------------------------------------------------
# Cross-operation binding in replay path. Probe finding #3.
# ---------------------------------------------------------------------------


async def test_record_intent_cross_operation_replay_is_refused(pool):
    """A lease for op A cannot return op B's row via the idempotent replay path.

    Probe-reproduced defect: `record_intent`'s duplicate check only tested
    attempt_id, so a key recorded by op B (attempt_id='attempt-b', fence_token=1)
    was returned to op A's lease (attempt_id='attempt-b', fence_token=1) when
    both happened to share those values.  The fix: the replay check also validates
    operation_id, org_id, workspace_id, and fence_token.
    """
    store = OperationStore()
    # Two operations in different tenants; both get natural fence_token=1.
    async with pool.acquire() as setup, setup.transaction():
        admitted_a = await admit_paid(
            store, setup, principal(org="org-a", workspace="ws-1"), request("xa-key")
        )
        admitted_b = await admit_paid(
            store, setup, principal(org="org-b", workspace="ws-1"), request("xb-key")
        )

    key_b = "ik-cross-op-test"
    async with pool.acquire() as connection:
        # Both leases get fence_token=1 and the same holder/attempt_id.
        async with connection.transaction():
            lease_a = await acquire(
                connection,
                operation_id=admitted_a.record.operation_id,
                holder="worker-x",
                attempt_id="attempt-x",
            )
        async with connection.transaction():
            lease_b = await acquire(
                connection,
                operation_id=admitted_b.record.operation_id,
                holder="worker-x",
                attempt_id="attempt-x",  # same as lease_a
            )
        # Record op B's intent.
        async with connection.transaction():
            await record_intent(connection, lease_b, idempotency_key=key_b, **CALL)

        # Replay with lease_a: key belongs to a different operation.
        # Must be refused, not returned as the idempotent row.
        async with connection.transaction():
            with pytest.raises(ProviderCallRefused):
                await record_intent(connection, lease_a, idempotency_key=key_b, **CALL)

    # The row must still be op B's.
    async with pool.acquire() as connection:
        row = await read_call(connection, idempotency_key=key_b)
    assert row is not None
    assert row.operation_id == admitted_b.record.operation_id, (
        "cross-operation replay must be refused; "
        "op A's lease must not return op B's row"
    )


# ---------------------------------------------------------------------------
# Runtime deadline enforcement in record_intent and observe.
# ---------------------------------------------------------------------------


async def test_record_intent_is_refused_when_runtime_deadline_expired(pool):
    """A worker whose runtime ceiling has passed cannot record a new provider call."""
    store = OperationStore()
    async with pool.acquire() as connection:
        async with connection.transaction():
            admitted = await admit_paid(
                store, connection, principal(), request("rt-ri")
            )
            lease = await acquire(
                connection,
                operation_id=admitted.record.operation_id,
                holder="worker-rt",
                attempt_id="attempt-rt",
            )

    key = "ik-expired-runtime-ri"
    async with pool.acquire() as connection:
        # Expire the runtime_deadline while keeping expires_at in the future.
        await connection.execute(
            """
            UPDATE harness_operation_leases
               SET runtime_deadline = clock_timestamp() - '1 second'::interval
             WHERE operation_id = $1
            """,
            lease.operation_id,
        )
        async with connection.transaction():
            with pytest.raises(ProviderCallRefused):
                await record_intent(connection, lease, idempotency_key=key, **CALL)
    # No row must have been written.
    async with pool.acquire() as connection:
        row = await read_call(connection, idempotency_key=key)
    assert row is None, "record_intent must be refused when runtime_deadline has passed"


async def test_observe_is_refused_when_runtime_deadline_expired(pool):
    """A worker whose runtime ceiling has passed cannot report an outcome."""
    store = OperationStore()
    async with pool.acquire() as connection:
        async with connection.transaction():
            admitted = await admit_paid(
                store, connection, principal(), request("rt-obs")
            )
            lease = await acquire(
                connection,
                operation_id=admitted.record.operation_id,
                holder="worker-rt-obs",
                attempt_id="attempt-rt-obs",
            )

    key = "ik-expired-runtime-obs"
    async with pool.acquire() as connection:
        async with connection.transaction():
            await record_intent(connection, lease, idempotency_key=key, **CALL)
        # Expire the runtime_deadline.
        await connection.execute(
            """
            UPDATE harness_operation_leases
               SET runtime_deadline = clock_timestamp() - '1 second'::interval
             WHERE operation_id = $1
            """,
            lease.operation_id,
        )
        async with connection.transaction():
            with pytest.raises(ProviderCallRefused):
                await observe(
                    connection,
                    lease,
                    idempotency_key=key,
                    outcome=CallOutcome.SUCCEEDED,
                )
    # Row must still be INTENDED.
    async with pool.acquire() as connection:
        row = await read_call(connection, idempotency_key=key)
    assert row is not None
    assert row.stage is CallStage.INTENDED, (
        "observe must be refused when runtime_deadline has passed"
    )


# ---------------------------------------------------------------------------
# OperationExecutor: worker-facing interface.
# ---------------------------------------------------------------------------


async def test_operation_executor_status_returns_current_state(pool):
    """The executor's status method reads the operation's current state."""
    from harness_jobs.execution import OperationExecutor, OperationStatus

    lease = await leased(pool)
    executor = OperationExecutor(lease, connect=pool.acquire)
    status = await executor.status()
    assert isinstance(status, OperationStatus)
    assert status.operation_id == lease.operation_id
    assert not status.cancel_requested


async def test_operation_executor_cancel_requested_reflects_pending_cancellation(pool):
    """cancel_requested() returns True once a cancellation request is written."""
    from harness_jobs.execution import OperationExecutor
    from harness_jobs.recovery import request_cancellation

    lease = await leased(pool)
    async with pool.acquire() as connection:
        executor = OperationExecutor(lease, connect=pool.acquire)
        assert not await executor.cancel_requested()

        await request_cancellation(
            connection,
            operation_id=lease.operation_id,
            principal=cancellation_principal("user:test"),
        )
        assert await executor.cancel_requested()


async def test_operation_executor_status_is_none_when_lease_released(pool):
    """status() returns None after the lease has been released."""
    from harness_jobs.execution import OperationExecutor
    from harness_jobs.leases import release

    lease = await leased(pool)
    async with pool.acquire() as connection:
        executor = OperationExecutor(lease, connect=pool.acquire)
        await release(connection, lease)
        status = await executor.status()
    assert status is None, "stale executor must not receive operation status"


async def test_operation_executor_settle_writes_terminal_state(pool):
    """settle() writes a terminal outcome, fenced by the lease."""
    from harness_jobs.execution import OperationExecutor
    from harness_jobs.identity import OperationState

    lease = await leased(pool)
    async with pool.acquire() as connection:
        executor = OperationExecutor(lease, connect=pool.acquire)
        async with connection.transaction():
            written = await executor.settle(
                state=OperationState.SUCCEEDED, detail="all done"
            )
    assert written, "settle must write when the lease is live"

    # Verify the state is persisted.
    async with pool.acquire() as connection:
        row = await connection.fetchrow(
            "SELECT state FROM harness_operations WHERE operation_id = $1",
            lease.operation_id,
        )
    assert row["state"] == "succeeded"


async def test_operation_executor_settle_is_refused_for_stale_worker(pool):
    """A fenced-out executor cannot publish a terminal state."""
    import asyncio

    from harness_jobs import OperationStore
    from harness_jobs.execution import OperationExecutor
    from harness_jobs.identity import OperationState

    store = OperationStore()
    async with pool.acquire() as connection, connection.transaction():
        admitted = await admit_paid(
            store, connection, principal(), request("exec-stale")
        )
        stale = await acquire(
            connection,
            operation_id=admitted.record.operation_id,
            holder="worker-stale",
            attempt_id="attempt-stale",
            duration=timedelta(milliseconds=1),
        )
    # Let the lease expire.
    await asyncio.sleep(0.05)

    # Recovery verifies no provider call before making the operation retryable.
    from harness_jobs.recovery import sweep_expired_leases

    async with pool.acquire() as connection:
        await sweep_expired_leases(connection)
        async with connection.transaction():
            await acquire(
                connection,
                operation_id=admitted.record.operation_id,
                holder="worker-new",
                attempt_id="attempt-new",
            )

    # The stale worker tries to settle.
    async with pool.acquire() as connection:
        executor = OperationExecutor(stale, connect=pool.acquire)
        async with connection.transaction():
            written = await executor.settle(state=OperationState.SUCCEEDED)
    assert not written, (
        "a fenced-out executor must not write a terminal state; "
        "this is the AC-02 failure where a stale worker publishes success"
    )

    # The operation must remain in its pre-stale state.
    async with pool.acquire() as connection:
        row = await connection.fetchrow(
            "SELECT state FROM harness_operations WHERE operation_id = $1",
            admitted.record.operation_id,
        )
    assert row["state"] == "pending", (
        "the operation state must not have been written by the stale executor"
    )


async def test_operation_executor_requires_execution_lease(pool):
    """OperationExecutor refuses anything that is not a real ExecutionLease."""
    from harness_jobs.execution import OperationExecutor
    from harness_jobs.identity import ContractViolation

    with pytest.raises(ContractViolation):
        OperationExecutor("not-a-lease", connect=pool.acquire)


async def test_operation_executor_audit_records_settled_and_refused_events(pool):
    """Both allowed and refused settle events appear in the audit trail."""
    import asyncio

    from harness_jobs import OperationStore
    from harness_jobs.execution import OperationExecutor, read_audit
    from harness_jobs.identity import OperationState

    store = OperationStore()
    async with pool.acquire() as connection, connection.transaction():
        admitted = await admit_paid(
            store, connection, principal(), request("exec-audit")
        )
        stale = await acquire(
            connection,
            operation_id=admitted.record.operation_id,
            holder="worker-audit",
            attempt_id="attempt-audit",
            duration=timedelta(milliseconds=1),
        )
    await asyncio.sleep(0.05)

    async with pool.acquire() as connection:
        from harness_jobs.recovery import sweep_expired_leases

        await sweep_expired_leases(connection)
        async with connection.transaction():
            await acquire(
                connection,
                operation_id=admitted.record.operation_id,
                holder="worker-new-audit",
                attempt_id="attempt-new-audit",
            )

    # Stale executor tries to settle: should be refused and audited.
    async with pool.acquire() as connection:
        executor = OperationExecutor(stale, connect=pool.acquire)
        async with connection.transaction():
            await executor.settle(state=OperationState.SUCCEEDED)

    # The audit trail must contain the refused event.
    async with pool.acquire() as connection:
        rows = await read_audit(
            connection,
            operation_id=admitted.record.operation_id,
            org_id=stale.org_id,
            workspace_id=stale.workspace_id,
        )
    refused_events = [r for r in rows if r["event"] == "settle" and not r["allowed"]]
    assert refused_events, (
        "a refused settle must be audited even though it changed nothing"
    )
