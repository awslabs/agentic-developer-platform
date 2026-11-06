"""Original bootstrap retry preserves paid history and requires its own proof."""

import json

import pytest

from harness_jobs.execution import CallOutcome, OperationExecutor
from harness_jobs.execution_rpc import ExecutionGrant, ExecutionRPCServer
from harness_jobs.identity import OperationRefused
from harness_jobs.leases import acquire
from harness_jobs.schema import downgrade

from .conftest import cancellation_principal, requires_postgres
from .test_provider_retry_postgres import recover, source

pytestmark = requires_postgres


async def complete_acquisition_boundary(operation, lease, call, additional_attempts=10):
    import copy

    from .test_admission_postgres import principal

    value = await boundary(operation, lease, call)
    owner = value["owner_evidence"]
    keys = ["registrar-entry", "registrar-policy", "workspace-namespace"]
    for actor in ("supervisor", "installer"):
        keys.append(actor + "-entry")
        for scope in ("cluster", "namespace", "system"):
            keys.extend(
                actor + "-" + scope + "-" + kind for kind in ("role", "binding")
            )
    keys.extend(["retirement-fence-policy", "retirement-fence-binding"])
    for scope in ("cluster", "namespace", "system"):
        keys.extend("cleanup-" + scope + "-" + kind for kind in ("role", "binding"))
    journal = {
        **{k: operation[k] for k in ("operation_id", "org_id", "workspace_id")},
        "cluster_arn": "arn:aws:eks:us-west-2:123456789012:cluster/original",
        "generation": "d" * 64,
        "claim": "a" * 64,
        "plan_sha256": "b" * 64,
        "revoked_progress_sha256": "c" * 64,
        "acquisition_prefix": [{"key": key, "generation": "d" * 64} for key in keys],
        "retained_namespace": {
            "progress": {"phase": "adopted", "identity": {"uid": "original-namespace"}}
        },
    }
    namespace = {
        "key": "workspace-namespace",
        "generation": journal["generation"],
        "cluster_arn": journal["cluster_arn"],
        "kind": "kubernetes",
        "lifetime": "resource",
        "actor": "registrar",
        "body": {"kind": "Namespace", "apiVersion": "v1"},
    }
    journal["acquisition_prefix"][2] = namespace
    journal["retained_namespace"]["grant"] = copy.deepcopy(namespace)
    revoked = {
        "current_generation": journal["generation"],
        "journals": [journal],
        "predecessor": None,
    }
    extension = {
        "authorized_by": principal().subject,
        "authorization": (
            "Authorize up to ten additional bootstrap attempts after cleanup, "
            "with a total ceiling of twenty"
        ),
        "previous_limit": 10,
        "recorded_attempts": 10,
        "additional_attempts": additional_attempts,
    }
    owner["source_hashes"][
        "workspace_bootstrap/superplane_bootstrap/retirement_fence.py"
    ] = "e" * 64
    for target in (owner, value):
        target.update(
            evidence_kind="bootstrap-acquisition-revoked-v1",
            revoked_bootstrap=copy.deepcopy(revoked),
            attempt_extension=copy.deepcopy(extension),
        )
    value.update(
        bootstrap_mutations_started=True,
        bootstrap_grants_created=True,
        bootstrap_components_started=True,
        bootstrap_journal_revoked=True,
    )
    return value


@pytest.mark.parametrize(
    "fault,additional_attempts",
    [
        (None, 1),
        (None, 5),
        (None, 10),
    ]
    + [
        (fault, 10)
        for fault in (
            "actor",
            "missing-authorization",
            "extra-attempt",
            "zero",
            "negative",
            "old-limit",
            "bool",
            "float",
            "not-exhausted",
            "already-extended",
            "reextend-eleven",
            "reextend-twenty",
            "partial-prefix",
            "no-current-journal",
            "excess-pre-extension-history",
            "unrevoked",
            "unqualified-source",
            "owner-mismatch",
            "changed-artifact",
            "live-session",
            "approval-denied",
            "extra-field",
        )
    ],
)
async def test_acquisition_allowance_is_granted_once_and_keeps_paid_history(
    pool, fault, additional_attempts
):
    import copy

    from harness_jobs.provider_retry import effective_step_key

    record, original, step = await source(pool, bootstrap=True)

    async def prove(operation, lease, call):
        value = await complete_acquisition_boundary(
            operation, lease, call, additional_attempts
        )
        owner = value["owner_evidence"]
        extension = value["attempt_extension"]
        if fault == "actor":
            extension["authorized_by"] = "another-human"
        elif fault == "missing-authorization":
            extension["authorization"] = " "
        elif fault == "extra-attempt":
            extension["additional_attempts"] = 11
        elif fault == "zero":
            extension["additional_attempts"] = 0
        elif fault == "negative":
            extension["additional_attempts"] = -1
        elif fault == "old-limit":
            extension["previous_limit"] = extension["recorded_attempts"] = 9
        elif fault == "bool":
            extension["additional_attempts"] = True
        elif fault == "float":
            extension["additional_attempts"] = 10.0
        elif fault in {"reextend-eleven", "reextend-twenty"}:
            extension["previous_limit"] = extension["recorded_attempts"] = (
                11 if fault == "reextend-eleven" else 20
            )
            extension["additional_attempts"] = 1
        elif fault == "extra-field":
            extension["bypass"] = True
        elif fault == "partial-prefix":
            value["revoked_bootstrap"]["journals"][0]["acquisition_prefix"].pop()
        elif fault == "no-current-journal":
            value["revoked_bootstrap"]["current_generation"] = None
        elif fault == "excess-pre-extension-history":
            value["revoked_bootstrap"]["journals"] *= 11
        elif fault == "unrevoked":
            value["bootstrap_journal_revoked"] = False
        elif fault == "unqualified-source":
            owner["source_hashes"] = {"runtime": "qualified"}
        elif fault == "changed-artifact":
            value["source_artifact_id"] = "c" * 64
        elif fault == "live-session":
            value["issued_sessions_expired"] = False
        owner["attempt_extension"] = copy.deepcopy(extension)
        owner["revoked_bootstrap"] = copy.deepcopy(value["revoked_bootstrap"])
        if fault == "owner-mismatch":
            owner["attempt_extension"]["authorization"] = "different"
        return value

    async with pool.acquire() as c:
        attempts, ceiling = (
            (9, 10)
            if fault == "not-exhausted"
            else (11, 11)
            if fault in {"already-extended", "reextend-eleven"}
            else (20, 20)
            if fault == "reextend-twenty"
            else (10, 10)
        )
        await c.execute(
            "UPDATE harness_operation_leases SET attempts=$1,max_attempts=$2",
            attempts,
            ceiling,
        )
        before = dict(await c.fetchrow("SELECT * FROM harness_operation_leases"))
        admission = dict(await c.fetchrow("SELECT * FROM harness_approval_consumption"))
        operation = dict(await c.fetchrow("SELECT * FROM harness_operations"))
        kwargs = dict(recovery_scope="unstarted-bootstrap", prove_not_started=prove)
        if fault == "approval-denied":

            async def denied(*args):
                raise OperationRefused("fresh original-human decision required")

            kwargs["authorize"] = denied
        if fault:
            with pytest.raises(OperationRefused):
                await recover(c, record, **kwargs)
            assert (
                dict(await c.fetchrow("SELECT * FROM harness_operation_leases"))
                == before
            )
            assert (
                dict(await c.fetchrow("SELECT * FROM harness_operations")) == operation
            )
            assert (
                await c.fetchval("SELECT count(*) FROM harness_provider_retries") == 0
            )
        else:
            receipt = await recover(c, record, **kwargs)
            assert await recover(c, record, **kwargs) == receipt
            assert (
                await c.fetchval("SELECT attempts FROM harness_operation_leases") == 10
            )
            assert (
                await c.fetchval("SELECT max_attempts FROM harness_operation_leases")
                == 10 + additional_attempts
            )
            assert (
                await c.fetchval(
                    "SELECT count(*) FROM harness_execution_audit "
                    "WHERE event='execution-attempt-limit-extended'"
                )
                == 1
            )
            assert (
                await effective_step_key(c, record, step) == receipt["replacement_key"]
            )
            current = dict(await c.fetchrow("SELECT * FROM harness_operations"))
            for key in (
                "request_payload",
                "plan_digest",
                "job_id",
                "attempt_id",
                "operation_id",
            ):
                assert current[key] == operation[key]
            next_lease = await acquire(
                c, operation_id=record.operation_id, holder="next", attempt_id="next"
            )
            assert (next_lease.attempts, next_lease.max_attempts) == (
                11,
                10 + additional_attempts,
            )
            assert next_lease.fence_token == original.fence_token + 2
        assert (
            dict(await c.fetchrow("SELECT * FROM harness_approval_consumption"))
            == admission
        )


async def test_concurrent_acquisition_decisions_cannot_duplicate_the_allowance(pool):
    import asyncio

    record, original, _ = await source(pool, bootstrap=True)
    entered, release = asyncio.Event(), asyncio.Event()

    async def prove(operation, lease, call):
        entered.set()
        await release.wait()
        return await complete_acquisition_boundary(operation, lease, call)

    async with pool.acquire() as first, pool.acquire() as competing:
        await first.execute(
            "UPDATE harness_operation_leases SET attempts=10,max_attempts=10"
        )
        paid = dict(await first.fetchrow("SELECT * FROM harness_approval_consumption"))
        pending = asyncio.create_task(
            recover(
                first,
                record,
                reauthorization_id="first-allowance-decision",
                recovery_scope="unstarted-bootstrap",
                prove_not_started=prove,
            )
        )
        try:
            await asyncio.wait_for(entered.wait(), timeout=5)
            with pytest.raises(OperationRefused, match="dispatch is still active"):
                await recover(
                    competing,
                    record,
                    reauthorization_id="competing-allowance-decision",
                    recovery_scope="unstarted-bootstrap",
                    prove_not_started=complete_acquisition_boundary,
                )
        finally:
            release.set()
            receipt = await asyncio.wait_for(pending, timeout=5)
        assert (
            await recover(
                competing,
                record,
                reauthorization_id="first-allowance-decision",
                recovery_scope="unstarted-bootstrap",
                prove_not_started=complete_acquisition_boundary,
            )
            == receipt
        )
        with pytest.raises(OperationRefused):
            await recover(
                competing,
                record,
                reauthorization_id="competing-allowance-decision",
                recovery_scope="unstarted-bootstrap",
                prove_not_started=complete_acquisition_boundary,
            )
        lease = await competing.fetchrow("SELECT * FROM harness_operation_leases")
        assert (lease["attempts"], lease["max_attempts"]) == (10, 20)
        assert lease["fence_token"] == original.fence_token + 1
        assert (
            await competing.fetchval("SELECT count(*) FROM harness_provider_retries")
            == 1
        )
        assert (
            await competing.fetchval(
                "SELECT count(*) FROM harness_execution_audit "
                "WHERE event='execution-attempt-limit-extended'"
            )
            == 1
        )
        assert (
            dict(await competing.fetchrow("SELECT * FROM harness_approval_consumption"))
            == paid
        )


async def test_fresh_recovery_inherits_twenty_without_another_extension(pool):
    import copy
    import hashlib

    from harness_jobs.store import _record

    record, _, step = await source(pool, bootstrap=True)
    async with pool.acquire() as c:
        await c.execute(
            "UPDATE harness_operation_leases SET attempts=10,max_attempts=10"
        )
        first = await recover(
            c,
            record,
            recovery_scope="unstarted-bootstrap",
            prove_not_started=complete_acquisition_boundary,
        )
        next_lease = await acquire(
            c,
            operation_id=record.operation_id,
            holder="attempt-11",
            attempt_id="attempt-11",
        )
        current = _record(await c.fetchrow("SELECT * FROM harness_operations"))
    await OperationExecutor(next_lease, connect=pool.acquire).record_intent(
        idempotency_key=first["replacement_key"],
        provider=step.provider,
        operation_kind=step.operation_kind,
        target=step.target,
    )

    async def fresh_proof(operation, lease, call):
        proof = await complete_acquisition_boundary(operation, lease, call)
        proof.pop("attempt_extension")
        proof.update(bootstrap_mutations_started=False, bootstrap_grants_created=False)
        owner = proof["owner_evidence"]
        owner.pop("attempt_extension")
        owner["record_id"] = proof["owner_evidence_id"] = "fresh-owner-after-attempt-11"
        # The later stopped attempt created no new journal. Retain the prior
        # complete cleanup and bind its immutable receipt as the predecessor.
        previous = json.loads(first["proof"])
        proof["revoked_bootstrap"].update(
            current_generation=None,
            predecessor={
                "reauthorization_id": first["reauthorization_id"],
                "source_fence": first["source_fence"],
                "proof_sha256": hashlib.sha256(
                    json.dumps(previous, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest(),
            },
        )
        owner["revoked_bootstrap"] = copy.deepcopy(proof["revoked_bootstrap"])
        return proof

    async with pool.acquire() as c:
        await c.execute(
            "UPDATE harness_operation_leases SET "
            "expires_at=clock_timestamp()-interval '1 second',"
            "runtime_deadline=clock_timestamp()-interval '1 second'"
        )
        second = await recover(
            c,
            current,
            reauthorization_id="fresh-attempt-11-recovery-decision",
            recovery_scope="unstarted-bootstrap",
            prove_not_started=fresh_proof,
        )
        assert second["original_key"] == first["replacement_key"]
        assert (
            dict(
                await c.fetchrow(
                    "SELECT * FROM harness_provider_retries "
                    "WHERE reauthorization_id=$1",
                    first["reauthorization_id"],
                )
            )
            == first
        )
        assert (
            await c.fetchval(
                "SELECT count(*) FROM harness_execution_audit "
                "WHERE event='execution-attempt-limit-extended'"
            )
            == 1
        )
        lease = await acquire(
            c,
            operation_id=record.operation_id,
            holder="attempt-12",
            attempt_id="attempt-12",
        )
        assert (lease.attempts, lease.max_attempts) == (12, 20)


async def test_twenty_attempt_history_preserves_every_receipt_and_refuses_twenty_one(
    pool,
):
    import copy
    import hashlib
    import importlib.util
    from pathlib import Path

    from harness_jobs.store import _record

    # Exercise the independently packaged Gateway proof reader against the same
    # growing history consumed by the real locked Harness recovery transaction.
    path = Path(__file__).resolve().parents[4] / (
        "modules/gateway/src/shared/domain_bootstrap_revoked.py"
    )
    spec = importlib.util.spec_from_file_location("gateway_revoked_contract", path)
    contract = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(contract)
    record, lease, step = await source(pool, bootstrap=True)
    previous, original_receipts = None, []
    async with pool.acquire() as c:
        await c.execute("UPDATE harness_operation_leases SET max_attempts=10")
        paid = dict(await c.fetchrow("SELECT * FROM harness_approval_consumption"))

    def generation(journal, attempt):
        journal = copy.deepcopy(journal)
        journal["generation"] = f"{attempt:064x}"
        for grant in journal["acquisition_prefix"]:
            grant["generation"] = journal["generation"]
        journal["retained_namespace"]["grant"]["generation"] = journal["generation"]
        return journal

    for attempt in range(1, 21):

        async def prove(operation, expired, call):
            proof = await complete_acquisition_boundary(operation, expired, call)
            owner = proof["owner_evidence"]
            prior = json.loads(previous["proof"]) if previous else None
            journal = generation(proof["revoked_bootstrap"]["journals"][0], attempt)
            journals = (
                copy.deepcopy(prior["revoked_bootstrap"]["journals"]) if prior else []
            )
            reference = {
                "journals": journals + [journal],
                "current_generation": journal["generation"],
                "predecessor": None
                if previous is None
                else {
                    "reauthorization_id": previous["reauthorization_id"],
                    "source_fence": previous["source_fence"],
                    "proof_sha256": hashlib.sha256(
                        json.dumps(
                            prior, sort_keys=True, separators=(",", ":")
                        ).encode()
                    ).hexdigest(),
                },
            }
            owner["record_id"] = proof["owner_evidence_id"] = f"fresh-owner-{attempt}"
            owner["revoked_bootstrap"] = copy.deepcopy(reference)
            proof["revoked_bootstrap"] = reference
            if attempt != 10:
                proof.pop("attempt_extension")
                owner.pop("attempt_extension")
            contract.validate_revoked(owner)
            if attempt == 20:
                oversized = copy.deepcopy(owner)
                oversized["revoked_bootstrap"]["journals"].append(
                    generation(journal, 21)
                )
                with pytest.raises(ValueError):
                    contract.validate_revoked(oversized)
            return proof

        async with pool.acquire() as c:
            current = _record(await c.fetchrow("SELECT * FROM harness_operations"))
            kwargs = dict(
                recovery_scope="unstarted-bootstrap",
                reauthorization_id=f"new-decision-{attempt}",
                prove_not_started=prove,
            )
            if attempt == 20:
                with pytest.raises(OperationRefused, match="exhausted"):
                    await recover(c, current, **kwargs)
                assert (
                    dict(await c.fetchrow("SELECT * FROM harness_approval_consumption"))
                    == paid
                )
                assert (
                    await c.fetchval(
                        "SELECT max_attempts FROM harness_operation_leases"
                    )
                    == 20
                )
                assert (
                    await c.fetchval("SELECT attempts FROM harness_operation_leases")
                    == 20
                )
                assert (
                    await c.fetchval(
                        "SELECT count(*) FROM harness_execution_audit "
                        "WHERE event='execution-attempt-limit-extended'"
                    )
                    == 1
                )
                assert [
                    dict(row)
                    for row in await c.fetch(
                        "SELECT * FROM harness_provider_retries ORDER BY source_fence"
                    )
                ] == original_receipts
                break
            receipt = await recover(c, current, **kwargs)
            assert await recover(c, current, **kwargs) == receipt
            lease = await acquire(
                c,
                operation_id=record.operation_id,
                holder=f"worker-{attempt + 1}",
                attempt_id=f"attempt-{attempt + 1}",
            )
            assert lease.attempts == attempt + 1
            assert lease.max_attempts == (10 if attempt < 10 else 20)
        previous = receipt
        original_receipts.append(receipt)
        await OperationExecutor(lease, connect=pool.acquire).record_intent(
            idempotency_key=receipt["replacement_key"],
            provider=step.provider,
            operation_kind=step.operation_kind,
            target=step.target,
        )
        async with pool.acquire() as c:
            await c.execute(
                "UPDATE harness_operation_leases SET "
                "expires_at=clock_timestamp()-interval '1 second',"
                "runtime_deadline=clock_timestamp()-interval '1 second'"
            )


async def boundary(operation, lease, call):
    owner = {
        **{
            k: operation[k]
            for k in ("operation_id", "org_id", "workspace_id", "plan_digest")
        },
        "record_id": "protected-bootstrap-source",
        "evidence_kind": "bootstrap-journal-barrier-v1",
        "source_fence": call["fence_token"],
        "source_key": call["idempotency_key"],
        "source_image": "qualified-original-image",
        "source_hashes": {"runtime": "qualified"},
    }
    return {
        "evidence_kind": "bootstrap-journal-barrier-v1",
        "original_authority_expired": True,
        "issued_sessions_expired": True,
        "bootstrap_mutations_started": False,
        "owner_evidence_id": owner["record_id"],
        "owner_evidence": owner,
        "source_artifact_id": call["target"],
        "source_artifact_sha256": "b" * 64,
    }


async def test_bootstrap_retry_uses_new_call_with_original_admission_and_counts(pool):
    record, original, step = await source(pool, bootstrap=True)
    async with pool.acquire() as c:
        paid = dict(await c.fetchrow("SELECT * FROM harness_approval_consumption"))
        receipt = await recover(
            c, record, recovery_scope="unstarted-bootstrap", prove_not_started=boundary
        )
        assert (
            await recover(
                c,
                record,
                recovery_scope="unstarted-bootstrap",
                prove_not_started=boundary,
            )
            == receipt
        )
        lease = await acquire(
            c, operation_id=record.operation_id, holder="next", attempt_id="next"
        )
        assert lease.attempts == original.attempts + 1
        assert lease.max_attempts == original.max_attempts
        assert (
            dict(await c.fetchrow("SELECT * FROM harness_approval_consumption")) == paid
        )
        with pytest.raises(Exception, match="cannot discard bootstrap"):
            async with c.transaction():
                await downgrade(c, target=15)
    calls = []

    async def provider(call):
        calls.append(call)
        return CallOutcome.SUCCEEDED, "fixture bootstrap completed", "fixture-result"

    executor = OperationExecutor(lease, connect=pool.acquire, provider_call=provider)
    rpc = ExecutionRPCServer(
        connect=pool.acquire, provider_call=provider, authenticate=lambda _: None
    )
    await rpc.execute_step(
        ExecutionGrant(cancellation_principal("next"), lease), executor, step.step_id
    )
    assert [c.idempotency_key for c in calls] == [receipt["replacement_key"]]
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT state FROM harness_operations") == "succeeded"
        history = await c.fetch(
            "SELECT * FROM harness_provider_call_intent ORDER BY fence_token"
        )
        assert len(history) == 2 and history[0]["fence_token"] == original.fence_token
        assert history[0]["attempt_id"] == original.attempt_id
        assert history[0]["stage"] == "reconciled"
        assert json.loads(receipt["proof"])["bootstrap_mutations_started"] is False


@pytest.mark.parametrize(
    "fault",
    [
        "phase",
        "started",
        "sessions",
        "foreign",
        "artifact",
        "extension",
        "missing-owner",
    ],
)
async def test_bootstrap_retry_refuses_unproven_or_foreign_boundary_without_mutation(
    pool, fault
):
    record, _, _ = await source(pool, bootstrap=fault != "phase")

    async def prove(*args):
        proof = await boundary(*args)
        if fault == "started":
            proof["bootstrap_mutations_started"] = True
        elif fault == "sessions":
            proof["issued_sessions_expired"] = False
        elif fault == "foreign":
            proof["owner_evidence"]["operation_id"] = "another-operation"
        elif fault == "artifact":
            proof["source_artifact_id"] = "c" * 64
        elif fault == "extension":
            proof["attempt_extension"] = {"additional_attempts": 1}
        elif fault == "missing-owner":
            del proof["owner_evidence"]
        return proof

    async with pool.acquire() as c:
        before = dict(await c.fetchrow("SELECT * FROM harness_operation_leases"))
        with pytest.raises(OperationRefused):
            await recover(
                c, record, recovery_scope="unstarted-bootstrap", prove_not_started=prove
            )
        assert (
            dict(await c.fetchrow("SELECT * FROM harness_operation_leases")) == before
        )
        assert await c.fetchval("SELECT count(*) FROM harness_provider_retries") == 0
        assert (
            await c.fetchval("SELECT stage FROM harness_provider_call_intent")
            == "intended"
        )


async def test_schema_15_upgrade_preserves_original_admission(pool):
    from harness_jobs.schema import SCHEMA_VERSION, apply, current_version

    record, _, _ = await source(pool, bootstrap=True)
    async with pool.acquire() as connection:
        before = dict(
            await connection.fetchrow("SELECT * FROM harness_approval_consumption")
        )
        async with connection.transaction():
            assert await downgrade(connection, target=15) == 15
        assert await current_version(connection) == 15
        assert await apply(connection) == SCHEMA_VERSION
        assert (
            dict(
                await connection.fetchrow("SELECT * FROM harness_approval_consumption")
            )
            == before
        )
        receipt = await recover(
            connection,
            record,
            recovery_scope="unstarted-bootstrap",
            prove_not_started=boundary,
        )
        assert receipt["recovery_scope"] == "unstarted-bootstrap"


@pytest.mark.parametrize(
    "fault", [None, "reused-proof", "changed-artifact", "ceiling", "started"]
)
async def test_later_bootstrap_failure_requires_fresh_proof_and_retains_chain(
    pool, fault
):
    from harness_jobs.provider_retry import effective_step_key
    from harness_jobs.store import _record

    record, original, step = await source(pool, bootstrap=True)
    async with pool.acquire() as c:
        admission = dict(await c.fetchrow("SELECT * FROM harness_approval_consumption"))
        first = await recover(
            c, record, recovery_scope="unstarted-bootstrap", prove_not_started=boundary
        )
        lease = await acquire(
            c, operation_id=record.operation_id, holder="retry", attempt_id="retry"
        )
        current = _record(await c.fetchrow("SELECT * FROM harness_operations"))
    await OperationExecutor(lease, connect=pool.acquire).record_intent(
        idempotency_key=first["replacement_key"],
        provider=step.provider,
        operation_kind=step.operation_kind,
        target=step.target,
    )

    async def new_boundary(*args):
        proof = await boundary(*args)
        if fault != "reused-proof":
            proof["owner_evidence_id"] = proof["owner_evidence"]["record_id"] = (
                "new-protected-proof"
            )
        if fault == "changed-artifact":
            proof["source_artifact_sha256"] = "c" * 64
        if fault == "started":
            proof["bootstrap_mutations_started"] = True
        return proof

    async with pool.acquire() as c:
        await c.execute(
            "UPDATE harness_operation_leases SET "
            "expires_at=now()-interval '1 second',"
            "runtime_deadline=now()-interval '1 second'"
        )
        if fault == "ceiling":
            await c.execute("UPDATE harness_operation_leases SET attempts=max_attempts")
        before = dict(await c.fetchrow("SELECT * FROM harness_operation_leases"))
        args = dict(
            reauthorization_id="another-human-decision",
            recovery_scope="unstarted-bootstrap",
            prove_not_started=new_boundary,
        )
        if fault:
            with pytest.raises(OperationRefused):
                await recover(c, current, **args)
            assert (
                dict(await c.fetchrow("SELECT * FROM harness_operation_leases"))
                == before
            )
            assert (
                await c.fetchval("SELECT count(*) FROM harness_provider_retries") == 1
            )
            assert (
                await c.fetchval(
                    "SELECT stage FROM harness_provider_call_intent "
                    "WHERE idempotency_key=$1",
                    first["replacement_key"],
                )
                == "intended"
            )
        else:
            second = await recover(c, current, **args)
            assert second["original_key"] == first["replacement_key"]
            assert (
                dict(
                    await c.fetchrow(
                        "SELECT * FROM harness_provider_retries WHERE source_fence=$1",
                        original.fence_token,
                    )
                )
                == first
            )
            assert (
                await effective_step_key(c, current, step) == second["replacement_key"]
            )
            assert (
                await c.fetchval(
                    "SELECT count(*) FROM harness_provider_call_intent "
                    "WHERE stage='reconciled'"
                )
                == 2
            )
            third = await acquire(
                c, operation_id=record.operation_id, holder="third", attempt_id="third"
            )
            assert third.attempts == original.attempts + 2
            assert third.max_attempts == original.max_attempts
        assert (
            dict(await c.fetchrow("SELECT * FROM harness_approval_consumption"))
            == admission
        )


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "not-revoked",
        "created",
        "not-attempted",
        "owner",
        "event",
        "absence",
        "hash",
    ],
)
async def test_denied_first_grant_boundary_keeps_attempt_and_admission_history(
    pool, fault
):
    record, original, _ = await source(pool, bootstrap=True)

    async def prove(*args):
        proof = await boundary(*args)
        owner = proof["owner_evidence"]
        denial = {
            **{k: owner[k] for k in ("operation_id", "org_id", "workspace_id")},
            **{
                k: "d" * 64
                for k in (
                    "generation",
                    "claim",
                    "plan_sha256",
                    "cloudtrail_sha256",
                    "inventory_sha256",
                )
            },
            "event_ids": ["aws-denial"],
            "denied_request_verified": True,
            "absence_verified": True,
        }
        owner.update(
            evidence_kind="bootstrap-denied-first-grant-v1", denied_grant=dict(denial)
        )
        proof.update(
            evidence_kind=owner["evidence_kind"],
            bootstrap_mutations_started=True,
            bootstrap_grants_created=False,
            bootstrap_journal_revoked=True,
            denied_grant=denial,
        )
        if fault == "not-revoked":
            proof["bootstrap_journal_revoked"] = False
        elif fault == "created":
            proof["bootstrap_grants_created"] = True
        elif fault == "not-attempted":
            proof["bootstrap_mutations_started"] = False
        elif fault == "owner":
            owner["denied_grant"]["generation"] = "e" * 64
        elif fault == "event":
            denial["event_ids"] = []
            owner["denied_grant"]["event_ids"] = []
        elif fault == "absence":
            denial["absence_verified"] = owner["denied_grant"]["absence_verified"] = (
                False
            )
        elif fault == "hash":
            denial["plan_sha256"] = owner["denied_grant"]["plan_sha256"] = "bad"
        return proof

    async with pool.acquire() as c:
        before = dict(await c.fetchrow("SELECT * FROM harness_approval_consumption"))
        if fault:
            with pytest.raises(OperationRefused):
                await recover(
                    c,
                    record,
                    recovery_scope="unstarted-bootstrap",
                    prove_not_started=prove,
                )
            assert (
                await c.fetchval("SELECT count(*) FROM harness_provider_retries") == 0
            )
        else:
            receipt = await recover(
                c, record, recovery_scope="unstarted-bootstrap", prove_not_started=prove
            )
            assert json.loads(receipt["proof"])["bootstrap_mutations_started"] is True
        assert (
            await c.fetchval("SELECT attempts FROM harness_operation_leases")
            == original.attempts
        )
        assert (
            dict(await c.fetchrow("SELECT * FROM harness_approval_consumption"))
            == before
        )


@pytest.mark.parametrize(
    "fault",
    [None, "receipt", "digest", "journal", "source-fence", "attempted", "absent-label"],
)
async def test_retained_revocation_is_linked_to_actual_predecessor(pool, fault):
    import copy
    import hashlib

    from harness_jobs.provider_retry import effective_step_key
    from harness_jobs.store import _record

    record, original, step = await source(pool, bootstrap=True)

    async def denied(*args):
        proof = await boundary(*args)
        owner = proof["owner_evidence"]
        denial = {
            **{k: owner[k] for k in ("operation_id", "org_id", "workspace_id")},
            **{
                k: "d" * 64
                for k in (
                    "generation",
                    "claim",
                    "plan_sha256",
                    "cloudtrail_sha256",
                    "inventory_sha256",
                )
            },
            "event_ids": ["verified-denial"],
            "denied_request_verified": True,
            "absence_verified": True,
        }
        owner.update(
            evidence_kind="bootstrap-denied-first-grant-v1",
            denied_grant=copy.deepcopy(denial),
        )
        proof.update(
            evidence_kind=owner["evidence_kind"],
            bootstrap_mutations_started=True,
            bootstrap_grants_created=False,
            bootstrap_journal_revoked=True,
            denied_grant=denial,
        )
        return proof

    async with pool.acquire() as c:
        first = await recover(
            c, record, recovery_scope="unstarted-bootstrap", prove_not_started=denied
        )
        lease = await acquire(
            c, operation_id=record.operation_id, holder="next", attempt_id="next"
        )
        current = _record(await c.fetchrow("SELECT * FROM harness_operations"))
        admission = dict(await c.fetchrow("SELECT * FROM harness_approval_consumption"))
    await OperationExecutor(lease, connect=pool.acquire).record_intent(
        idempotency_key=first["replacement_key"],
        provider=step.provider,
        operation_kind=step.operation_kind,
        target=step.target,
    )

    async def retained(*args):
        proof = await boundary(*args)
        prior = json.loads(first["proof"])
        reference = {
            "reauthorization_id": first["reauthorization_id"],
            "source_fence": first["source_fence"],
            "proof_sha256": hashlib.sha256(
                json.dumps(prior, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
            "journal": {
                k: prior["denied_grant"][k]
                for k in (
                    "operation_id",
                    "org_id",
                    "workspace_id",
                    "generation",
                    "claim",
                    "plan_sha256",
                )
            },
        }
        if fault == "receipt":
            reference["reauthorization_id"] = "other"
        elif fault == "digest":
            reference["proof_sha256"] = "0" * 64
        elif fault == "journal":
            reference["journal"]["generation"] = "0" * 64
        elif fault == "source-fence":
            reference["source_fence"] = lease.fence_token
        proof["owner_evidence"].update(
            record_id="fresh-owner-proof",
            evidence_kind="bootstrap-retained-revocation-barrier-v1",
            retained_revocation=copy.deepcopy(reference),
        )
        proof.update(
            owner_evidence_id="fresh-owner-proof",
            evidence_kind=proof["owner_evidence"]["evidence_kind"],
            retained_revocation=reference,
        )
        if fault == "attempted":
            proof["bootstrap_mutations_started"] = True
        elif fault == "absent-label":
            proof["evidence_kind"] = proof["owner_evidence"]["evidence_kind"] = (
                "bootstrap-journal-barrier-v1"
            )
        return proof

    async with pool.acquire() as c:
        await c.execute(
            "UPDATE harness_operation_leases SET "
            "expires_at=now()-interval '1 hour',"
            "runtime_deadline=now()-interval '1 hour'"
        )
        kwargs = dict(
            reauthorization_id="retained-human-decision",
            recovery_scope="unstarted-bootstrap",
            prove_not_started=retained,
        )
        if fault:
            with pytest.raises(OperationRefused):
                await recover(c, current, **kwargs)
            assert (
                await c.fetchval("SELECT count(*) FROM harness_provider_retries") == 1
            )
        else:
            second = await recover(c, current, **kwargs)
            assert second["original_key"] == first["replacement_key"]
            assert (
                await effective_step_key(c, current, step) == second["replacement_key"]
            )
            third = await acquire(
                c, operation_id=record.operation_id, holder="last", attempt_id="last"
            )
            assert third.attempts == original.attempts + 2
            assert third.max_attempts == original.max_attempts
        assert (
            dict(await c.fetchrow("SELECT * FROM harness_approval_consumption"))
            == admission
        )
        assert (
            dict(
                await c.fetchrow(
                    "SELECT * FROM harness_provider_retries WHERE source_fence=$1",
                    first["source_fence"],
                )
            )
            == first
        )


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "actor",
        "count",
        "limit",
        "cap",
        "bool",
        "blank",
        "extra",
        "owner",
        "digest",
        "ancestor",
    ],
)
async def test_exhausted_retained_chain_extends_only_once_with_current_authorization(
    pool, fault, monkeypatch
):
    import copy
    import hashlib

    from harness_jobs.provider_retry import effective_step_key
    from harness_jobs.store import _record

    from .test_admission_postgres import principal

    record, _, step = await source(pool, bootstrap=True)
    previous = None
    originals = []

    def sha(value):
        return hashlib.sha256(
            json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    async def proof(operation, lease, call):
        value = await boundary(operation, lease, call)
        owner = value["owner_evidence"]
        owner["record_id"] = value["owner_evidence_id"] = f"owner-{lease['attempts']}"
        if previous is None:
            denial = {
                **{k: owner[k] for k in ("operation_id", "org_id", "workspace_id")},
                **{
                    k: "d" * 64
                    for k in (
                        "generation",
                        "claim",
                        "plan_sha256",
                        "cloudtrail_sha256",
                        "inventory_sha256",
                    )
                },
                "event_ids": ["denied"],
                "denied_request_verified": True,
                "absence_verified": True,
            }
            owner.update(
                evidence_kind="bootstrap-denied-first-grant-v1", denied_grant=denial
            )
            value.update(
                evidence_kind=owner["evidence_kind"],
                denied_grant=denial,
                bootstrap_mutations_started=True,
                bootstrap_grants_created=False,
                bootstrap_journal_revoked=True,
            )
        else:
            prior = json.loads(previous["proof"])
            journal = prior.get("retained_revocation", {}).get("journal") or {
                k: prior["denied_grant"][k]
                for k in (
                    "operation_id",
                    "org_id",
                    "workspace_id",
                    "generation",
                    "claim",
                    "plan_sha256",
                )
            }
            reference = {
                "reauthorization_id": previous["reauthorization_id"],
                "source_fence": previous["source_fence"],
                "proof_sha256": sha(prior),
                "journal": journal,
            }
            for target in (owner, value):
                target.update(
                    evidence_kind="bootstrap-retained-revocation-barrier-v1",
                    retained_revocation=reference,
                )
        if lease["attempts"] == 5:
            extension = {
                "authorized_by": principal().subject,
                "authorization": "Authorize five additional bootstrap attempts",
                "previous_limit": 5,
                "recorded_attempts": 5,
                "additional_attempts": 5,
            }
            if fault == "actor":
                extension["authorized_by"] = "other-human"
            if fault == "count":
                extension["recorded_attempts"] = 4
            if fault == "limit":
                extension["previous_limit"] = 4
            if fault == "cap":
                extension["additional_attempts"] = 6
            if fault == "bool":
                extension["additional_attempts"] = True
            if fault == "blank":
                extension["authorization"] = " "
            if fault == "extra":
                extension["unreviewed"] = True
            value["attempt_extension"] = extension
            owner["attempt_extension"] = copy.deepcopy(extension)
            if fault == "owner":
                owner["attempt_extension"]["authorization"] = "different"
            if fault == "digest":
                value["retained_revocation"]["proof_sha256"] = "0" * 64
        return value

    async with pool.acquire() as c:
        admission = dict(await c.fetchrow("SELECT * FROM harness_approval_consumption"))
    for attempt in range(1, 7):
        async with pool.acquire() as c:
            current = _record(await c.fetchrow("SELECT * FROM harness_operations"))
            before = dict(await c.fetchrow("SELECT * FROM harness_operation_leases"))
            assert before["attempts"] == attempt
            if attempt == 5 and fault == "ancestor":
                # Simulate a corrupted read of immutable history; the database
                # itself rejects updates to these retained receipts.
                from harness_jobs import provider_retry

                read_receipts = provider_retry.receipts_for

                async def broken_read(*args):
                    rows = [dict(row) for row in await read_receipts(*args)]
                    rows[0]["replacement_key"] = "broken-ancestor"
                    return rows

                monkeypatch.setattr(provider_retry, "receipts_for", broken_read)
            kwargs = dict(
                reauthorization_id=f"decision-{attempt}",
                recovery_scope="unstarted-bootstrap",
                prove_not_started=proof,
            )
            if attempt == 5 and fault:
                with pytest.raises(OperationRefused):
                    await recover(c, current, **kwargs)
                assert (
                    dict(await c.fetchrow("SELECT * FROM harness_operation_leases"))
                    == before
                )
                assert (
                    await c.fetchval("SELECT count(*) FROM harness_provider_retries")
                    == 4
                )
                return
            receipt = await recover(c, current, **kwargs)
            assert await recover(c, current, **kwargs) == receipt
            assert (
                await c.fetchval("SELECT attempts FROM harness_operation_leases")
                == attempt
            )
            assert await c.fetchval(
                "SELECT max_attempts FROM harness_operation_leases"
            ) == (10 if attempt >= 5 else 5)
            assert (
                await effective_step_key(c, current, step) == receipt["replacement_key"]
            )
            for original in originals:
                assert (
                    dict(
                        await c.fetchrow(
                            "SELECT * FROM harness_provider_retries "
                            "WHERE reauthorization_id=$1",
                            original["reauthorization_id"],
                        )
                    )
                    == original
                )
            assert (
                dict(await c.fetchrow("SELECT * FROM harness_approval_consumption"))
                == admission
            )
            lease = await acquire(
                c,
                operation_id=record.operation_id,
                holder=f"worker-{attempt + 1}",
                attempt_id=f"attempt-{attempt + 1}",
            )
        originals.append(receipt)
        previous = receipt
        await OperationExecutor(lease, connect=pool.acquire).record_intent(
            idempotency_key=receipt["replacement_key"],
            provider=step.provider,
            operation_kind=step.operation_kind,
            target=step.target,
        )
        async with pool.acquire() as c:
            await c.execute(
                "UPDATE harness_operation_leases SET "
                "expires_at=now()-interval '1 hour',"
                "runtime_deadline=now()-interval '1 hour'"
            )
    assert lease.attempts == 7 and lease.max_attempts == 10
    async with pool.acquire() as c:
        assert (
            await c.fetchval(
                "SELECT count(*) FROM harness_execution_audit "
                "WHERE event='execution-attempt-limit-extended'"
            )
            == 1
        )


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "lost-old",
        "changed-old",
        "extra",
        "receipt",
        "claimed-no-grants",
        "namespace",
        "absent-label",
        "foreign",
        "malformed",
    ],
)
async def test_revoked_prefix_receipt_chain_preserves_original_admission(pool, fault):
    import copy
    import hashlib

    from harness_jobs.provider_retry import active_call_rows, effective_step_key
    from harness_jobs.store import _record

    record, original, step = await source(pool, bootstrap=True)
    first = None

    async def revoked(*args):
        proof = await boundary(*args)
        owner = proof["owner_evidence"]
        journal = {
            **{k: owner[k] for k in ("operation_id", "org_id", "workspace_id")},
            **{
                k: ("c" if first else "d") * 64
                for k in (
                    "generation",
                    "claim",
                    "plan_sha256",
                    "revoked_progress_sha256",
                )
            },
            "cluster_arn": "arn:aws:eks:us-east-1:123456789012:cluster/fixture",
        }
        prior = json.loads(first["proof"]) if first else None
        journals = (
            copy.deepcopy(prior["revoked_bootstrap"]["journals"]) if prior else []
        )
        journals.append(journal)
        reference = {
            "journals": sorted(journals, key=lambda j: j["generation"]),
            "current_generation": journal["generation"],
            "predecessor": None
            if first is None
            else {
                "reauthorization_id": first["reauthorization_id"],
                "source_fence": first["source_fence"],
                "proof_sha256": hashlib.sha256(
                    json.dumps(prior, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest(),
            },
        }
        if first and fault == "lost-old":
            reference["journals"] = [journal]
        elif first and fault == "changed-old":
            reference["journals"][1]["plan_sha256"] = "e" * 64
        elif first and fault == "extra":
            reference["journals"].append({**journal, "generation": "f" * 64})
        elif first and fault == "receipt":
            reference["predecessor"]["proof_sha256"] = "0" * 64
        elif first and fault == "foreign":
            journal["workspace_id"] = "foreign"
        elif first and fault == "malformed":
            reference = None
        owner.update(
            evidence_kind="bootstrap-pre-namespace-revoked-v1",
            revoked_bootstrap=copy.deepcopy(reference),
        )
        if first:
            owner["record_id"] = "fresh-registrar-owner-proof"
            proof["owner_evidence_id"] = owner["record_id"]
        proof.update(
            evidence_kind=owner["evidence_kind"],
            revoked_bootstrap=reference,
            bootstrap_mutations_started=True,
            bootstrap_grants_created=True,
            bootstrap_journal_revoked=True,
            bootstrap_components_started=False,
        )
        if first and fault == "claimed-no-grants":
            proof["bootstrap_grants_created"] = False
        elif first and fault == "namespace":
            proof["bootstrap_components_started"] = True
        elif first and fault == "absent-label":
            proof["evidence_kind"] = owner["evidence_kind"] = (
                "bootstrap-journal-barrier-v1"
            )
            proof["bootstrap_mutations_started"] = False
        return proof

    async with pool.acquire() as c:
        admission = dict(await c.fetchrow("SELECT * FROM harness_approval_consumption"))
        first = await recover(
            c, record, recovery_scope="unstarted-bootstrap", prove_not_started=revoked
        )
        outcome = await c.fetchval("SELECT outcome FROM harness_provider_call_intent")
        assert "native revocation" in outcome and "created-grant" in outcome
        assert "absent protected mutation journals" not in outcome
        lease = await acquire(
            c, operation_id=record.operation_id, holder="next", attempt_id="next"
        )
        current = _record(await c.fetchrow("SELECT * FROM harness_operations"))
    await OperationExecutor(lease, connect=pool.acquire).record_intent(
        idempotency_key=first["replacement_key"],
        provider=step.provider,
        operation_kind=step.operation_kind,
        target=step.target,
    )
    async with pool.acquire() as c:
        await c.execute(
            "UPDATE harness_operation_leases SET expires_at=now()-interval '1 hour',"
            "runtime_deadline=now()-interval '1 hour'"
        )
        kwargs = dict(
            reauthorization_id="registrar-second-decision",
            recovery_scope="unstarted-bootstrap",
            prove_not_started=revoked,
        )
        if fault:
            with pytest.raises(OperationRefused):
                await recover(c, current, **kwargs)
            assert (
                await c.fetchval("SELECT count(*) FROM harness_provider_retries") == 1
            )
        else:
            second = await recover(c, current, **kwargs)
            assert second["original_key"] == first["replacement_key"]
            assert (
                await effective_step_key(c, current, step) == second["replacement_key"]
            )
            await active_call_rows(c, record.operation_id, [])
            next_lease = await acquire(
                c, operation_id=record.operation_id, holder="third", attempt_id="third"
            )
            assert next_lease.attempts == original.attempts + 2
            assert next_lease.max_attempts == original.max_attempts
        assert (
            dict(await c.fetchrow("SELECT * FROM harness_approval_consumption"))
            == admission
        )
