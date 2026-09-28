"""Real-database behaviour of the dispatch outbox.

Issue #5525 (w6-02), EPIC #4910, Wave 6. AC-01 (outbox replay, restart) and design
requirement 3 (resumable, duplicate-safe, no secrets to workers).
"""

from __future__ import annotations

import asyncio

import pytest

from harness_jobs import (
    DispatchEnvelope,
    DispatchOutbox,
    OperationState,
    OperationStore,
)

from .conftest import admit_paid, requires_postgres
from .test_store_postgres import principal, request

pytestmark = requires_postgres


class RecordingExecutor:
    """Delivers successfully and remembers what it was handed."""

    def __init__(self) -> None:
        self.seen: list[DispatchEnvelope] = []

    async def deliver(self, envelope: DispatchEnvelope) -> bool:
        self.seen.append(envelope)
        return True


class FailingExecutor:
    """Never delivers. Optionally by raising, to cover both failure shapes."""

    def __init__(self, *, raising: bool = False) -> None:
        self.calls = 0
        self._raising = raising

    async def deliver(self, envelope: DispatchEnvelope) -> bool:
        self.calls += 1
        if self._raising:
            raise RuntimeError("transport exploded")
        return False


class CrashingExecutor:
    """Delivers durably, then the process 'dies' before the row is settled.

    Models the delete-last gap: the handoff happened, the marking did not.
    """

    def __init__(self) -> None:
        self.delivered: list[str] = []

    async def deliver(self, envelope: DispatchEnvelope) -> bool:
        self.delivered.append(envelope.operation_id)
        raise _CrashAfterDelivery(envelope.operation_id)


class _CrashAfterDelivery(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Happy path and the envelope's contents
# ---------------------------------------------------------------------------


async def test_admitted_operation_is_claimable_and_delivered_once(connection):
    """An admitted operation is dispatched exactly once and then not again."""
    store = OperationStore()
    outbox = DispatchOutbox(store=store)
    admitted = await admit_paid(store, connection, principal(), request())
    executor = RecordingExecutor()

    first = await outbox.drain_once(connection, executor)
    second = await outbox.drain_once(connection, executor)

    assert first.delivered == 1
    assert second.handled == 0, "a delivered row was claimed a second time"
    assert [e.operation_id for e in executor.seen] == [admitted.record.operation_id]
    assert await outbox.pending_count(connection) == 0


async def test_registration_filter_never_consumes_undeliverable_attempts(connection):
    admitted = await admit_paid(OperationStore(), connection, principal(), request())
    outbox, executor = DispatchOutbox(), RecordingExecutor()
    for selection in ((), ("other-operation",)):
        result = await outbox.drain_once(connection, executor, operation_ids=selection)
        assert result.handled == 0
    assert (
        await connection.fetchval("SELECT attempts FROM harness_dispatch_outbox") == 0
    )
    result = await outbox.drain_once(
        connection, executor, operation_ids=(admitted.record.operation_id,)
    )
    assert result.delivered == 1
    assert len(executor.seen) == 1


async def test_the_envelope_carries_no_connection_or_credential(connection):
    """Design requirement 3: workers see a description, not a database.

    Asserted structurally on the dataclass rather than by inspecting one instance, so
    adding a `dsn` or `secret` field to the envelope fails here instead of in review.
    """
    store = OperationStore()
    outbox = DispatchOutbox(store=store)
    await admit_paid(store, connection, principal(), request())
    executor = RecordingExecutor()
    await outbox.drain_once(connection, executor)

    envelope = executor.seen[0]
    assert set(vars(envelope)) == {
        "outbox_id",
        "operation_id",
        "org_id",
        "workspace_id",
        "action",
        "attempts",
        # Added with the F2/F3/F6 repairs. Each is inert data a worker needs and
        # none is a handle: `job_id`/`attempt_id` are the published budget key,
        # `request_payload` is the serialised request so a recovered worker can
        # perform it, and `claim_generation` is the fence its settlement must
        # present. The allowlist is extended deliberately rather than relaxed --
        # the point of asserting equality is that a future `dsn` still fails here.
        "job_id",
        "attempt_id",
        "request_payload",
        "claim_generation",
    }
    for value in vars(envelope).values():
        assert isinstance(value, str | int), (
            "an envelope field holds a live object; a worker must receive only data"
        )
    with pytest.raises(Exception):
        envelope.operation_id = "rewritten"  # type: ignore[misc]


async def test_delivery_moves_the_operation_off_pending(connection):
    """A caller polling right after dispatch does not see PENDING forever."""
    store = OperationStore()
    outbox = DispatchOutbox(store=store)
    admitted = await admit_paid(store, connection, principal(), request())

    await outbox.drain_once(connection, RecordingExecutor())

    current = await store.get(connection, principal(), admitted.record.operation_id)
    assert current is not None
    assert current.state is OperationState.RUNNING


# ---------------------------------------------------------------------------
# Replay and resumability
# ---------------------------------------------------------------------------


async def test_a_crash_after_delivery_replays_rather_than_loses(connection):
    """AC-01, outbox replay: the delete-last trade, demonstrated.

    The executor delivers and then the settling step never runs. The row must still be
    pending, so the next pass delivers it again. At-least-once by construction: a
    duplicate, which the store's uniqueness constraint absorbs, instead of a loss,
    which nothing can.
    """
    store = OperationStore()
    outbox = DispatchOutbox(store=store)
    admitted = await admit_paid(store, connection, principal(), request())
    crashing = CrashingExecutor()

    await outbox.drain_once(connection, crashing)

    # Not marked delivered, because the marking is last and never happened.
    assert await outbox.pending_count(connection) == 1

    # A restarted worker finds it and delivers it again.
    recovered = RecordingExecutor()
    report = await outbox.drain_once(connection, recovered)

    assert report.delivered == 1
    assert crashing.delivered == [admitted.record.operation_id]
    assert [e.operation_id for e in recovered.seen] == [admitted.record.operation_id]
    assert await outbox.pending_count(connection) == 0


async def test_pending_work_is_recoverable_by_a_new_pool(pool, schema_name):
    """AC-01, restart: undelivered rows are found by a process that never saw them.

    The original pool is closed entirely; a fresh one drains the table. Nothing carried
    over in memory -- the queue is the table.
    """
    asyncpg = pytest.importorskip("asyncpg")
    from .conftest import postgres_url

    store = OperationStore()
    outbox = DispatchOutbox(store=store)
    async with pool.acquire() as connection:
        for index in range(3):
            await admit_paid(store, connection, principal(), request(f"k-{index}"))
    await pool.close()

    fresh = await asyncpg.create_pool(
        postgres_url(),
        min_size=1,
        max_size=2,
        server_settings={"search_path": schema_name},
    )
    try:
        executor = RecordingExecutor()
        async with fresh.acquire() as connection:
            report = await outbox.drain(connection, executor)
            remaining = await outbox.pending_count(connection)
    finally:
        await fresh.close()

    assert report.delivered == 3
    assert remaining == 0


async def test_an_expired_claim_becomes_claimable_again(connection):
    """A worker that died holding a claim does not strand its row.

    The claim is expired by hand -- the alternative is sleeping out a real lease, which
    makes a test slow without making it stronger.
    """
    store = OperationStore()
    outbox = DispatchOutbox(store=store, claim_seconds=3600)
    await admit_paid(store, connection, principal(), request())

    claimed = await outbox.claim(connection)
    assert len(claimed) == 1
    # Still held: a second worker sees nothing.
    assert await outbox.claim(connection) == ()

    await connection.execute(
        "UPDATE harness_dispatch_outbox SET claimed_until = now() - interval '1 second'"
    )

    reclaimed = await outbox.claim(connection)
    assert len(reclaimed) == 1, "an abandoned claim was never released"
    assert reclaimed[0].attempts == 2, "the reclaim did not record a second attempt"


async def test_concurrent_workers_claim_disjoint_rows(pool):
    """`SKIP LOCKED`: a fleet divides the work instead of queueing on it.

    Each worker is on its own connection. If the claim used plain `FOR UPDATE`, the
    second worker would block rather than skip; if it used no lock at all, both would
    claim the same rows and deliver everything twice.
    """
    store = OperationStore()
    outbox = DispatchOutbox(store=store, claim_seconds=3600)
    async with pool.acquire() as connection:
        for index in range(20):
            await admit_paid(store, connection, principal(), request(f"k-{index}"))

    async def worker():
        async with pool.acquire() as connection:
            return await outbox.claim(connection, limit=10)

    batches = await asyncio.gather(*(worker() for _ in range(4)))
    claimed = [envelope.operation_id for batch in batches for envelope in batch]

    assert len(claimed) == len(set(claimed)), "two workers claimed the same row"
    assert len(claimed) == 20


# ---------------------------------------------------------------------------
# Failure handling
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raising", [False, True])
async def test_a_failed_delivery_stays_pending_and_records_why(connection, raising):
    """Both failure shapes -- returning False and raising -- leave the row retryable."""
    store = OperationStore()
    outbox = DispatchOutbox(store=store)
    await admit_paid(store, connection, principal(), request())

    report = await outbox.drain_once(connection, FailingExecutor(raising=raising))

    assert report.failed == 1
    assert report.delivered == 0
    row = await connection.fetchrow(
        "SELECT attempts, claimed_until, last_error, delivered_at"
        "  FROM harness_dispatch_outbox"
    )
    assert row["delivered_at"] is None
    assert row["attempts"] == 1
    assert row["claimed_until"] is None, "the claim was not released after a failure"
    assert row["last_error"], "no reason was recorded for the failure"


async def test_attempts_are_capped_and_the_outcome_is_unknown_not_failed(connection):
    """The cap stops the retry loop; the operation is UNKNOWN, never FAILED.

    Undeliverability is not evidence about what a provider did. Recording FAILED here
    is how a caller retries a provision that may already be running -- duplicate
    spend -- or releases a reservation for resources that exist.
    """
    store = OperationStore()
    outbox = DispatchOutbox(store=store, max_attempts=3)
    admitted = await admit_paid(store, connection, principal(), request())
    executor = FailingExecutor()

    totals = await outbox.drain(connection, executor, max_batches=10)

    assert executor.calls == 3, "the attempt cap was not enforced"
    assert totals.exhausted == 1

    current = await store.get(connection, principal(), admitted.record.operation_id)
    assert current is not None
    assert current.state is OperationState.UNKNOWN
    assert current.state is not OperationState.FAILED

    # The row is kept, undelivered, with its reason -- an operator has to be able to
    # see that accepted work was never delivered.
    row = await connection.fetchrow(
        "SELECT delivered_at, last_error FROM harness_dispatch_outbox"
    )
    assert row is not None, "the exhausted row was deleted"
    assert row["delivered_at"] is None
    assert row["last_error"]
    assert await outbox.pending_count(connection) == 1, (
        "pending_count hid an exhausted row; a queue that looks empty while holding "
        "unfinished obligations is worse than a visible backlog"
    )


async def test_exhaustion_does_not_overwrite_a_real_conclusion(connection):
    """If something already knows the outcome, UNKNOWN must not clobber it."""
    store = OperationStore()
    outbox = DispatchOutbox(store=store, max_attempts=1)
    admitted = await admit_paid(store, connection, principal(), request())
    await store.transition(
        connection,
        admitted.record.operation_id,
        expected_version=admitted.record.version,
        state=OperationState.SUCCEEDED,
        detail="provider confirmed",
    )

    await outbox.drain(connection, FailingExecutor(), max_batches=3)

    current = await store.get(connection, principal(), admitted.record.operation_id)
    assert current is not None
    assert current.state is OperationState.SUCCEEDED
    assert current.detail == "provider confirmed"


# ---------------------------------------------------------------------------
# Claim ownership (F6) and abandoned final claims (F5)
# ---------------------------------------------------------------------------


async def _expire_claims(connection):
    """Push every live lease into the past, making the rows claimable again."""
    await connection.execute(
        "UPDATE harness_dispatch_outbox SET claimed_until = now() - interval '1 hour'"
        " WHERE delivered_at IS NULL"
    )


@pytest.mark.parametrize("settle", ["delivered", "failure", "exhaustion"])
async def test_an_expired_claim_cannot_settle_its_successors_row(connection, settle):
    """F6. A stale worker's verdict must not land on the claim that replaced it.

    The scenario: worker A claims a row and stalls past its lease. The lease expires,
    worker B claims the same row -- which is the recovery the lease is *for* -- and
    starts delivering. Then A wakes up and settles.

    Settlements carried no reference to which claim was speaking, so A's verdict
    applied to B's row: a delivery A never completed marked delivered, or an
    operation B was actively delivering concluded UNKNOWN. Parametrized over all
    three settlement paths because one guarded path and two unguarded ones is the
    same bug with a smaller blast radius.

    The fence is a monotonic `claim_generation` on the row: every claim increments it
    and every settlement must present the value it was handed. Two processes' clocks
    can disagree about whose lease is live; a counter on the row cannot.
    """
    store = OperationStore()
    # Attempt headroom so the reclaim is possible at all: at the cap the row is no
    # longer claimable, and a test where B never gets the row proves nothing about
    # whether A's settlement would have hit it.
    outbox = DispatchOutbox(store=store, max_attempts=5)
    admitted = await admit_paid(store, connection, principal(), request())

    stale = (await outbox.claim(connection))[0]
    await _expire_claims(connection)
    fresh = (await outbox.claim(connection))[0]

    assert fresh.outbox_id == stale.outbox_id, "the test did not reclaim the same row"
    assert fresh.claim_generation > stale.claim_generation, (
        "reclaiming did not advance the fence, so no settlement can be attributed"
    )

    if settle == "delivered":
        landed = await outbox._mark_delivered(connection, stale)
    elif settle == "failure":
        landed = await outbox._record_failure(connection, stale, "A woke up late")
    else:
        landed = await outbox._mark_undeliverable(connection, stale, detail="A gave up")

    assert landed is False, (
        "a superseded claim reported that it had settled the row; the caller would "
        "count it as handled and stop"
    )

    row = await connection.fetchrow(
        "SELECT delivered_at, abandoned_at, last_error FROM harness_dispatch_outbox"
    )
    assert row["delivered_at"] is None, "a stale claim marked a row delivered"
    assert row["abandoned_at"] is None, "a stale claim abandoned an active row"
    assert row["last_error"] is None, "a stale claim wrote over the active claim's row"

    # The operation is untouched, which is the consequence that actually matters:
    # B is still delivering, and a premature UNKNOWN here is what invites a consumer
    # to release a budget reservation or re-provision resources that exist.
    current = await store.get(connection, principal(), admitted.record.operation_id)
    assert current is not None
    assert current.state is OperationState.PENDING

    # And B, holding the current generation, still settles normally. A fence that
    # blocked everyone would pass every assertion above and deliver nothing.
    assert await outbox._mark_delivered(connection, fresh) is True


async def test_a_crashed_final_claim_reaches_a_durable_unknown(connection):
    """F5. A worker killed holding the last attempt must not leave a limbo row.

    The gap: `claim()` increments `attempts` *before* delivery, so a worker that dies
    mid-delivery on the final attempt leaves a row at the cap with its lease expiring
    and no settlement. The claim query will not hand it out again -- attempts are at
    the cap -- and nothing else was looking at it. The operation stays PENDING
    forever: a caller polls an accepted operation that no process will ever conclude,
    which is the "accepted but never concluded" state AC-01 exists to exclude.

    `recover_abandoned` is that second look. UNKNOWN rather than FAILED for the same
    reason exhaustion is: nothing here is evidence about what the provider did.

    It settles the claim rather than making the row retryable, and the rejected
    alternative is instructive -- counting only *completed* failures would make a row
    whose delivery kills its worker every time immortal at the head of the queue.
    """
    store = OperationStore()
    outbox = DispatchOutbox(store=store, max_attempts=1)
    admitted = await admit_paid(store, connection, principal(), request())

    # A process killed mid-delivery, modelled exactly: `claim()` commits the attempt
    # increment and the lease, and then nothing else runs. Not via `drain_once` with a
    # raising executor -- that would *catch* the failure and settle the row, which is
    # the case already covered above. The defect is specifically the code after the
    # claim never executing at all, which no in-process exception can reproduce.
    claimed = await outbox.claim(connection)
    assert len(claimed) == 1

    # Precondition: the row is at the cap, unsettled, and its lease has lapsed.
    await _expire_claims(connection)
    row = await connection.fetchrow(
        "SELECT attempts, delivered_at, abandoned_at FROM harness_dispatch_outbox"
    )
    assert row["attempts"] == 1
    assert row["delivered_at"] is None
    assert row["abandoned_at"] is None

    # Nothing routine picks it up: this is the limbo, demonstrated rather than asserted.
    assert (await outbox.drain_once(connection, RecordingExecutor())).handled == 0
    stuck = await store.get(connection, principal(), admitted.record.operation_id)
    assert stuck is not None and stuck.state is OperationState.PENDING

    recovered = await outbox.recover_abandoned(connection)

    assert recovered == 1
    current = await store.get(connection, principal(), admitted.record.operation_id)
    assert current is not None
    assert current.state is OperationState.UNKNOWN
    assert current.state is not OperationState.FAILED
    assert current.detail, "an UNKNOWN with no explanation is not actionable"

    # Idempotent, and the row is stamped so it is not recovered twice -- a second
    # sweep must not re-announce work it already concluded.
    assert await outbox.recover_abandoned(connection) == 0
    assert (
        await connection.fetchval("SELECT abandoned_at FROM harness_dispatch_outbox")
        is not None
    )


async def test_recovery_leaves_a_live_claim_alone(connection):
    """F5's boundary: recovery must not conclude work that is still in flight.

    A sweep that settled any unfinished row would be indistinguishable from the bug
    it fixes -- concluding an operation a worker is actively delivering. Only an
    *expired* final claim qualifies.
    """
    store = OperationStore()
    outbox = DispatchOutbox(store=store, max_attempts=1)
    admitted = await admit_paid(store, connection, principal(), request())

    await outbox.claim(connection)  # live lease, not expired

    assert await outbox.recover_abandoned(connection) == 0
    current = await store.get(connection, principal(), admitted.record.operation_id)
    assert current is not None
    assert current.state is OperationState.PENDING


# ---------------------------------------------------------------------------
# F7: the settlement is one fact, and an existing split commit is repairable
# ---------------------------------------------------------------------------
#
# `_mark_undeliverable` writes two things -- the row is given up on, and the operation's
# outcome is UNKNOWN -- and they used to be two commits with a reachable gap between
# them. The first commit alone removes the row from *both* recovery predicates at once
# (`claim` skips abandoned rows; `recover_abandoned` only looked for unabandoned ones),
# so an interruption in the gap left an operation PENDING with nothing anywhere still
# looking for it.
#
# What makes these tests different from `test_a_crashed_final_claim_reaches_a_durable_
# unknown` above: that one kills a worker *before* recovery begins, which the sweep
# already handled. These interrupt *inside* the settlement, at each of its three
# internal boundaries, which is where the gap actually was.


class _Interrupted(RuntimeError):
    """A process ending mid-settlement. Not a delivery failure -- a disappearance."""


class _FailsAfterAbandoning(DispatchOutbox):
    """Interrupts immediately after the abandonment write, before the operation is read.

    Overrides the one method every guarded settlement UPDATE goes through, so the write
    really happens and then the process stops. This is boundary one: the state that used
    to be committed on its own.
    """

    async def _owned_update(self, connection, envelope, statement, *extra):
        result = await super()._owned_update(connection, envelope, statement, *extra)
        raise _Interrupted("stopped after the abandonment write")
        return result  # pragma: no cover - unreachable, kept so the intent is explicit


class _StoreThatFailsBeforeWriting(OperationStore):
    """Interrupts after the operation has been read and before its outcome is written.

    Boundary two. The read has happened, so a settlement that trusted "I have the
    version I need" would be past the point of no return here.
    """

    async def transition(self, *args, **kwargs):
        raise _Interrupted("stopped between the read and the transition")


class _StoreThatFailsAfterWriting(OperationStore):
    """Interrupts after the outcome is written, before the caller learns it landed.

    Boundary three, and the one where the transaction does the most work: a real UPDATE
    has already been issued against `harness_operations`, and the caller that would have
    reported it settled never returns. Under two commits both halves stay written and
    nobody knows, which happens to be a consistent outcome and is consistent only by
    luck -- nothing ordered them and nothing checked. Under one transaction the rollback
    is total, and the next sweep re-derives the same answer rather than inheriting one
    it cannot attribute.
    """

    async def transition(self, *args, **kwargs):
        record = await super().transition(*args, **kwargs)
        raise _Interrupted("stopped after the transition was written")
        return record  # pragma: no cover - unreachable, kept so the intent is explicit


async def _row(connection):
    return await connection.fetchrow(
        "SELECT attempts, delivered_at, abandoned_at, claimed_until, claim_generation"
        "  FROM harness_dispatch_outbox"
    )


async def _strand_a_final_claim(connection, outbox):
    """Bring a row to the state the sweep looks for: final claim taken, lease lapsed."""
    claimed = await outbox.claim(connection)
    assert len(claimed) == 1
    await _expire_claims(connection)


@pytest.mark.parametrize(
    "interrupt_at",
    ["after-the-abandonment-write", "before-the-transition", "after-the-transition"],
)
async def test_an_interruption_inside_the_settlement_leaves_nothing_stranded(
    connection, interrupt_at
):
    """F7. Every boundary inside the settlement is recoverable, by the same database.

    The settlement is one transaction, so an interruption anywhere inside it rolls back
    the abandonment -- which returns the row to precisely the condition the sweep
    selects on, and the next ordinary sweep finishes the job. No operator, no manual
    UPDATE, no second mechanism.

    The recovery is deliberately performed by *fresh, uninstrumented* objects: a store
    and an outbox with no interruption wired into them, which is what a restarted
    process has. Recovering with the instrumented objects would only show that the same
    call works when it is not interrupted.
    """
    store = OperationStore()
    outbox = DispatchOutbox(store=store, max_attempts=1)
    admitted = await admit_paid(store, connection, principal(), request())
    await _strand_a_final_claim(connection, outbox)

    before = await _row(connection)

    if interrupt_at == "after-the-abandonment-write":
        broken = _FailsAfterAbandoning(store=store, max_attempts=1)
    elif interrupt_at == "before-the-transition":
        broken = DispatchOutbox(store=_StoreThatFailsBeforeWriting(), max_attempts=1)
    else:
        broken = DispatchOutbox(store=_StoreThatFailsAfterWriting(), max_attempts=1)

    with pytest.raises(_Interrupted):
        await broken.recover_abandoned(connection)

    # Nothing half-written survived. `claim_generation` is expected to have moved -- the
    # sweep takes a claim before settling, and that claim is not part of the settlement
    # -- so it is compared separately from the state that must be unchanged.
    after = await _row(connection)
    assert after["abandoned_at"] is None, (
        "the row is marked given-up-on after an interrupted settlement; it is now "
        "invisible to every sweep, and the only way back is an operator editing the "
        "table"
    )
    assert after["delivered_at"] is None
    assert after["claimed_until"] == before["claimed_until"], (
        "the lease was released by a settlement that did not complete"
    )
    assert after["claim_generation"] > before["claim_generation"], (
        "the sweep did not take a claim, so nothing fenced the settlement it attempted"
    )
    mid = await store.get(connection, principal(), admitted.record.operation_id)
    assert mid is not None
    assert mid.state is OperationState.PENDING, (
        "an outcome was recorded by a settlement that did not complete"
    )

    # A restarted process, with nothing instrumented, finishes it.
    restarted = DispatchOutbox(store=OperationStore(), max_attempts=1)
    assert await restarted.recover_abandoned(connection) == 1

    current = await store.get(connection, principal(), admitted.record.operation_id)
    assert current is not None
    assert current.state is OperationState.UNKNOWN
    assert current.state is not OperationState.FAILED
    assert current.detail, "an UNKNOWN with no explanation is not actionable"
    assert (await _row(connection))["abandoned_at"] is not None

    # And it stays settled: the repair is not a loop that re-concludes its own work.
    assert await restarted.recover_abandoned(connection) == 0


async def test_an_interrupted_exhaustion_during_a_drain_is_also_recovered(connection):
    """The same boundary, reached through `drain_once` rather than through the sweep.

    Exhaustion has two callers, and a transaction that only protected one of them
    would be a fix for the test rather than for the defect. Here the attempt cap is
    reached inside a normal drain and the settlement is interrupted; the row must still
    come back.

    The lease is expired by hand afterwards because the rollback correctly restores the
    live claim this worker was holding: until that lease lapses the interrupted worker
    cannot be assumed dead, and a sweep that concluded its operation anyway would be the
    stale-verdict defect the generation fence exists to prevent.
    """
    admitted_store = OperationStore()
    outbox = DispatchOutbox(store=_StoreThatFailsBeforeWriting(), max_attempts=1)
    admitted = await admit_paid(admitted_store, connection, principal(), request())

    with pytest.raises(_Interrupted):
        await outbox.drain_once(connection, FailingExecutor())

    row = await _row(connection)
    assert row["abandoned_at"] is None
    assert row["claimed_until"] is not None, (
        "the interrupted worker's lease was released, so a successor may act while it "
        "may still be alive"
    )

    await _expire_claims(connection)
    restarted = DispatchOutbox(store=OperationStore(), max_attempts=1)
    assert await restarted.recover_abandoned(connection) == 1

    current = await admitted_store.get(
        connection, principal(), admitted.record.operation_id
    )
    assert current is not None
    assert current.state is OperationState.UNKNOWN


async def test_a_row_stranded_by_the_earlier_two_commit_settlement_is_repaired(
    connection,
):
    """F7's other half: state that already exists must be repairable in place.

    The transaction stops this state from being *produced*. It does nothing about rows a
    process running the earlier build already left behind -- given up on, with their
    operation never concluded -- and those rows are invisible to `claim` and were
    invisible to the sweep too. A fix that requires an operator to find and repair them
    by hand is not a fix for a durable store.

    The stranded state is produced by executing the earlier build's first statement
    verbatim and then stopping, which is what that build did. Written out here rather
    than described, because the current code cannot reach this state and a test that
    could not construct it could not show the repair either.
    """
    store = OperationStore()
    outbox = DispatchOutbox(store=store, max_attempts=1)
    admitted = await admit_paid(store, connection, principal(), request())
    await _strand_a_final_claim(connection, outbox)

    # The earlier build's statement, as it stood, followed by nothing.
    outbox_id, generation = await connection.fetchrow(
        "SELECT id, claim_generation FROM harness_dispatch_outbox"
    )
    await connection.execute(
        """
        UPDATE harness_dispatch_outbox
           SET claimed_until = NULL, abandoned_at = now()
         WHERE id = $1 AND claim_generation = $2
           AND delivered_at IS NULL AND abandoned_at IS NULL
        """,
        outbox_id,
        generation,
    )

    stranded = await _row(connection)
    assert stranded["abandoned_at"] is not None, "the split commit was not reproduced"
    assert stranded["claimed_until"] is None
    stuck = await store.get(connection, principal(), admitted.record.operation_id)
    assert stuck is not None and stuck.state is OperationState.PENDING

    # The demonstration of the harm: no routine path sees this row at all.
    assert (await outbox.drain_once(connection, RecordingExecutor())).handled == 0

    assert await outbox.recover_abandoned(connection) == 1, (
        "a row given up on whose operation was never concluded was not repaired; the "
        "operation stays PENDING for as long as the table does"
    )

    current = await store.get(connection, principal(), admitted.record.operation_id)
    assert current is not None
    assert current.state is OperationState.UNKNOWN
    assert current.detail, "an UNKNOWN with no explanation is not actionable"

    # The first stamp is kept: when the row was given up on is evidence, and the pass
    # that finished the settlement did not witness it.
    assert (await _row(connection))["abandoned_at"] == stranded["abandoned_at"]
    assert await outbox.recover_abandoned(connection) == 0


async def test_the_repair_branch_leaves_correctly_settled_rows_alone(connection):
    """The repair must not re-conclude work that a real channel already concluded.

    A sweep that swept every abandoned row would overwrite a provider's own verdict
    with UNKNOWN -- the information-destroying move this module refuses everywhere else
    -- and would do it on every pass, forever. The branch is keyed on the *operation*
    being unconcluded, not on the row being abandoned.
    """
    store = OperationStore()
    outbox = DispatchOutbox(store=store, max_attempts=1)
    admitted = await admit_paid(store, connection, principal(), request())
    await store.transition(
        connection,
        admitted.record.operation_id,
        expected_version=admitted.record.version,
        state=OperationState.SUCCEEDED,
        detail="provider confirmed",
    )

    # Exhausted, so the row is abandoned, while the operation carries a real conclusion.
    await outbox.drain(connection, FailingExecutor(), max_batches=3)
    assert (await _row(connection))["abandoned_at"] is not None

    for _ in range(3):
        assert await outbox.recover_abandoned(connection) == 0

    current = await store.get(connection, principal(), admitted.record.operation_id)
    assert current is not None
    assert current.state is OperationState.SUCCEEDED
    assert current.detail == "provider confirmed"
    assert current.version == admitted.record.version + 1, (
        "the repair branch bumped a settled operation's version, so every pass "
        "rewrites a row it has nothing new to say about"
    )


async def test_the_repair_branch_ignores_a_delivered_row(connection):
    """Delivered work is not stranded work, whatever its operation's state says.

    Worth asserting separately because the branch's condition is about the operation,
    and a delivered row whose operation is still RUNNING satisfies "not concluded".
    Sweeping it would conclude an operation handed to an executor that may be working.
    """
    store = OperationStore()
    outbox = DispatchOutbox(store=store, max_attempts=1)
    admitted = await admit_paid(store, connection, principal(), request())

    assert (await outbox.drain_once(connection, RecordingExecutor())).delivered == 1
    await connection.execute("UPDATE harness_dispatch_outbox SET abandoned_at = now()")

    assert await outbox.recover_abandoned(connection) == 0
    current = await store.get(connection, principal(), admitted.record.operation_id)
    assert current is not None
    assert current.state is OperationState.RUNNING


async def test_marking_delivered_twice_keeps_the_first_timestamp(connection):
    """Settlement is idempotent: a replayed settle does not rewrite the evidence."""
    store = OperationStore()
    outbox = DispatchOutbox(store=store)
    await admit_paid(store, connection, principal(), request())
    envelopes = await outbox.claim(connection)

    await outbox._mark_delivered(connection, envelopes[0])
    first = await connection.fetchval(
        "SELECT delivered_at FROM harness_dispatch_outbox"
    )
    await outbox._mark_delivered(connection, envelopes[0])
    second = await connection.fetchval(
        "SELECT delivered_at FROM harness_dispatch_outbox"
    )

    assert first == second
