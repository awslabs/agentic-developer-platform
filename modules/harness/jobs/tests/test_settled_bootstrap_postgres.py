"""A real unknown settlement requires a new decision and retains its history."""

import copy
import json
from datetime import UTC, datetime, timedelta

import pytest

from harness_jobs.identity import OperationRefused, ResolvedPrincipal
from harness_jobs.leases import acquire
from harness_jobs.recovery import sweep_scoped_expired_leases
from harness_jobs.settled_bootstrap import read
from harness_jobs.store import _record

from .conftest import requires_postgres
from .test_bootstrap_retry_postgres import complete_acquisition_boundary
from .test_provider_retry_postgres import recover, source

pytestmark = requires_postgres


async def settled_source(pool, max_runtime_seconds=3600):
    record, original, step = await source(
        pool, bootstrap=True, max_runtime_seconds=max_runtime_seconds
    )

    async def retained(connection, lease, result):
        await connection.execute(
            "INSERT INTO harness_recovery_settlements("
            "receipt_id,operation_id,org_id,workspace_id,job_id,attempt_id,"
            "claim_holder,claim_attempt_id,claim_fence_token,"
            "payload_digest,accounting) "
            "VALUES('original-settlement',$1,$2,$3,$4,$5,$6,$7,$8,$9,$10::jsonb)",
            record.operation_id,
            record.org_id,
            record.workspace_id,
            record.job_id,
            record.attempt_id,
            lease.holder,
            lease.attempt_id,
            lease.fence_token,
            "a" * 64,
            json.dumps(
                {"action": "unknown", "budget": "retain", "release_permitted": False}
            ),
        )

    async with pool.acquire() as connection:
        result = await sweep_scoped_expired_leases(
            connection,
            principal=ResolvedPrincipal(
                record.org_id,
                record.workspace_id,
                "observer",
                frozenset({"workspace:recover"}),
            ),
            max_reconcile_attempts=1,
            on_settled=retained,
        )
        assert result.results[0].action == "unknown"
        await connection.execute(
            "UPDATE harness_recovery_settlements SET delivered_at=clock_timestamp()"
        )
        current = _record(await connection.fetchrow("SELECT * FROM harness_operations"))
    return current, original, step


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "legacy-endpoint",
        "version",
        "failed",
        "cancelled",
        "cleanup",
        "closure",
        "owner",
        "proof-kind",
        "still-granted",
        "decision",
        "provider-ref",
        "claim",
        "undelivered",
        "exhausted",
        "missing-runtime",
        "expired-runtime",
        "wrong-runtime-kind",
    ],
)
@pytest.mark.parametrize("original_runtime", [3600, 10800])
async def test_only_exact_settlement_reopens_with_history_and_runtime(
    pool, fault, original_runtime
):
    record, original, step = await settled_source(pool, original_runtime)
    deadline = datetime.now(UTC) + timedelta(hours=3)

    async def authorize(*args):
        if fault == "decision":
            raise OperationRefused("human decision refused")
        return deadline

    async with pool.acquire() as connection:
        if fault == "failed":
            await connection.execute("UPDATE harness_operations SET state='failed'")
        elif fault == "cancelled":
            await connection.execute(
                "UPDATE harness_operations SET cancel_requested_at=now()"
            )
        elif fault == "cleanup":
            await connection.execute(
                "UPDATE harness_operations SET cleanup_required=true"
            )
        elif fault == "provider-ref":
            await connection.execute(
                "UPDATE harness_provider_call_intent SET provider_ref='unexpected'"
            )
        elif fault == "claim":
            await connection.execute(
                "UPDATE harness_operation_leases SET closed_holder='foreign'"
            )
        elif fault == "undelivered":
            # The immutable trigger forbids undoing delivery. Simulate a corrupted
            # reader below rather than weakening the production database contract.
            pass
        elif fault == "exhausted":
            await connection.execute(
                "UPDATE harness_operation_leases SET attempts=max_attempts"
            )
        tables = (
            "harness_operations",
            "harness_operation_leases",
            "harness_provider_call_intent",
            "harness_approval_consumption",
            "harness_recovery_settlements",
        )
        before = {
            t: dict(await connection.fetchrow("SELECT * FROM " + t)) for t in tables
        }
        audits = [
            dict(r)
            for r in await connection.fetch(
                "SELECT * FROM harness_execution_audit ORDER BY id"
            )
        ]

        async def prove(operation, lease, call):
            proof = await complete_acquisition_boundary(operation, lease, call)
            proof.pop("attempt_extension")
            proof["owner_evidence"].pop("attempt_extension")
            closure = await read(connection, operation, lease, call)
            proof["settled_bootstrap"] = closure
            proof["owner_evidence"]["settled_bootstrap"] = copy.deepcopy(closure)
            proof.update(
                runtime_amendment={
                    "version": 1,
                    "kind": (
                        "bootstrap-runtime-reauthorization-v1"
                        if original_runtime == 10800
                        else "bootstrap-runtime-extension-v1"
                    ),
                    "original_max_runtime_seconds": original_runtime,
                    "max_runtime_seconds": 10800,
                },
                runtime_authority_expires_at=deadline.isoformat(),
            )
            if fault == "closure":
                closure["operation"]["version"] += 1
            elif fault == "owner":
                proof["owner_evidence"].pop("settled_bootstrap")
            elif fault == "proof-kind":
                proof["evidence_kind"] = "bootstrap-journal-barrier-v1"
            elif fault == "still-granted":
                proof["bootstrap_journal_revoked"] = False
            elif fault == "undelivered":
                closure["settlement"]["delivered_at"] = None
            elif fault == "missing-runtime":
                proof.pop("runtime_amendment")
                proof.pop("runtime_authority_expires_at")
            elif fault == "expired-runtime":
                proof["runtime_authority_expires_at"] = (
                    datetime.now(UTC) - timedelta(seconds=1)
                ).isoformat()
            elif fault == "wrong-runtime-kind":
                proof["runtime_amendment"]["kind"] = (
                    "bootstrap-runtime-extension-v1"
                    if original_runtime == 10800
                    else "bootstrap-runtime-reauthorization-v1"
                )
            return proof

        args = dict(
            recovery_scope="unstarted-bootstrap",
            settled_bootstrap=fault != "legacy-endpoint",
            prove_not_started=prove,
            authorize=authorize,
            expected_version=record.version + (fault == "version"),
        )
        if fault:
            with pytest.raises(OperationRefused):
                await recover(connection, record, **args)
            assert {
                t: dict(await connection.fetchrow("SELECT * FROM " + t)) for t in tables
            } == before
            assert not await connection.fetchval(
                "SELECT count(*) FROM harness_provider_retries"
            )
            return
        receipt = await recover(connection, record, **args)
        assert await recover(connection, record, **args) == receipt
        retained = json.loads(receipt["proof"])["settled_bootstrap"]
        assert (
            retained["lease"]["closed_holder"]
            == before["harness_operation_leases"]["closed_holder"]
        )
        assert retained["call"]["stage"] == "unresolved"
        assert (
            json.loads(receipt["source_intent"])["outcome"]
            == before["harness_provider_call_intent"]["outcome"]
        )
        assert (
            dict(
                await connection.fetchrow("SELECT * FROM harness_recovery_settlements")
            )
            == before["harness_recovery_settlements"]
        )
        assert (
            dict(
                await connection.fetchrow("SELECT * FROM harness_approval_consumption")
            )
            == before["harness_approval_consumption"]
        )
        assert [
            dict(r)
            for r in await connection.fetch(
                "SELECT * FROM harness_execution_audit WHERE id<=$1 ORDER BY id",
                audits[-1]["id"],
            )
        ] == audits
        fresh = await acquire(
            connection,
            operation_id=record.operation_id,
            holder="next",
            attempt_id="next",
        )
        assert fresh.runtime_deadline == deadline
        assert fresh.attempts == original.attempts + 1
        assert fresh.max_attempts == original.max_attempts
        assert fresh.fence_token > retained["lease"]["fence_token"]
        current = await connection.fetchrow("SELECT * FROM harness_operations")
        assert (
            current["state"] == "running" and current["version"] == record.version + 1
        )
        assert current["request_payload"] == record.request_payload
