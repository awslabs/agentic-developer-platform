"""Recovery cannot release a live writer or forget failed compensation."""

import asyncio

import pytest

from harness_jobs import DispatchOutbox, OperationRefused, OperationStore
from harness_jobs.admission import (
    BudgetUnavailable,
    Reservation,
    admit_operation,
    cancel_before_dispatch,
    list_interrupted_admissions,
    reconcile_interrupted_admissions,
)
from harness_jobs.approval import ApprovalRefused
from harness_jobs.identity import ContractViolation

from .conftest import requires_postgres
from .test_admission_postgres import (
    NOW,
    FakeFence,
    FakeLedger,
    admit,
    approval,
    envelope,
    principal,
    request,
    statuses,
)

pytestmark = requires_postgres


async def admit_with_store(connection, ledger, store):
    return await admit_operation(
        connection,
        store,
        ledger,
        principal=principal(),
        request=request(),
        approval=approval(),
        requested_envelope=envelope(),
        approver_statuses=statuses(),
        now=NOW,
    )


class Recorder:
    def __init__(self):
        self.deliveries = []

    async def deliver(self, envelope):
        self.deliveries.append(envelope)
        return True


@pytest.mark.parametrize("writer_lost", [False, True])
async def test_recovery_requires_live_writer_to_leave_before_releasing(
    pool, writer_lost
):
    entered, proceed = asyncio.Event(), asyncio.Event()

    class PausedStore(OperationStore):
        async def admit(self, *args, **kwargs):
            entered.set()
            await asyncio.wait_for(proceed.wait(), 5)
            return await super().admit(*args, **kwargs)

    ledger = FakeLedger()
    async with pool.acquire() as writer, pool.acquire() as recovery:
        task = asyncio.create_task(admit_with_store(writer, ledger, PausedStore()))
        try:
            await asyncio.wait_for(entered.wait(), 5)
            live = await reconcile_interrupted_admissions(recovery, ledger)
            assert live.released == 0 and live.unresolved == 1
            assert len(ledger._held) == 1
            if writer_lost:
                writer.terminate()
            proceed.set()
            result = (await asyncio.gather(task, return_exceptions=True))[0]
            after = await reconcile_interrupted_admissions(recovery, ledger)
            recorder = Recorder()
            delivery = await DispatchOutbox().drain_once(recovery, recorder)
            if writer_lost:
                assert isinstance(result, Exception)
                assert after.released == 1
                assert ledger._held == {}
                assert delivery.delivered == 0
                assert (
                    await recovery.fetchval("SELECT count(*) FROM harness_operations")
                    == 0
                )
            else:
                assert not isinstance(result, BaseException)
                assert after.released == 0
                assert len(ledger._held) == 1
                assert delivery.delivered == 1
        finally:
            proceed.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_recovery_skips_a_reserve_with_a_reply_still_in_flight(pool):
    entered, proceed = asyncio.Event(), asyncio.Event()

    class PausedLedger(FakeLedger):
        async def reserve(self, **kwargs):
            held = await super().reserve(**kwargs)
            entered.set()
            await asyncio.wait_for(proceed.wait(), 5)
            return held

    ledger = PausedLedger()
    async with pool.acquire() as writer, pool.acquire() as recovery:
        task = asyncio.create_task(admit(writer, ledger))
        try:
            await asyncio.wait_for(entered.wait(), 5)
            live = await reconcile_interrupted_admissions(recovery, ledger)
            assert live.released == 0 and live.unresolved == 1
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            proceed.set()
            dead = await reconcile_interrupted_admissions(recovery, ledger)
            assert dead.released == 1
            assert ledger._held == {}
        finally:
            proceed.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)


class ProcessLoss(BaseException):
    pass


class LostReserve(FakeLedger):
    lose_reply = True

    async def reserve(self, **kwargs):
        held = await super().reserve(**kwargs)
        if self.lose_reply:
            raise ProcessLoss()
        return held


async def test_concurrent_sweeps_cannot_release_the_same_intent_in_parallel(pool):
    entered, proceed = asyncio.Event(), asyncio.Event()

    class SlowRelease(LostReserve):
        async def release(self, **kwargs):
            entered.set()
            await asyncio.wait_for(proceed.wait(), 5)
            await super().release(**kwargs)

    ledger = SlowRelease()
    async with pool.acquire() as first, pool.acquire() as second:
        with pytest.raises(ProcessLoss):
            await admit(first, ledger)
        ledger.lose_reply = False
        task = asyncio.create_task(reconcile_interrupted_admissions(first, ledger))
        try:
            await asyncio.wait_for(entered.wait(), 5)
            other = await reconcile_interrupted_admissions(second, ledger)
            assert other.released == 0 and other.unresolved == 1
            proceed.set()
            result = await task
            assert result.released == 1
            assert len(ledger.released) == 1
            assert not await list_interrupted_admissions(first)
        finally:
            proceed.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)


class FailedRelease(FakeLedger):
    fail_release = True

    async def release(self, **kwargs):
        if self.fail_release:
            raise BudgetUnavailable("release unavailable")
        await super().release(**kwargs)


async def test_conflict_compensation_remains_enumerable_until_release_succeeds(
    connection,
):
    ledger = FailedRelease()
    await admit(connection, ledger, record=approval("first"))
    with pytest.raises(BudgetUnavailable):
        await admit(connection, ledger, record=approval("second"))
    assert len(ledger._held) == 2
    assert len(await list_interrupted_admissions(connection)) == 1
    ledger.fail_release = False
    report = await reconcile_interrupted_admissions(connection, ledger)
    assert report.released == 1 and report.unresolved == 0
    assert len(ledger._held) == 1
    assert not await list_interrupted_admissions(connection)


async def test_store_failure_preserves_original_error_and_pending_compensation(
    connection,
):
    class BrokenStore(OperationStore):
        async def admit(self, *args, **kwargs):
            raise OperationRefused("store unavailable")

    ledger = FailedRelease()
    with pytest.raises(OperationRefused, match="store unavailable"):
        await admit_with_store(connection, ledger, BrokenStore())
    assert len(await list_interrupted_admissions(connection)) == 1
    ledger.fail_release = False
    report = await reconcile_interrupted_admissions(connection, ledger)
    assert report.released == 1
    assert ledger._held == {}


async def test_recovery_settlement_prevents_a_late_retry_from_reopening_budget(
    connection,
):
    ledger = LostReserve()
    with pytest.raises(ProcessLoss):
        await admit(connection, ledger)
    ledger.lose_reply = False
    report = await reconcile_interrupted_admissions(connection, ledger)
    assert report.released == 1
    before = len(ledger.reserve_calls)
    with pytest.raises(ApprovalRefused, match="settled"):
        await admit(connection, ledger)
    assert len(ledger.reserve_calls) == before
    assert ledger._held == {}
    assert await connection.fetchval("SELECT count(*) FROM harness_operations") == 0


async def test_cancelled_admission_replay_does_not_reserve_again(connection):
    ledger = FakeLedger()
    first = await admit(connection, ledger)
    record = first.operation.record
    await cancel_before_dispatch(
        connection,
        ledger,
        FakeFence(),
        operation_id=record.operation_id,
        job_id=record.job_id,
        attempt_id=record.attempt_id,
        reservation=first.reservation,
        reason="cancel pending work",
    )
    before = len(ledger.reserve_calls)
    replay = await admit(connection, ledger)
    assert replay.operation.record.operation_id == record.operation_id
    assert not replay.created
    assert len(ledger.reserve_calls) == before
    assert ledger._held == {}
    assert (
        await connection.fetchval("SELECT count(*) FROM harness_dispatch_outbox") == 0
    )


async def test_outer_transaction_is_refused_before_external_effects(connection):
    ledger = FakeLedger()
    async with connection.transaction():
        with pytest.raises(ContractViolation, match="idle database connection"):
            await admit(connection, ledger)
    assert ledger.reserve_calls == []
    assert (
        await connection.fetchval("SELECT count(*) FROM harness_admission_intent") == 0
    )


async def test_recovery_does_not_release_an_unrelated_reservation(connection):
    class WrongReservation(LostReserve):
        async def reserve(self, **kwargs):
            held = await super().reserve(**kwargs)
            return Reservation(
                reservation_id=held.reservation_id,
                job_id="unrelated-job",
                attempt_id="unrelated-attempt",
            )

    ledger = WrongReservation()
    with pytest.raises(ProcessLoss):
        await admit(connection, ledger)
    ledger.lose_reply = False
    report = await reconcile_interrupted_admissions(connection, ledger)
    assert report.unresolved == 1
    assert report.released == 0
    assert ledger.released == []
    assert len(await list_interrupted_admissions(connection)) == 1


async def test_a_stale_sweep_snapshot_cannot_reopen_a_settled_intent(pool, monkeypatch):
    import harness_jobs.admission as admission_module

    enumerated, proceed = asyncio.Event(), asyncio.Event()
    original = admission_module.list_interrupted_admissions
    ledger = LostReserve()
    async with pool.acquire() as slow, pool.acquire() as fast:
        with pytest.raises(ProcessLoss):
            await admit(slow, ledger)
        ledger.lose_reply = False

        async def paused_list(connection, **kwargs):
            result = await original(connection, **kwargs)
            if connection is slow:
                enumerated.set()
                await asyncio.wait_for(proceed.wait(), 5)
            return result

        monkeypatch.setattr(
            admission_module, "list_interrupted_admissions", paused_list
        )
        task = asyncio.create_task(reconcile_interrupted_admissions(slow, ledger))
        try:
            await asyncio.wait_for(enumerated.wait(), 5)
            first = await reconcile_interrupted_admissions(fast, ledger)
            assert first.released == 1
            reserve_calls = len(ledger.reserve_calls)
            proceed.set()
            stale = await task
            assert stale.released == 0
            assert len(ledger.reserve_calls) == reserve_calls
            assert len(ledger.released) == 1
        finally:
            proceed.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_crash_after_release_leaves_a_known_reservation_to_settle(
    connection, monkeypatch
):
    import harness_jobs.admission as admission_module

    ledger = LostReserve()
    with pytest.raises(ProcessLoss):
        await admit(connection, ledger)
    ledger.lose_reply = False
    original = admission_module._resolve_intent

    async def crash_before_record(*args, **kwargs):
        raise ProcessLoss()

    monkeypatch.setattr(admission_module, "_resolve_intent", crash_before_record)
    with pytest.raises(ProcessLoss):
        await reconcile_interrupted_admissions(connection, ledger)
    assert ledger._held == {}
    pending = await list_interrupted_admissions(connection)
    assert len(pending) == 1 and pending[0].reservation_id is not None
    reserve_calls = len(ledger.reserve_calls)
    monkeypatch.setattr(admission_module, "_resolve_intent", original)
    settled = await reconcile_interrupted_admissions(connection, ledger)
    assert settled.released == 1
    assert len(ledger.reserve_calls) == reserve_calls
    assert ledger._held == {}
    assert not await list_interrupted_admissions(connection)
