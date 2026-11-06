"""A fresh runtime decision extends one retry while original admission survives."""

import json
from datetime import UTC, datetime, timedelta

import pytest

from harness_jobs.execution import OperationExecutor
from harness_jobs.identity import OperationRefused
from harness_jobs.leases import LeaseRefusal, LeaseRefused, acquire, renew
from harness_jobs.store import _record

from .conftest import requires_postgres
from .test_bootstrap_retry_postgres import boundary
from .test_bootstrap_runtime_authorization import amendment
from .test_provider_retry_postgres import recover, source

pytestmark = requires_postgres


@pytest.mark.parametrize("authority_seconds", [7200, 10800])
async def test_fresh_runtime_receipt_bounds_lease_and_keeps_admission(
    pool, authority_seconds
):
    record, original, step = await source(pool, bootstrap=True)
    deadline = datetime.now(UTC) + timedelta(seconds=authority_seconds)

    async def authorize(*args):
        return deadline

    async def prove(*args):
        proof = await boundary(*args)
        proof.update(
            runtime_amendment=amendment(),
            runtime_authority_expires_at=deadline.isoformat(),
        )
        return proof

    async with pool.acquire() as c:
        paid = dict(await c.fetchrow("SELECT * FROM harness_approval_consumption"))
        receipt = await recover(
            c,
            record,
            recovery_scope="unstarted-bootstrap",
            authorize=authorize,
            prove_not_started=prove,
        )
        assert (
            await recover(
                c,
                record,
                recovery_scope="unstarted-bootstrap",
                authorize=authorize,
                prove_not_started=prove,
            )
            == receipt
        )
        lease = await acquire(
            c, operation_id=record.operation_id, holder="fresh", attempt_id="fresh"
        )
        assert lease.runtime_deadline == deadline
        assert (lease.runtime_deadline - lease.acquired_at).total_seconds() > 3600
        assert lease.attempts == original.attempts + 1
        renewed = await renew(c, lease)
        assert renewed.runtime_deadline == deadline
        current = _record(await c.fetchrow("SELECT * FROM harness_operations"))
        assert current.request_payload == record.request_payload
        assert current.plan_digest == record.plan_digest
        assert (
            dict(await c.fetchrow("SELECT * FROM harness_approval_consumption")) == paid
        )
        assert (
            await c.fetchval(
                "SELECT count(*) FROM harness_execution_audit "
                "WHERE event='bootstrap-runtime-extended'"
            )
            == 1
        )
    await OperationExecutor(lease, connect=pool.acquire).record_intent(
        idempotency_key=receipt["replacement_key"],
        provider=step.provider,
        operation_kind=step.operation_kind,
        target=step.target,
    )

    async def without_amendment(*args):
        proof = await boundary(*args)
        proof["owner_evidence_id"] = proof["owner_evidence"]["record_id"] = (
            "fresh-proof"
        )
        return proof

    async with pool.acquire() as c:
        await c.execute(
            "UPDATE harness_operation_leases SET expires_at=now()-interval '1 second',"
            "runtime_deadline=now()-interval '1 second'"
        )
        second = await recover(
            c,
            current,
            reauthorization_id="fresh-review-without-runtime-amendment",
            recovery_scope="unstarted-bootstrap",
            prove_not_started=without_amendment,
        )
        lease = await acquire(
            c, operation_id=record.operation_id, holder="next", attempt_id="next"
        )
        assert (
            3599 <= (lease.runtime_deadline - lease.acquired_at).total_seconds() <= 3601
        )
        assert "runtime_amendment" not in json.loads(second["proof"])
        assert (
            dict(await c.fetchrow("SELECT * FROM harness_approval_consumption")) == paid
        )


@pytest.mark.parametrize(
    "fault",
    ["deadline", "expired", "missing-expiry", "wrong-original", "scope", "denied"],
)
async def test_runtime_amendment_refusal_keeps_every_original_record(pool, fault):
    record, _, _ = await source(pool, bootstrap=fault != "scope")
    deadline = datetime.now(UTC) + timedelta(hours=3)

    async def authorize(*args):
        if fault == "denied":
            raise OperationRefused("fresh decision refused")
        return deadline

    async def prove(*args):
        proof = await boundary(*args)
        proof.update(
            runtime_amendment=amendment(900 if fault == "wrong-original" else 3600),
            runtime_authority_expires_at=(
                deadline - timedelta(seconds=1)
                if fault == "deadline"
                else datetime.now(UTC) - timedelta(seconds=1)
                if fault == "expired"
                else deadline
            ).isoformat(),
        )
        if fault == "missing-expiry":
            del proof["runtime_authority_expires_at"]
        return proof

    async with pool.acquire() as c:
        tables = (
            "harness_operations",
            "harness_approval_consumption",
            "harness_operation_leases",
            "harness_provider_call_intent",
        )
        before = {t: dict(await c.fetchrow("SELECT * FROM " + t)) for t in tables}
        with pytest.raises(OperationRefused):
            await recover(
                c,
                record,
                recovery_scope="unstarted-planning"
                if fault == "scope"
                else "unstarted-bootstrap",
                authorize=authorize,
                prove_not_started=prove,
            )
        assert {
            t: dict(await c.fetchrow("SELECT * FROM " + t)) for t in tables
        } == before
        assert await c.fetchval("SELECT count(*) FROM harness_provider_retries") == 0


async def test_expired_runtime_receipt_cannot_acquire_and_records_refusal(
    pool, monkeypatch
):
    from harness_jobs import bootstrap_runtime_authorization as runtime

    record, _, _ = await source(pool, bootstrap=True)
    deadline = datetime.now(UTC) + timedelta(hours=3)

    async def authorize(*args):
        return deadline

    async def prove(*args):
        proof = await boundary(*args)
        proof.update(
            runtime_amendment=amendment(),
            runtime_authority_expires_at=deadline.isoformat(),
        )
        return proof

    class Expired(datetime):
        @classmethod
        def now(cls, zone):
            return deadline + timedelta(seconds=1)

    async with pool.acquire() as c:
        await recover(
            c,
            record,
            recovery_scope="unstarted-bootstrap",
            authorize=authorize,
            prove_not_started=prove,
        )
        before = dict(await c.fetchrow("SELECT * FROM harness_operation_leases"))
        monkeypatch.setattr(runtime, "datetime", Expired)
        with pytest.raises(LeaseRefused) as refusal:
            await acquire(
                c, operation_id=record.operation_id, holder="late", attempt_id="late"
            )
        assert refusal.value.reason == LeaseRefusal.RECOVERY_REQUIRED
        assert (
            dict(await c.fetchrow("SELECT * FROM harness_operation_leases")) == before
        )
        assert (
            await c.fetchval(
                "SELECT count(*) FROM harness_execution_audit "
                "WHERE event='lease.acquire' AND actor='late' AND NOT allowed"
            )
            == 1
        )
