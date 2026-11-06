"""Real locks, stale acknowledgements and transaction rollback for recovery."""

import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest

from harness_jobs.dispatch_recovery import recover_undispatched
from harness_jobs.execution import audit
from harness_jobs.identity import OperationRefused
from harness_jobs.outbox import DispatchOutbox

from .conftest import requires_postgres
from .test_admission_postgres import FakeLedger, admit, principal
from .test_outbox_postgres import FailingExecutor, RecordingExecutor

pytestmark = requires_postgres


async def authorize(operation, paid):
    assert paid["requester"] == principal().subject
    return datetime.now(UTC) + timedelta(minutes=5)


async def absent(operation):
    return {"undelivered": True, "mapping_digests": ["proof"]}


async def abandoned(connection):
    admitted = await admit(connection, FakeLedger())
    outbox = DispatchOutbox(max_attempts=1)
    await outbox.drain_once(connection, FailingExecutor())
    row = await connection.fetchrow("SELECT * FROM harness_operations")
    assert row["state"] == "unknown"
    return admitted, dict(row)


async def recover(connection, row, **overrides):
    args = dict(
        principal=principal(),
        operation_id=row["operation_id"],
        expected_version=row["version"],
        reauthorization_id="renewal-1",
        authorize=authorize,
        prove_absence=absent,
    )
    args.update(overrides)
    return await recover_undispatched(connection, **args)


async def test_recovery_preserves_admission_and_records_old_cycle(connection):
    _, row = await abandoned(connection)
    before = dict(await connection.fetchrow("SELECT * FROM harness_dispatch_outbox"))
    paid = dict(await connection.fetchrow("SELECT * FROM harness_approval_consumption"))
    receipt = await recover(connection, row)
    actual = dict(await connection.fetchrow("SELECT * FROM harness_operations"))
    for key in (
        "operation_id",
        "job_id",
        "attempt_id",
        "org_id",
        "workspace_id",
        "request_payload",
        "plan_digest",
    ):
        assert actual[key] == row[key]
    assert actual["state"] == "pending" and actual["version"] == row["version"] + 1
    assert (
        dict(await connection.fetchrow("SELECT * FROM harness_approval_consumption"))
        == paid
    )
    history = json.loads(receipt["outbox_snapshot"])
    assert history["attempts"] == before["attempts"] and history["abandoned_at"]
    assert (
        await connection.fetchval(
            "SELECT claim_generation FROM harness_dispatch_outbox"
        )
        == before["claim_generation"] + 1
    )
    assert await recover(connection, row) == receipt
    executor = RecordingExecutor()
    assert (await DispatchOutbox().drain_once(connection, executor)).delivered == 1
    assert (await DispatchOutbox().drain_once(connection, executor)).handled == 0
    assert len(executor.seen) == 1
    assert (
        await connection.fetchval(
            "SELECT count(*) FROM harness_dispatch_reauthorizations"
        )
        == 1
    )


@pytest.mark.parametrize(
    "change",
    [
        "cancelled",
        "cleanup",
        "delivered",
        "claimed",
        "budget",
        "version",
        "foreign",
        "audit",
    ],
)
async def test_unsafe_recovery_refused(connection, change):
    _, row = await abandoned(connection)
    overrides = {}
    if change == "cancelled":
        await connection.execute(
            "UPDATE harness_operations SET cancel_requested_at=now()"
        )
    elif change == "cleanup":
        await connection.execute("UPDATE harness_operations SET cleanup_required=true")
    elif change == "delivered":
        await connection.execute(
            "UPDATE harness_dispatch_outbox SET delivered_at=now()"
        )
    elif change == "claimed":
        await connection.execute(
            "UPDATE harness_dispatch_outbox SET claimed_until=now()+interval '1 minute'"
        )
    elif change == "budget":
        await connection.execute(
            "UPDATE harness_approval_consumption SET reservation_state='retained'"
        )
    elif change == "version":
        overrides["expected_version"] = row["version"] + 1
    elif change == "foreign":
        overrides["principal"] = principal(org="other")
    else:
        await audit(
            connection,
            operation_id=row["operation_id"],
            org_id=row["org_id"],
            workspace_id=row["workspace_id"],
            event="execution-attempt",
            actor="worker",
            allowed=False,
        )
    with pytest.raises(OperationRefused):
        await recover(connection, row, **overrides)
    assert (
        await connection.fetchval(
            "SELECT count(*) FROM harness_dispatch_reauthorizations"
        )
        == 0
    )
    assert (
        await connection.fetchval("SELECT state FROM harness_operations") == "unknown"
    )


@pytest.mark.parametrize("failure", ["transport", "expired", "ambiguous"])
async def test_missing_authority_or_transport_proof_rolls_back(connection, failure):
    _, row = await abandoned(connection)

    async def broken(operation):
        if failure == "transport":
            raise TimeoutError("protected transport unavailable")
        return {"undelivered": False}

    async def expired(operation, paid):
        return datetime.now(UTC) - timedelta(seconds=1)

    with pytest.raises((OperationRefused, TimeoutError)):
        await recover(
            connection,
            row,
            **(
                {"authorize": expired}
                if failure == "expired"
                else {"prove_absence": broken}
            ),
        )
    assert (
        await connection.fetchval(
            "SELECT count(*) FROM harness_dispatch_reauthorizations"
        )
        == 0
    )
    assert (
        await connection.fetchval("SELECT state FROM harness_operations") == "unknown"
    )


async def test_concurrent_response_loss_replay_is_single_recovery(pool):
    async with pool.acquire() as connection:
        _, row = await abandoned(connection)

    async def one():
        async with pool.acquire() as connection:
            return await recover(connection, row)

    left, right = await asyncio.gather(one(), one())
    assert left == right
    async with pool.acquire() as connection:
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM harness_dispatch_reauthorizations"
            )
            == 1
        )
        with pytest.raises(OperationRefused):
            await recover(connection, row, reauthorization_id="another-decision")
        with pytest.raises(Exception, match="immutable"):
            await connection.execute("DELETE FROM harness_dispatch_reauthorizations")


async def test_old_dispatch_ack_and_failure_cannot_settle_recovered_cycle(connection):
    from harness_jobs.outbox import DispatchEnvelope
    from harness_jobs.schema import downgrade

    _, row = await abandoned(connection)
    previous = dict(await connection.fetchrow("SELECT * FROM harness_dispatch_outbox"))
    envelope = DispatchEnvelope(
        outbox_id=previous["id"],
        **{
            key: previous[key]
            for key in DispatchEnvelope.__dataclass_fields__
            if key != "outbox_id"
        },
    )
    await recover(connection, row)
    outbox = DispatchOutbox()
    assert not await outbox._mark_delivered(connection, envelope)
    assert not await outbox._record_failure(connection, envelope, "late error")
    assert (
        await connection.fetchval("SELECT delivered_at FROM harness_dispatch_outbox")
        is None
    )
    with pytest.raises(Exception, match="reauthorization"):
        await downgrade(connection, target=10)


async def test_mismatched_outbox_is_not_reauthorized(connection):
    _, row = await abandoned(connection)
    await connection.execute("UPDATE harness_dispatch_outbox SET job_id='another-job'")
    with pytest.raises(OperationRefused, match="identity"):
        await recover(connection, row)
