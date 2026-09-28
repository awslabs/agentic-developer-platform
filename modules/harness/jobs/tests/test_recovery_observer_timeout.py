"""Recovery must finish when a provider observation never returns."""

import asyncio

import pytest

from harness_jobs.execution import (
    BudgetDisposition,
    CallOutcome,
    CallStage,
    disposition_for,
    read_call,
)
from harness_jobs.identity import ContractViolation
from harness_jobs.recovery import sweep_expired_leases, sweep_unresolved_calls

from .conftest import requires_postgres
from .test_executor_recovery_boundaries import expire, prepared

pytestmark = requires_postgres


@pytest.mark.parametrize("orphan", [False, True])
@pytest.mark.parametrize("attempts", [1, 3])
@pytest.mark.parametrize("observer_cancelled", [False, True])
async def test_hung_observer_defers_and_allows_later_operations(
    pool, orphan, attempts, observer_cancelled
):
    async with pool.acquire() as connection:
        first, _ = await prepared(connection, key="hung", outcomes=(None,))
        second, _ = await prepared(connection, key="later", outcomes=(None,))
        for record in (first, second):
            await expire(connection, record.operation_id)
        seen = []
        cancelled = asyncio.Event()

        async def observer(key, *_):
            seen.append(key)
            if key == "hung-0":
                try:
                    if observer_cancelled:
                        raise asyncio.CancelledError("observer-local cancellation")
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()
            return CallOutcome.ABSENT, None, None

        sweep = sweep_unresolved_calls if orphan else sweep_expired_leases
        await asyncio.wait_for(
            sweep(
                connection,
                observe_call=observer,
                observation_timeout_seconds=0.02,
                max_reconcile_attempts=attempts,
            ),
            timeout=3,
        )
        assert cancelled.is_set()
        assert seen == ["hung-0", "later-0"]
        call = await read_call(connection, idempotency_key="hung-0")
        assert call.stage is (
            CallStage.INTENDED if attempts > 1 else CallStage.UNRESOLVED
        )
        assert disposition_for(call) is BudgetDisposition.RETAIN
        row = await connection.fetchrow(
            "SELECT reconcile_attempts, reconcile_after > clock_timestamp() AS delayed "
            "FROM harness_provider_call_intent WHERE idempotency_key='hung-0'"
        )
        assert row["reconcile_attempts"] == 1 and row["delayed"]
        later = await read_call(connection, idempotency_key="later-0")
        assert later.stage is not CallStage.INTENDED
        async with pool.acquire() as other:
            key = f"harness-provider-dispatch:{first.operation_id}"
            assert await other.fetchval(
                "SELECT pg_try_advisory_lock(hashtextextended($1, 0))", key
            )
            await other.execute(
                "SELECT pg_advisory_unlock(hashtextextended($1, 0))", key
            )


@pytest.mark.parametrize("orphan", [False, True])
@pytest.mark.parametrize("timeout", [0, -1, 31, float("nan"), float("inf"), True, "1"])
async def test_timeout_must_be_finite_and_below_recovery_claim(
    connection, orphan, timeout
):
    async def observer(*_):
        raise AssertionError("invalid configuration must not observe")

    sweep = sweep_unresolved_calls if orphan else sweep_expired_leases
    with pytest.raises(ContractViolation, match="observation_timeout_seconds"):
        await sweep(
            connection, observe_call=observer, observation_timeout_seconds=timeout
        )


@pytest.mark.parametrize("orphan", [False, True])
@pytest.mark.parametrize("resistant", [False, True])
async def test_sweep_cancellation_propagates_and_releases_advisory_lock(
    pool, orphan, resistant
):
    async with pool.acquire() as connection:
        record, _ = await prepared(connection, key="cancelled", outcomes=(None,))
        await expire(connection, record.operation_id)
        entered = asyncio.Event()
        finish = asyncio.Event()

        async def observer(*_):
            entered.set()
            while not finish.is_set():
                try:
                    await finish.wait()
                except asyncio.CancelledError:
                    if not resistant:
                        raise

        sweep = sweep_unresolved_calls if orphan else sweep_expired_leases
        task = asyncio.create_task(sweep(connection, observe_call=observer))
        try:
            await asyncio.wait_for(entered.wait(), 3)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            call = await read_call(connection, idempotency_key="cancelled-0")
            assert call.stage is CallStage.INTENDED
            assert disposition_for(call) is BudgetDisposition.RETAIN
            async with pool.acquire() as other:
                key = f"harness-provider-dispatch:{record.operation_id}"
                assert await other.fetchval(
                    "SELECT pg_try_advisory_lock(hashtextextended($1, 0))", key
                )
                await other.execute(
                    "SELECT pg_advisory_unlock(hashtextextended($1, 0))", key
                )
        finally:
            finish.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await asyncio.sleep(0.02)


@pytest.mark.parametrize("orphan", [False, True])
async def test_cancellation_resistant_observer_cannot_strand_sweep(pool, orphan):
    async with pool.acquire() as connection:
        first, _ = await prepared(connection, key="resistant", outcomes=(None,))
        second, _ = await prepared(connection, key="following", outcomes=(None,))
        for record in (first, second):
            await expire(connection, record.operation_id)
        finish = asyncio.Event()
        seen = []

        async def observer(key, *_):
            seen.append(key)
            if key == "resistant-0":
                while not finish.is_set():
                    try:
                        await finish.wait()
                    except asyncio.CancelledError:
                        pass
            return CallOutcome.ABSENT, None, None

        sweep = sweep_unresolved_calls if orphan else sweep_expired_leases
        task = asyncio.create_task(
            sweep(connection, observe_call=observer, observation_timeout_seconds=0.02)
        )
        try:
            done, _ = await asyncio.wait({task}, timeout=0.5)
            assert task in done, "observer cancellation exceeded the recovery deadline"
            await task
            assert seen == ["resistant-0", "following-0"]
            call = await read_call(connection, idempotency_key="resistant-0")
            assert call.stage is CallStage.INTENDED
            assert disposition_for(call) is BudgetDisposition.RETAIN
            async with pool.acquire() as other:
                key = f"harness-provider-dispatch:{first.operation_id}"
                assert await other.fetchval(
                    "SELECT pg_try_advisory_lock(hashtextextended($1, 0))", key
                )
                await other.execute(
                    "SELECT pg_advisory_unlock(hashtextextended($1, 0))", key
                )
            # A retry cannot start a duplicate observer while the old one lingers.
            await expire(connection, first.operation_id)
            await connection.execute(
                "UPDATE harness_provider_call_intent SET reconcile_after=NULL "
                "WHERE idempotency_key='resistant-0'"
            )
            await sweep_unresolved_calls(
                connection, observe_call=observer, observation_timeout_seconds=0.02
            )
            assert seen.count("resistant-0") == 1
        finally:
            finish.set()
            await asyncio.wait_for(task, 3)
            await asyncio.sleep(0.02)
        # A late ABSENT result cannot release the retained reservation.
        call = await read_call(connection, idempotency_key="resistant-0")
        assert call.stage is CallStage.INTENDED
        assert disposition_for(call) is BudgetDisposition.RETAIN


@pytest.mark.parametrize("orphan", [False, True])
async def test_lingering_observer_capacity_is_bounded_and_reusable(
    connection, monkeypatch, orphan
):
    from harness_jobs import recovery

    monkeypatch.setattr(recovery, "_MAX_OBSERVER_TASKS", 2)
    for key in ("slot-a", "slot-b", "over-capacity"):
        record, _ = await prepared(connection, key=key, outcomes=(None,))
        await expire(connection, record.operation_id)
    finish = asyncio.Event()
    seen = []

    async def observer(key, *_):
        seen.append(key)
        while not finish.is_set():
            try:
                await finish.wait()
            except asyncio.CancelledError:
                pass
        raise RuntimeError("late provider failure")

    sweep = sweep_unresolved_calls if orphan else sweep_expired_leases
    try:
        await asyncio.wait_for(
            sweep(connection, observe_call=observer, observation_timeout_seconds=0.02),
            3,
        )
        assert seen == ["slot-a-0", "slot-b-0"]
        assert len(recovery._observer_tasks) == 2
        for key in ("slot-a-0", "slot-b-0", "over-capacity-0"):
            call = await read_call(connection, idempotency_key=key)
            assert call.stage is CallStage.INTENDED
            assert disposition_for(call) is BudgetDisposition.RETAIN
    finally:
        finish.set()
        await asyncio.sleep(0.02)
    assert not recovery._observer_tasks

    async def responsive(*_):
        return CallOutcome.ABSENT, None, None

    await connection.execute(
        "UPDATE harness_provider_call_intent SET reconcile_after=NULL"
    )
    await connection.execute(
        "UPDATE harness_operation_leases "
        "SET expires_at=clock_timestamp()-interval '1 second'"
    )
    assert await sweep_unresolved_calls(connection, observe_call=responsive) == 3
