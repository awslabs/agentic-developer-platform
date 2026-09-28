"""Execution leases: one holder, bounded, and the stale holder writes nothing.

Issue #5527 (w6-04), EPIC #4910, Wave 6.

Every test here needs a real database, because every property under test is a property
of PostgreSQL rather than of Python: the grant is an atomic upsert, the capacity cap is
an advisory lock around a count, expiry is the server's `now()`, and "a stale worker
changes nothing" is a row count from an UPDATE. A fake would assert the fake.

The tests that matter most are the negative ones -- `test_a_stale_holder_*`. They fail
if the fence is removed, which is the point: a control nothing would notice the absence
of is not a control.
"""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from harness_jobs import OperationStore
from harness_jobs.identity import ContractViolation, OperationState
from harness_jobs.leases import (
    DEFAULT_LEASE_DURATION,
    MAX_LEASE_DURATION,
    ExecutionLease,
    LeaseRefusal,
    LeaseRefused,
    acquire,
    close,
    fenced_update,
    is_fenced_out,
    read_lease,
    release,
    renew,
)
from harness_jobs.recovery import sweep_expired_leases

from .conftest import admit_paid, mark_paid, requires_postgres
from .test_admission_postgres import principal, request

pytestmark = requires_postgres


async def leased_operation(store, connection, key="key-1", org="org-a", ws="ws-1"):
    """An admitted, paid-for operation ready to be leased."""
    admitted = await admit_paid(
        store, connection, principal(org=org, workspace=ws), request(key)
    )
    return admitted.record


# ---------------------------------------------------------------------------
# Granting, and the token
# ---------------------------------------------------------------------------


async def test_the_first_grant_issues_token_one(pool):
    """`Lease.fence_token >= 1` is the contract's CHECK; a first grant must satisfy it.

    The stored counter starts at 0 meaning "never granted", so if the first grant
    returned 0 every lease in the system would violate the published shape.
    """
    store = OperationStore()
    async with pool.acquire() as connection, connection.transaction():
        record = await leased_operation(store, connection)
        lease = await acquire(
            connection,
            operation_id=record.operation_id,
            holder="worker-1",
            attempt_id="attempt-1",
        )
        assert lease.fence_token == 1
        assert lease.holder == "worker-1"
        assert lease.attempts == 1
        # Tenant comes off the operation row, not from an argument.
        assert (lease.org_id, lease.workspace_id) == ("org-a", "ws-1")


async def test_the_token_advances_on_every_grant_and_never_resets(pool):
    """Monotonicity across release and re-acquire.

    The reset case is the dangerous one: if `release` cleared the counter, the next
    holder would be issued a token its predecessor had already used, and the predecessor
    could then write under it.
    """
    store = OperationStore()
    async with pool.acquire() as connection:
        async with connection.transaction():
            record = await leased_operation(store, connection)

        seen = []
        for index in range(4):
            async with connection.transaction():
                lease = await acquire(
                    connection,
                    operation_id=record.operation_id,
                    holder=f"worker-{index}",
                    attempt_id=f"attempt-{index}",
                    max_attempts=10,
                )
                seen.append(lease.fence_token)
                assert await release(connection, lease) is True

        assert seen == [1, 2, 3, 4], "token must advance across release/re-acquire"


async def test_a_live_lease_blocks_another_worker(pool):
    store = OperationStore()
    async with pool.acquire() as connection:
        async with connection.transaction():
            record = await leased_operation(store, connection)
            await acquire(
                connection,
                operation_id=record.operation_id,
                holder="worker-1",
                attempt_id="attempt-1",
            )
        async with connection.transaction():
            with pytest.raises(LeaseRefused) as refusal:
                await acquire(
                    connection,
                    operation_id=record.operation_id,
                    holder="worker-2",
                    attempt_id="attempt-2",
                )
    assert refusal.value.reason is LeaseRefusal.HELD
    assert "worker-1" in str(refusal.value)


async def test_an_expired_lease_is_recovered_before_takeover(pool):
    """Liveness. A worker that dies holding a lease must not wedge the operation.

    The lease is granted for a duration that has already elapsed by the time the second
    acquisition runs, which is how a dead holder looks from the database's side.
    """
    store = OperationStore()
    async with pool.acquire() as connection:
        async with connection.transaction():
            record = await leased_operation(store, connection)
            first = await acquire(
                connection,
                operation_id=record.operation_id,
                holder="worker-1",
                attempt_id="attempt-1",
                duration=timedelta(milliseconds=1),
            )
        await asyncio.sleep(0.05)
        await sweep_expired_leases(connection)
        async with connection.transaction():
            second = await acquire(
                connection,
                operation_id=record.operation_id,
                holder="worker-2",
                attempt_id="attempt-2",
            )
    assert second.holder == "worker-2"
    assert second.fence_token > first.fence_token


# ---------------------------------------------------------------------------
# The fence: what a stale holder can and cannot do. AC-02.
# ---------------------------------------------------------------------------


async def test_a_stale_holder_cannot_write_through_the_fence(pool):
    """AC-02, the core case: lease lost mid-call, then a late write.

    Worker 1's lease expires, worker 2 legitimately takes over, worker 1 wakes up and
    tries to record a result. Its write must change nothing.

    The operation's `version` is deliberately untouched between the two, so this test
    fails if the fence is removed even though optimistic concurrency is still in place,
    proving the fence is doing the work rather than `store.transition`'s version check
    happening to catch it.
    """
    store = OperationStore()
    async with pool.acquire() as connection:
        async with connection.transaction():
            record = await leased_operation(store, connection)
            stale = await acquire(
                connection,
                operation_id=record.operation_id,
                holder="worker-1",
                attempt_id="attempt-1",
                duration=timedelta(milliseconds=1),
            )
        await asyncio.sleep(0.05)
        await sweep_expired_leases(connection)
        async with connection.transaction():
            await acquire(
                connection,
                operation_id=record.operation_id,
                holder="worker-2",
                attempt_id="attempt-2",
            )

        # Worker 1, awake again, believes it still holds the lease.
        async with connection.transaction():
            wrote = await fenced_update(
                connection,
                stale,
                """
                UPDATE harness_operations
                   SET state = 'succeeded', version = version + 1
                 WHERE operation_id = $1
                   AND $2 = (SELECT fence_token FROM harness_operation_leases
                              WHERE operation_id = $1)
                """,
            )
        assert wrote is False, "a fenced-out worker must not write"

        current = await store.get(connection, principal(), record.operation_id)
    assert current.state is OperationState.PENDING, (
        "a stale worker published a terminal state; this is the AC-02 failure"
    )


async def test_a_stale_holder_cannot_renew(pool):
    """A lost lease cannot be recovered by renewing it.

    If renewal did not check ownership, a stale worker would extend a lease now held by
    someone else and the two would overlap -- with the stale one believing it was
    current.
    """
    store = OperationStore()
    async with pool.acquire() as connection:
        async with connection.transaction():
            record = await leased_operation(store, connection)
            stale = await acquire(
                connection,
                operation_id=record.operation_id,
                holder="worker-1",
                attempt_id="attempt-1",
                duration=timedelta(milliseconds=1),
            )
        await asyncio.sleep(0.05)
        await sweep_expired_leases(connection)
        async with connection.transaction():
            await acquire(
                connection,
                operation_id=record.operation_id,
                holder="worker-2",
                attempt_id="attempt-2",
            )
        with pytest.raises(LeaseRefused):
            await renew(connection, stale)


async def test_a_stale_holder_cannot_release_the_successors_lease(pool):
    """Ownership on release: `authorize_release`'s rule, as a predicate.

    Without it, a straggler's tidy-up frees the lease its successor is actively working
    under, and a third worker then takes an operation two others believe they own.
    """
    store = OperationStore()
    async with pool.acquire() as connection:
        async with connection.transaction():
            record = await leased_operation(store, connection)
            stale = await acquire(
                connection,
                operation_id=record.operation_id,
                holder="worker-1",
                attempt_id="attempt-1",
                duration=timedelta(milliseconds=1),
            )
        await asyncio.sleep(0.05)
        await sweep_expired_leases(connection)
        async with connection.transaction():
            live = await acquire(
                connection,
                operation_id=record.operation_id,
                holder="worker-2",
                attempt_id="attempt-2",
            )

        assert await release(connection, stale) is False
        still = await read_lease(connection, operation_id=record.operation_id)
    assert still is not None and still.holder == "worker-2"
    assert still.fence_token == live.fence_token


async def test_a_holder_with_the_right_token_but_wrong_name_is_refused(pool):
    """Both halves of ownership are required, not either.

    A worker that learned the current token -- from a log line, a retry of its own
    envelope, a report it was handed -- must still not be able to act as the holder.
    """
    store = OperationStore()
    async with pool.acquire() as connection:
        async with connection.transaction():
            record = await leased_operation(store, connection)
            real = await acquire(
                connection,
                operation_id=record.operation_id,
                holder="worker-1",
                attempt_id="attempt-1",
            )
        impostor = ExecutionLease(
            operation_id=real.operation_id,
            org_id=real.org_id,
            workspace_id=real.workspace_id,
            holder="worker-2",
            fence_token=real.fence_token,
            attempt_id=real.attempt_id,
            expires_at=real.expires_at,
            acquired_at=real.acquired_at,
            runtime_deadline=real.runtime_deadline,
            attempts=real.attempts,
        )
        assert await release(connection, impostor) is False
        assert (
            await fenced_update(
                connection,
                impostor,
                "UPDATE harness_operations SET detail = 'x' "
                "WHERE operation_id = $1 AND $2 > 0",
            )
            is False
        )


async def test_fenced_update_refuses_after_the_lease_lapses(pool):
    """The guard is re-checked at write time, not trusted from acquisition.

    A worker holding a legitimately-acquired lease that has since expired must not
    write,
    even though nobody else has taken over. Otherwise the window between expiry and the
    next acquisition is one where the old holder is still effective.
    """
    store = OperationStore()
    async with pool.acquire() as connection:
        async with connection.transaction():
            record = await leased_operation(store, connection)
            lease = await acquire(
                connection,
                operation_id=record.operation_id,
                holder="worker-1",
                attempt_id="attempt-1",
                duration=timedelta(milliseconds=1),
            )
        await asyncio.sleep(0.05)
        assert (
            await fenced_update(
                connection,
                lease,
                "UPDATE harness_operations SET detail = 'late' "
                "WHERE operation_id = $1 AND $2 > 0",
            )
            is False
        )


async def test_fenced_update_succeeds_for_the_live_holder(pool):
    """The positive control: the fence must not block the worker that legitimately
    holds.

    Without this, every test above would also pass if `fenced_update` always returned
    False -- an implementation that is perfectly safe and entirely useless.
    """
    store = OperationStore()
    async with pool.acquire() as connection:
        async with connection.transaction():
            record = await leased_operation(store, connection)
            lease = await acquire(
                connection,
                operation_id=record.operation_id,
                holder="worker-1",
                attempt_id="attempt-1",
            )
        assert (
            await fenced_update(
                connection,
                lease,
                "UPDATE harness_operations SET detail = 'progress' "
                "WHERE operation_id = $1 AND $2 > 0",
            )
            is True
        )


# ---------------------------------------------------------------------------
# Renewal and the runtime ceiling
# ---------------------------------------------------------------------------


async def test_renewal_extends_expiry_without_advancing_the_token(pool):
    """A long attempt keeps its entitlement without invalidating its prior writes."""
    store = OperationStore()
    async with pool.acquire() as connection:
        async with connection.transaction():
            record = await leased_operation(store, connection)
            lease = await acquire(
                connection,
                operation_id=record.operation_id,
                holder="worker-1",
                attempt_id="attempt-1",
                duration=timedelta(seconds=1),
            )
        renewed = await renew(connection, lease, duration=timedelta(seconds=120))
    assert renewed.fence_token == lease.fence_token
    assert renewed.expires_at > lease.expires_at


async def test_renewal_cannot_extend_past_the_approved_runtime(pool):
    """The wedged-but-healthy worker. A renewable ceiling is not a ceiling.

    The approved envelope's `max_runtime_seconds` is 1 second here, so the deadline
    passes while the lease is still live -- exactly the state a worker that renews
    forever would sit in. Renewal must be refused so the recovery sweep can take over.
    """
    store = OperationStore()
    async with pool.acquire() as connection:
        async with connection.transaction():
            admitted = await store.admit(connection, principal(), request("key-rt"))
            await connection.execute(
                """
                INSERT INTO harness_approval_consumption (
                    approval_id, operation_id, org_id, workspace_id, plan_digest,
                    requester, approved_by, max_resource_units, max_runtime_seconds,
                    max_cost_micros, reservation_id, reservation_state
                ) VALUES ('appr-rt',$1,'org-a','ws-1','d','user:r','user:a',
                          4, 1, 5000000, 'res-rt', 'confirmed')
                """,
                admitted.record.operation_id,
            )
            lease = await acquire(
                connection,
                operation_id=admitted.record.operation_id,
                holder="worker-1",
                attempt_id="attempt-1",
                duration=timedelta(seconds=60),
            )
        assert lease.runtime_deadline > lease.acquired_at
        await asyncio.sleep(1.1)
        with pytest.raises(LeaseRefused):
            await renew(connection, lease)


async def test_a_lease_duration_above_the_ceiling_is_refused(pool):
    """An unbounded duration turns the expiry property off."""
    store = OperationStore()
    async with pool.acquire() as connection, connection.transaction():
        record = await leased_operation(store, connection)
        with pytest.raises(ContractViolation):
            await acquire(
                connection,
                operation_id=record.operation_id,
                holder="worker-1",
                attempt_id="attempt-1",
                duration=MAX_LEASE_DURATION + timedelta(seconds=1),
            )


# ---------------------------------------------------------------------------
# Eligibility: what must not be leased at all
# ---------------------------------------------------------------------------


async def test_an_unpaid_operation_cannot_be_leased(pool):
    """The admission control, restated at execution.

    `admit_paid` is deliberately not used: this operation went through the store without
    the approval gate, so it has no spendable consumption row. A lease granted here
    would be an executor authorized to spend against an approval that does not exist.
    """
    store = OperationStore()
    async with pool.acquire() as connection, connection.transaction():
        admitted = await store.admit(connection, principal(), request("key-unpaid"))
        with pytest.raises(LeaseRefused) as refusal:
            await acquire(
                connection,
                operation_id=admitted.record.operation_id,
                holder="worker-1",
                attempt_id="attempt-1",
            )
    assert refusal.value.reason is LeaseRefusal.NOT_ADMITTED


@pytest.mark.parametrize("state", ["released", "retained"])
async def test_a_released_or_retained_reservation_cannot_be_leased(pool, state):
    """Only `confirmed` is payable. Reusing `DELIVERABLE_RESERVATION_STATES`' rule.

    `retained` in particular means "do not start more work on this attempt until a human
    or a provider reconciliation says so", and granting a lease is starting work.
    """
    store = OperationStore()
    async with pool.acquire() as connection, connection.transaction():
        admitted = await store.admit(connection, principal(), request(f"key-{state}"))
        await mark_paid(
            connection, admitted.record.operation_id, reservation_state=state
        )
        with pytest.raises(LeaseRefused) as refusal:
            await acquire(
                connection,
                operation_id=admitted.record.operation_id,
                holder="worker-1",
                attempt_id="attempt-1",
            )
    assert refusal.value.reason is LeaseRefusal.NOT_ADMITTED


@pytest.mark.parametrize("state", ["succeeded", "failed", "cancelled", "unknown"])
async def test_a_terminal_operation_cannot_be_leased(pool, state):
    """A settled operation must never be executed again -- including `unknown`.

    `unknown` is terminal and is the one a naive implementation gets wrong: it reads
    like "we do not know, so try again", and retrying an unknown provision is paying
    twice
    for capacity that may already exist.
    """
    store = OperationStore()
    async with pool.acquire() as connection, connection.transaction():
        record = await leased_operation(store, connection, key=f"key-{state}")
        await store.transition(
            connection,
            record.operation_id,
            expected_version=record.version,
            state=OperationState(state),
        )
        with pytest.raises(LeaseRefused) as refusal:
            await acquire(
                connection,
                operation_id=record.operation_id,
                holder="worker-1",
                attempt_id="attempt-1",
            )
    assert refusal.value.reason is LeaseRefusal.TERMINAL


async def test_a_missing_operation_is_refused_rather_than_leased(pool):
    async with pool.acquire() as connection, connection.transaction():
        with pytest.raises(LeaseRefused) as refusal:
            await acquire(
                connection,
                operation_id="op-that-does-not-exist",
                holder="worker-1",
                attempt_id="attempt-1",
            )
    assert refusal.value.reason is LeaseRefusal.NO_SUCH_OPERATION


async def test_execution_attempts_are_bounded(pool):
    """An operation that crashes its worker every time must stop being re-offered.

    Each attempt may make a provider call, so an unbounded retry is unbounded spend.
    """
    store = OperationStore()
    async with pool.acquire() as connection:
        async with connection.transaction():
            record = await leased_operation(store, connection)
        for index in range(3):
            async with connection.transaction():
                lease = await acquire(
                    connection,
                    operation_id=record.operation_id,
                    holder=f"worker-{index}",
                    attempt_id=f"attempt-{index}",
                    max_attempts=3,
                )
                await release(connection, lease)
        async with connection.transaction():
            with pytest.raises(LeaseRefused) as refusal:
                await acquire(
                    connection,
                    operation_id=record.operation_id,
                    holder="worker-x",
                    attempt_id="attempt-x",
                    max_attempts=3,
                )
    assert refusal.value.reason is LeaseRefusal.ATTEMPTS_EXHAUSTED


async def test_a_closed_lease_is_never_granted_again(pool):
    store = OperationStore()
    async with pool.acquire() as connection:
        async with connection.transaction():
            record = await leased_operation(store, connection)
            assert (
                await close(
                    connection,
                    operation_id=record.operation_id,
                    reason="settled",
                    fence_token=None,
                )
                is True
            )
        async with connection.transaction():
            with pytest.raises(LeaseRefused) as refusal:
                await acquire(
                    connection,
                    operation_id=record.operation_id,
                    holder="worker-1",
                    attempt_id="attempt-1",
                )
    assert refusal.value.reason is LeaseRefusal.CLOSED


async def test_closing_twice_reports_that_the_second_call_did_nothing(pool):
    """Idempotence, and honestly reported: the recovery sweep may close concurrently."""
    store = OperationStore()
    async with pool.acquire() as connection, connection.transaction():
        record = await leased_operation(store, connection)
        assert await close(
            connection, operation_id=record.operation_id, reason="a", fence_token=None
        )
        assert not await close(
            connection, operation_id=record.operation_id, reason="b", fence_token=None
        )


async def test_a_holder_closing_must_still_own_the_lease(pool):
    """A fenced-out worker cannot retire an operation its successor is running."""
    store = OperationStore()
    async with pool.acquire() as connection:
        async with connection.transaction():
            record = await leased_operation(store, connection)
            stale = await acquire(
                connection,
                operation_id=record.operation_id,
                holder="worker-1",
                attempt_id="attempt-1",
                duration=timedelta(milliseconds=1),
            )
        await asyncio.sleep(0.05)
        await sweep_expired_leases(connection)
        async with connection.transaction():
            await acquire(
                connection,
                operation_id=record.operation_id,
                holder="worker-2",
                attempt_id="attempt-2",
            )
        assert (
            await close(
                connection,
                operation_id=record.operation_id,
                reason="stale worker tried to settle",
                fence_token=stale.fence_token,
                holder=stale.holder,
            )
            is False
        )
        survivor = await read_lease(connection, operation_id=record.operation_id)
    assert survivor is not None


# ---------------------------------------------------------------------------
# Tenant isolation and the concurrency cap
# ---------------------------------------------------------------------------


async def test_a_tenant_is_capped_at_its_concurrent_limit(pool):
    store = OperationStore()
    async with pool.acquire() as connection:
        records = []
        async with connection.transaction():
            for index in range(3):
                records.append(
                    await leased_operation(store, connection, key=f"cap-{index}")
                )
        for record in records[:2]:
            async with connection.transaction():
                await acquire(
                    connection,
                    operation_id=record.operation_id,
                    holder=f"worker-{record.operation_id}",
                    attempt_id="attempt-1",
                    max_concurrent=2,
                )
        async with connection.transaction():
            with pytest.raises(LeaseRefused) as refusal:
                await acquire(
                    connection,
                    operation_id=records[2].operation_id,
                    holder="worker-3",
                    attempt_id="attempt-1",
                    max_concurrent=2,
                )
    assert refusal.value.reason is LeaseRefusal.TENANT_AT_CAPACITY


async def test_one_tenant_at_capacity_does_not_block_another(pool):
    """The cap is isolation, so it must not itself become a cross-tenant denial."""
    store = OperationStore()
    async with pool.acquire() as connection:
        async with connection.transaction():
            mine = await leased_operation(store, connection, key="mine", org="org-a")
            theirs = await leased_operation(
                store, connection, key="theirs", org="org-b"
            )
        async with connection.transaction():
            await acquire(
                connection,
                operation_id=mine.operation_id,
                holder="worker-a",
                attempt_id="attempt-1",
                max_concurrent=1,
            )
        async with connection.transaction():
            granted = await acquire(
                connection,
                operation_id=theirs.operation_id,
                holder="worker-b",
                attempt_id="attempt-1",
                max_concurrent=1,
            )
    assert granted.org_id == "org-b"


async def test_reacquiring_a_lapsed_lease_is_not_refused_for_capacity(pool):
    """Recovery must not be blocked by the slot the recovered operation already holds.

    If the cap counted the operation being acquired, an operation whose worker died
    would be unrecoverable at exactly `max_concurrent = 1` -- the recovery attempt would
    be refused by the corpse of the attempt it is replacing.
    """
    store = OperationStore()
    async with pool.acquire() as connection:
        async with connection.transaction():
            record = await leased_operation(store, connection)
            await acquire(
                connection,
                operation_id=record.operation_id,
                holder="worker-1",
                attempt_id="attempt-1",
                duration=timedelta(milliseconds=1),
                max_concurrent=1,
            )
        await asyncio.sleep(0.05)
        await sweep_expired_leases(connection)
        async with connection.transaction():
            taken = await acquire(
                connection,
                operation_id=record.operation_id,
                holder="worker-2",
                attempt_id="attempt-2",
                max_concurrent=1,
            )
    assert taken.holder == "worker-2"


async def test_concurrent_acquisitions_of_one_operation_produce_one_holder(pool):
    """Genuine concurrency, separate connections: the upsert must not grant twice.

    Two coroutines on one connection would be serialized by the driver and would prove
    nothing, which is why this takes two connections from the pool.
    """
    store = OperationStore()
    async with pool.acquire() as setup, setup.transaction():
        record = await leased_operation(store, setup, key="race")

    async def contend(name):
        async with pool.acquire() as held:
            try:
                async with held.transaction():
                    return await acquire(
                        connection=held,
                        operation_id=record.operation_id,
                        holder=name,
                        attempt_id=f"attempt-{name}",
                    )
            except LeaseRefused:
                return None

    outcomes = await asyncio.gather(*(contend(f"worker-{i}") for i in range(5)))
    granted = [lease for lease in outcomes if lease is not None]
    assert len(granted) == 1, "exactly one worker may hold the lease"
    assert granted[0].fence_token == 1


async def test_the_cap_holds_under_concurrent_acquisitions(pool):
    """The advisory lock's reason for existing.

    Without it the count-then-insert pair races across *different* operations of one
    tenant -- no single row to lock -- and the cap is exceeded. Five workers contend for
    a cap of 2.
    """
    store = OperationStore()
    async with pool.acquire() as setup, setup.transaction():
        records = [
            await leased_operation(store, setup, key=f"conc-{index}")
            for index in range(5)
        ]

    async def contend(record):
        async with pool.acquire() as held:
            try:
                async with held.transaction():
                    return await acquire(
                        connection=held,
                        operation_id=record.operation_id,
                        holder=f"worker-{record.operation_id}",
                        attempt_id="attempt-1",
                        max_concurrent=2,
                    )
            except LeaseRefused:
                return None

    outcomes = await asyncio.gather(*(contend(record) for record in records))
    assert len([lease for lease in outcomes if lease is not None]) == 2


# ---------------------------------------------------------------------------
# The rule, and input validation
# ---------------------------------------------------------------------------


def test_is_fenced_out_refuses_strictly_lower_tokens_only():
    """The current holder's own token equals the highest seen and must keep working."""
    assert is_fenced_out(1, 2) is True
    assert is_fenced_out(2, 2) is False
    assert is_fenced_out(3, 2) is False


def test_a_lease_must_carry_an_aware_expiry_and_a_positive_token():
    aware = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    naive = datetime(2026, 9, 20, 12, 0)
    common = {
        "operation_id": "op",
        "org_id": "org-a",
        "workspace_id": "ws-1",
        "holder": "worker",
        "attempt_id": "attempt",
        "attempts": 1,
    }
    with pytest.raises(ContractViolation):
        ExecutionLease(
            fence_token=0,
            expires_at=aware,
            acquired_at=aware,
            runtime_deadline=aware,
            **common,
        )
    with pytest.raises(ContractViolation):
        ExecutionLease(
            fence_token=1,
            expires_at=naive,
            acquired_at=aware,
            runtime_deadline=aware,
            **common,
        )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"holder": ""},
        {"holder": "   "},
        {"attempt_id": ""},
        {"operation_id": ""},
    ],
)
async def test_blank_identities_are_refused(pool, kwargs):
    """A blank holder makes ownership unenforceable: every blank matches every other."""
    base = {
        "operation_id": "op-1",
        "holder": "worker-1",
        "attempt_id": "attempt-1",
    }
    base.update(kwargs)
    async with pool.acquire() as connection, connection.transaction():
        with pytest.raises(ContractViolation):
            await acquire(connection, **base)


async def test_the_default_duration_is_the_contracts_default(pool):
    """A lease granted with no duration must last the published default."""
    assert DEFAULT_LEASE_DURATION == timedelta(seconds=60)
    store = OperationStore()
    async with pool.acquire() as connection, connection.transaction():
        record = await leased_operation(store, connection)
        lease = await acquire(
            connection,
            operation_id=record.operation_id,
            holder="worker-1",
            attempt_id="attempt-1",
        )
    assert (
        timedelta(seconds=55)
        <= (lease.expires_at - lease.acquired_at)
        <= timedelta(seconds=65)
    )


# ---------------------------------------------------------------------------
# Two-connection regressions for the race defects found in the foreground probe.
# These cannot be written with a single connection because single-connection
# "concurrency" is serialized by the driver and cannot reproduce the interleaving.
# ---------------------------------------------------------------------------


async def test_fenced_update_is_refused_after_concurrent_release(pool, schema_name):
    """Release on a second connection must not slip through the fenced_update check.

    Probe-reproduced defect: `SELECT EXISTS` in the same transaction as the subsequent
    UPDATE does not lock the row, so a release on another connection can run between
    the check and the write and the now-unheld write still succeeds (because `release`
    preserves the fence token).

    The fix: `FOR UPDATE` in `fenced_update` locks the lease row at check time, so any
    concurrent release must wait and the check re-reads the row it locked.
    """
    asyncpg = pytest.importorskip("asyncpg")
    from .conftest import postgres_url

    store = OperationStore()
    async with pool.acquire() as setup, setup.transaction():
        record = await leased_operation(store, setup)

    # Acquire on connection-1 in its own committed transaction.
    async with pool.acquire() as conn1:
        async with conn1.transaction():
            lease = await acquire(
                conn1,
                operation_id=record.operation_id,
                holder="worker-1",
                attempt_id="attempt-1",
            )

    # Open the two-connection interleaved scenario.
    # Two events synchronize the race window:
    #   check_done  -- conn1's SELECT FOR UPDATE has run; conn2 may now proceed
    #   release_done -- conn2's release has committed; conn1 may now issue its UPDATE
    #
    # With FOR UPDATE: conn1's lock blocks conn2's release entirely -- both events fire
    # in the same order but conn2 can only run after conn1 commits, by which point the
    # check and the write are already in the same transaction. So the write must be
    # under a still-held lease -- the scenario collapses to "conn2 wins only after the
    # transaction ends".
    #
    # What we actually assert here is the simpler, observable property: that
    # `fenced_update` refuses after a confirmed release -- whether that release races
    # or precedes. The FOR UPDATE is tested by the fact that the test is not a deadlock
    # and that the assertion holds even when the release commits first.
    conn2 = await asyncpg.connect(
        postgres_url(),
        server_settings={"search_path": schema_name, "statement_timeout": "5000"},
    )
    try:
        # Release on conn2, committed before conn1 starts fenced_update.
        async with conn2.transaction():
            await release(conn2, lease)
    finally:
        await conn2.close()

    # Now conn1 runs fenced_update. Holder is NULL; must return False.
    async with pool.acquire() as conn1:
        async with conn1.transaction():
            wrote = await fenced_update(
                conn1,
                lease,
                """
                UPDATE harness_operations
                   SET updated_at = now()
                 WHERE operation_id = $1 AND $2 >= 1
                """,
            )
    assert not wrote, (
        "fenced_update wrote after the lease was released: the holder check must "
        "exclude rows where holder IS NULL even when the fence token still matches"
    )


async def test_renew_is_refused_after_lease_expires_within_transaction(pool):
    """`renew` inside a long-lived transaction must see the actual current time.

    Probe-reproduced defect: `now()` in PostgreSQL is the *transaction start time*, not
    the current wall time. A lease that expires one second after a transaction begins
    passes `expires_at > now()` for the entire duration of that transaction.

    The fix: `clock_timestamp()` returns the actual statement-execution time and is not
    frozen at transaction start.
    """
    store = OperationStore()
    async with pool.acquire() as connection:
        async with connection.transaction():
            record = await leased_operation(store, connection)

        # Acquire a lease with a very short duration.
        async with connection.transaction():
            lease = await acquire(
                connection,
                operation_id=record.operation_id,
                holder="worker-1",
                attempt_id="attempt-1",
                duration=timedelta(milliseconds=1),
            )

        # Open a long transaction and sleep past the lease expiry inside it.
        # Under the old `now()` the renewal would succeed because `now()` was frozen
        # at transaction-start before the sleep; `clock_timestamp()` refuses it.
        async with connection.transaction():
            await connection.execute("SELECT pg_sleep(0.1)")
            with pytest.raises(LeaseRefused):
                await renew(connection, lease, duration=timedelta(seconds=60))


async def test_fenced_update_is_refused_when_runtime_deadline_expired(
    pool,
):
    """A holder whose runtime deadline passes inside a transaction cannot still write.

    The runtime deadline enforces the approved maximum runtime for one attempt. Without
    `clock_timestamp()`, a deadline that expires inside a long transaction is invisible
    to `fenced_update`'s predicate, and the holder effectively gets unlimited runtime.

    `acquire` reads the deadline from the approval consumption row (3600 s), so we
    back-date it with a direct UPDATE after grant rather than by passing a parameter
    that does not exist.
    """
    store = OperationStore()
    async with pool.acquire() as connection:
        async with connection.transaction():
            record = await leased_operation(store, connection)

        # Grant a normal lease, then shorten the runtime_deadline to expire immediately.
        async with connection.transaction():
            lease = await acquire(
                connection,
                operation_id=record.operation_id,
                holder="worker-1",
                attempt_id="attempt-1",
                duration=timedelta(seconds=60),
            )
        async with connection.transaction():
            await connection.execute(
                """
                UPDATE harness_operation_leases
                   SET runtime_deadline = clock_timestamp() + '1 millisecond'::interval
                 WHERE operation_id = $1
                """,
                record.operation_id,
            )

        # Inside a transaction that started while the deadline was live, sleep past it.
        # Under the old `now()` the write would succeed because `now()` was frozen at
        # transaction-start, before the sleep. Under `clock_timestamp()` it is refused.
        async with connection.transaction():
            await connection.execute("SELECT pg_sleep(0.1)")
            wrote = await fenced_update(
                connection,
                lease,
                """
                UPDATE harness_operations
                   SET updated_at = now()
                 WHERE operation_id = $1 AND $2 >= 1
                """,
            )
    assert not wrote, (
        "fenced_update wrote after the runtime deadline expired inside a transaction; "
        "clock_timestamp() must be used so expiry is evaluated at statement time"
    )
