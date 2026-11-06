"""A real partial mutation keeps its admission and receives one distinct repair key."""

import asyncio
import copy
import hashlib
import json

import pytest

from harness_jobs.identity import OperationRefused
from harness_jobs.provider_retry import active_call_rows
from harness_jobs.schema import downgrade

from .conftest import requires_postgres
from .test_provider_retry_postgres import recover, source

pytestmark = requires_postgres


async def evidence(operation, lease, call):
    snapshot = {
        "resources": {
            "aws_eks_cluster.workspace": {
                "type": "aws_eks_cluster",
                "id": "cluster",
                "tainted": False,
            },
            "aws_eip.nat[0]": {
                "type": "aws_eip",
                "id": "eipalloc-0123456789abcdef0",
                "tainted": True,
            },
        },
        "serial": 2,
        "lineage": "original-state",
        "version_id": "retained-version",
    }
    owner = {
        k: operation[k]
        for k in ("operation_id", "org_id", "workspace_id", "plan_digest")
    }
    owner.update(
        record_id="protected-partial-state",
        original_artifact_id="a" * 64,
        source_version=operation["version"],
        source_fence=call["fence_token"],
        source_key=call["idempotency_key"],
        snapshot=snapshot,
    )
    return {
        "evidence_kind": "partial-apply-state-v1",
        "apply_started": True,
        "original_authority_expired": True,
        "issued_sessions_expired": True,
        "owner_evidence_id": owner["record_id"],
        "owner_evidence": owner,
        "repair": {
            "artifact_id": "b" * 64,
            "preparation_operation_id": "separate-preparation",
            "plan_file_sha256": "c" * 64,
            "plan_json_sha256": "d" * 64,
            "snapshot_sha256": hashlib.sha256(
                json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
            "changes": {
                "created": ["aws_nat_gateway.workspace[0]"],
                "preserved": ["aws_eks_cluster.workspace"],
                "replaced": [
                    {
                        "address": "aws_eip.nat[0]",
                        "id": "eipalloc-0123456789abcdef0",
                        "actions": ["delete", "create"],
                    }
                ],
            },
        },
    }


async def test_concurrent_and_lost_replies_preserve_paid_admission_and_partial_history(
    pool,
):
    record, lease, _ = await source(pool, partial=True)
    async with pool.acquire() as connection:
        original = dict(await connection.fetchrow("SELECT * FROM harness_operations"))
        paid = dict(
            await connection.fetchrow("SELECT * FROM harness_approval_consumption")
        )
        intent = dict(
            await connection.fetchrow("SELECT * FROM harness_provider_call_intent")
        )

    async def resume():
        async with pool.acquire() as connection:
            try:
                return await recover(
                    connection,
                    record,
                    recovery_scope="partial-apply-repair",
                    prove_not_started=evidence,
                )
            except OperationRefused as exc:
                assert "still active" in str(exc)
                return None

    results = await asyncio.gather(resume(), resume())
    assert any(results)
    receipt = await resume()
    assert all(r is None or r == receipt for r in results)
    assert (
        receipt["original_key"]
        == intent["idempotency_key"]
        != receipt["replacement_key"]
    )
    assert json.loads(receipt["proof"])["owner_evidence"]["snapshot"]["serial"] == 2
    async with pool.acquire() as connection:
        current = dict(await connection.fetchrow("SELECT * FROM harness_operations"))
        assert {
            k: current[k]
            for k in original
            if k not in {"detail", "updated_at", "version"}
        } == {
            k: original[k]
            for k in original
            if k not in {"detail", "updated_at", "version"}
        }
        assert current["version"] == original["version"] + 1
        assert (
            dict(
                await connection.fetchrow("SELECT * FROM harness_approval_consumption")
            )
            == paid
        )
        retained = dict(
            await connection.fetchrow("SELECT * FROM harness_provider_call_intent")
        )
        assert retained["stage"] == "reconciled" and "failed" in retained["outcome"]
        assert retained["fence_token"] == intent["fence_token"]
        assert await active_call_rows(connection, record.operation_id, [retained]) == []
        current_lease = await connection.fetchrow(
            "SELECT * FROM harness_operation_leases"
        )
        assert (
            current_lease["attempts"] == lease.attempts
            and current_lease["max_attempts"] == lease.max_attempts
        )
        assert current_lease["fence_token"] == lease.fence_token + 1
        with pytest.raises(OperationRefused, match="single immutable receipt"):
            await recover(
                connection,
                record,
                recovery_scope="partial-apply-repair",
                prove_not_started=evidence,
                reauthorization_id="another-decision",
                expected_version=record.version + 1,
            )
        with pytest.raises(Exception, match="cannot discard partial apply"):
            async with connection.transaction():
                await downgrade(connection, target=14)


@pytest.mark.parametrize(
    "change",
    [
        "unstarted",
        "wrong-fence",
        "wrong-source",
        "same-plan",
        "omit-cluster",
        "replace-cluster",
        "different-state",
        "extend-attempts",
    ],
)
async def test_partial_repair_refusal_rolls_back_every_write(pool, change):
    record, _, _ = await source(pool, partial=True)

    async def changed(*args):
        proof = copy.deepcopy(await evidence(*args))
        if change == "unstarted":
            proof["apply_started"] = False
        elif change == "wrong-fence":
            proof["owner_evidence"]["source_fence"] += 1
        elif change == "wrong-source":
            proof["owner_evidence"]["operation_id"] = "foreign"
        elif change == "same-plan":
            proof["repair"]["artifact_id"] = "a" * 64
        elif change == "omit-cluster":
            proof["repair"]["changes"]["preserved"] = ["other"]
        elif change == "replace-cluster":
            proof["repair"]["changes"]["replaced"].append(
                {
                    "address": "aws_eks_cluster.workspace",
                    "actions": ["delete", "create"],
                }
            )
        elif change == "different-state":
            proof["owner_evidence"]["snapshot"]["serial"] += 1
        else:
            proof["attempt_extension"] = {"additional_attempts": 1}
        return proof

    async with pool.acquire() as connection:
        tables = [
            "harness_operations",
            "harness_operation_leases",
            "harness_approval_consumption",
            "harness_provider_call_intent",
        ]
        before = {
            t: [dict(r) for r in await connection.fetch("SELECT * FROM " + t)]
            for t in tables
        }

        with pytest.raises(OperationRefused):
            await recover(
                connection,
                record,
                recovery_scope="partial-apply-repair",
                prove_not_started=changed,
            )
        assert {
            t: [dict(r) for r in await connection.fetch("SELECT * FROM " + t)]
            for t in tables
        } == before
        assert (
            await connection.fetchval("SELECT count(*) FROM harness_provider_retries")
            == 0
        )


async def test_original_planning_cannot_use_partial_apply_authority(pool):
    record, _, _ = await source(pool)
    async with pool.acquire() as connection:
        with pytest.raises(OperationRefused, match="original infrastructure apply"):
            await recover(
                connection,
                record,
                recovery_scope="partial-apply-repair",
                prove_not_started=evidence,
            )


@pytest.mark.parametrize("bootstrap", [False, True])
@pytest.mark.parametrize("claims", [1, 3])
async def test_expired_observers_preserve_original_producer_and_advance_current_fence(
    pool, claims, bootstrap
):
    from harness_jobs.identity import ResolvedPrincipal
    from harness_jobs.leases import fence_expired_lease

    record, original, _ = await source(pool, partial=not bootstrap, bootstrap=bootstrap)
    async with pool.acquire() as connection:
        observer = ResolvedPrincipal(
            record.org_id,
            record.workspace_id,
            "observer",
            frozenset({"workspace:recover"}),
        )
        for _ in range(claims):
            takeover = await fence_expired_lease(
                connection,
                operation_id=record.operation_id,
                recovery_principal=observer,
            )
            assert takeover is not None
            await connection.execute(
                "UPDATE harness_operation_leases SET "
                "expires_at=clock_timestamp()-interval '1 second',"
                "runtime_deadline=clock_timestamp()-interval '1 second'"
            )
        from .test_bootstrap_retry_postgres import boundary

        receipt = await recover(
            connection,
            record,
            recovery_scope="unstarted-bootstrap"
            if bootstrap
            else "partial-apply-repair",
            prove_not_started=boundary if bootstrap else evidence,
        )
        assert receipt["source_fence"] == original.fence_token
        assert receipt["source_attempt_id"] == original.attempt_id
        current = await connection.fetchrow("SELECT * FROM harness_operation_leases")
        assert current["fence_token"] == takeover.lease.fence_token + 1
        assert current["attempts"] == original.attempts
        assert current["max_attempts"] == original.max_attempts
        calls = await connection.fetch("SELECT * FROM harness_provider_call_intent")
        assert await active_call_rows(connection, record.operation_id, calls) == []
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM harness_recovery_claim_bindings"
            )
            == claims
        )


@pytest.mark.parametrize(
    "change",
    [
        "live",
        "unbound",
        "foreign",
        "contradictory-execution-audit",
        "different-holder",
        "missing-audit",
    ],
)
@pytest.mark.parametrize("bootstrap", [False, True])
async def test_observer_drift_cannot_authorize_repair(pool, change, bootstrap):
    from harness_jobs.identity import ResolvedPrincipal
    from harness_jobs.leases import fence_expired_lease

    record, _, _ = await source(pool, partial=not bootstrap, bootstrap=bootstrap)
    async with pool.acquire() as connection:
        observer = ResolvedPrincipal(
            record.org_id,
            record.workspace_id,
            "observer",
            frozenset({"workspace:recover"}),
        )
        takeover = await fence_expired_lease(
            connection,
            operation_id=record.operation_id,
            recovery_principal=None if change == "unbound" else observer,
        )
        if change == "contradictory-execution-audit":
            from harness_jobs.execution import audit

            await audit(
                connection,
                operation_id=record.operation_id,
                org_id=record.org_id,
                workspace_id=record.workspace_id,
                attempt_id="successor",
                actor="successor",
                fence_token=takeover.lease.fence_token,
                event="lease.acquire",
                allowed=True,
            )
        if change != "live":
            await connection.execute(
                "UPDATE harness_operation_leases SET "
                "expires_at=clock_timestamp()-interval '1 second',"
                "runtime_deadline=clock_timestamp()-interval '1 second'"
            )
        if change == "foreign":
            await connection.execute(
                "UPDATE harness_recovery_claim_bindings SET workspace_id='foreign'"
            )
        elif change == "different-holder":
            await connection.execute(
                "UPDATE harness_operation_leases SET holder='other'"
            )
        elif change == "missing-audit":
            # Simulate incomplete provenance without altering append-only audit.
            await connection.execute(
                "UPDATE harness_recovery_claim_bindings "
                "SET holder='not-the-audited-holder'"
            )
        before = dict(
            await connection.fetchrow("SELECT * FROM harness_operation_leases")
        )
        from .test_bootstrap_retry_postgres import boundary

        with pytest.raises(OperationRefused):
            await recover(
                connection,
                record,
                recovery_scope="unstarted-bootstrap"
                if bootstrap
                else "partial-apply-repair",
                prove_not_started=boundary if bootstrap else evidence,
            )
        assert (
            dict(await connection.fetchrow("SELECT * FROM harness_operation_leases"))
            == before
        )
        assert (
            await connection.fetchval("SELECT count(*) FROM harness_provider_retries")
            == 0
        )
