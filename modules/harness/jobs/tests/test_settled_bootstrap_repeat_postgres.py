"""Two native closures retain the first decision and permit one fresh successor."""

import copy
import json
from datetime import UTC, datetime, timedelta

import asyncpg
import pytest

from harness_jobs import settled_bootstrap, settled_bootstrap_repeat
from harness_jobs.execution import OperationExecutor
from harness_jobs.identity import OperationRefused, ResolvedPrincipal
from harness_jobs.leases import acquire
from harness_jobs.provider_retry import active_call_rows
from harness_jobs.recovery import sweep_scoped_expired_leases
from harness_jobs.store import _record

from .conftest import requires_postgres
from .test_bootstrap_retry_postgres import complete_acquisition_boundary
from .test_provider_retry_postgres import recover
from .test_settled_bootstrap_postgres import settled_source

pytestmark = requires_postgres


def journal_row(identity):
    return {
        **identity,
        "plan_json": json.dumps(identity["retained_plan"]),
        "progress_json": json.dumps(identity["retained_progress"]),
        "revoked": True,
    }


async def component_proof(operation, lease, call, closure, *, prior=None):
    value = await complete_acquisition_boundary(operation, lease, call)
    owner = value["owner_evidence"]
    owner["record_id"] += "-" + str(call["fence_token"])
    value["owner_evidence_id"] = owner["record_id"]
    for target in (value, owner):
        target.pop("attempt_extension")
    if prior is None:
        source = value["revoked_bootstrap"]["journals"][0]
        generation = source["generation"]
        grants = [
            {**g, "cluster_arn": source["cluster_arn"]}
            for g in source["acquisition_prefix"]
        ]
        progress = {
            g["key"]: {"phase": "revoked", "identity": {"uid": g["key"]}}
            for g in grants
        }
        progress["workspace-namespace"] = {
            "phase": "granted",
            "identity": {"uid": "retained-namespace", "generation": generation},
        }
        progress.update(
            phase="revoked",
            complete=True,
            component_inventory_complete=True,
            component_inventory_mode="management",
            components={
                str(i): {
                    "phase": "owned",
                    "creation": "a" * 32,
                    "identity": {"uid": str(i), "creation": "a" * 32},
                }
                for i in range(5)
            },
        )
        identity = {
            k: source[k]
            for k in (
                "operation_id",
                "org_id",
                "workspace_id",
                "cluster_arn",
                "generation",
                "claim",
            )
        }
        identity.update(
            retained_plan={"version": 1, "grants": grants}, retained_progress=progress
        )
        identity.update(
            plan_sha256=settled_bootstrap.digest(identity["retained_plan"]),
            revoked_progress_sha256=settled_bootstrap.digest(progress),
        )
        revoked = {
            "journals": [identity],
            "current_generation": generation,
            "predecessor": None,
        }
        kind = settled_bootstrap_repeat.ORIGINAL_COMPONENTS_KIND
    else:
        previous_journals = json.loads(prior["proof"])["revoked_bootstrap"]["journals"]
        previous = settled_bootstrap_repeat.component_anchor(closure)
        identity = copy.deepcopy(previous)
        identity.update(
            generation=settled_bootstrap.digest(call["fence_token"]),
            claim=settled_bootstrap.digest(call["attempt_id"]),
        )
        grants = identity["retained_plan"]["grants"]
        grants.extend(
            {"key": "reader-issuer-" + str(i), "cluster_arn": identity["cluster_arn"]}
            for i in range(4)
        )
        for grant in grants:
            grant["generation"] = identity["generation"]
        progress = identity["retained_progress"]
        progress["workspace-namespace"]["phase"] = "adopted"
        progress.update(
            {
                "reader-issuer-" + str(i): {
                    "phase": "revoked",
                    "identity": {"uid": "issuer-" + str(i)},
                }
                for i in range(4)
            }
        )
        identity.update(
            plan_sha256=settled_bootstrap.digest(identity["retained_plan"]),
            revoked_progress_sha256=settled_bootstrap.digest(progress),
        )
        revoked = {
            "journals": [*previous_journals, identity],
            "current_generation": identity["generation"],
            "predecessor": {
                "reauthorization_id": prior["reauthorization_id"],
                "source_fence": prior["source_fence"],
                "proof_sha256": settled_bootstrap.digest(json.loads(prior["proof"])),
            },
        }
        kind = settled_bootstrap_repeat.COMPONENTS_KIND
    for target in (value, owner):
        target.update(
            evidence_kind=kind,
            revoked_bootstrap=copy.deepcopy(revoked),
            settled_bootstrap=closure,
        )
    value.update(
        bootstrap_components_retained=True,
        bootstrap_admission_started=False,
        runtime_amendment={
            "version": 1,
            "kind": "bootstrap-runtime-reauthorization-v1",
            "original_max_runtime_seconds": 10800,
            "max_runtime_seconds": 10800,
        },
    )
    return value


@pytest.fixture
async def repeated(pool):
    first_record, _, step = await settled_source(pool, 10800)
    deadline = datetime.now(UTC) + timedelta(hours=3)

    async def authorize(*_):
        return deadline

    async with pool.acquire() as connection:

        async def first_proof(operation, lease, call):
            closure = await settled_bootstrap.read(connection, operation, lease, call)
            proof = await component_proof(operation, lease, call, closure)
            proof["runtime_authority_expires_at"] = deadline.isoformat()
            return proof

        first = await recover(
            connection,
            first_record,
            recovery_scope="unstarted-bootstrap",
            settled_bootstrap=True,
            authorize=authorize,
            prove_not_started=first_proof,
        )
        first_settlement = dict(
            await connection.fetchrow("SELECT * FROM harness_recovery_settlements")
        )
        paid = dict(
            await connection.fetchrow("SELECT * FROM harness_approval_consumption")
        )
        lease = await acquire(
            connection,
            operation_id=first_record.operation_id,
            holder="second-worker",
            attempt_id="second-worker",
        )
        current = _record(await connection.fetchrow("SELECT * FROM harness_operations"))
    await OperationExecutor(lease, connect=pool.acquire).record_intent(
        idempotency_key=first["replacement_key"],
        provider=step.provider,
        operation_kind=step.operation_kind,
        target=step.target,
    )

    async def retained(connection, claim, result):
        await connection.execute(
            "INSERT INTO harness_recovery_settlements("
            "receipt_id,operation_id,org_id,workspace_id,job_id,attempt_id,"
            "claim_holder,claim_attempt_id,claim_fence_token,"
            "payload_digest,accounting) "
            "VALUES('second-settlement',$1,$2,$3,$4,$5,$6,$7,$8,$9,$10::jsonb)",
            current.operation_id,
            current.org_id,
            current.workspace_id,
            current.job_id,
            current.attempt_id,
            claim.holder,
            claim.attempt_id,
            claim.fence_token,
            "b" * 64,
            json.dumps(
                {"action": "unknown", "budget": "retain", "release_permitted": False}
            ),
        )

    async with pool.acquire() as connection:
        await connection.execute(
            "UPDATE harness_operation_leases SET "
            "expires_at=now()-interval '1 second',"
            "runtime_deadline=now()-interval '1 second'"
        )
        result = await sweep_scoped_expired_leases(
            connection,
            principal=ResolvedPrincipal(
                current.org_id,
                current.workspace_id,
                "second-observer",
                frozenset({"workspace:recover"}),
            ),
            max_reconcile_attempts=1,
            on_settled=retained,
        )
        assert result.results[0].action == "unknown"
        await connection.execute(
            "UPDATE harness_recovery_settlements SET delivered_at=clock_timestamp() "
            "WHERE receipt_id='second-settlement'"
        )
        operation = dict(await connection.fetchrow("SELECT * FROM harness_operations"))
        lease = dict(
            await connection.fetchrow("SELECT * FROM harness_operation_leases")
        )
        calls = await active_call_rows(
            connection,
            operation["operation_id"],
            await connection.fetch("SELECT * FROM harness_provider_call_intent"),
        )
        closure = await settled_bootstrap.read(connection, operation, lease, calls[0])
    return first, first_settlement, paid, operation, lease, dict(calls[0]), closure


async def test_two_native_settlements_reopen_once_without_replacing_paid_history(
    pool, repeated
):
    first, first_settlement, paid, operation, lease, call, closure = repeated
    assert closure["kind"] == settled_bootstrap_repeat.KIND
    assert closure["predecessor"] == settled_bootstrap.snapshot(first)
    deadline = datetime.now(UTC) + timedelta(hours=3)

    async def authorize(*_):
        return deadline

    async def prove(*_):
        value = await component_proof(operation, lease, call, closure, prior=first)
        value["runtime_authority_expires_at"] = deadline.isoformat()
        return value

    async with pool.acquire() as connection:
        args = dict(
            reauthorization_id="second-human-decision",
            recovery_scope="unstarted-bootstrap",
            settled_bootstrap=True,
            authorize=authorize,
            prove_not_started=prove,
        )
        second = await recover(connection, _record(operation), **args)
        assert await recover(connection, _record(operation), **args) == second
        assert (
            await connection.fetchval("SELECT count(*) FROM harness_provider_retries")
            == 2
        )
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM harness_recovery_settlements"
            )
            == 2
        )
        assert (
            dict(
                await connection.fetchrow(
                    "SELECT * FROM harness_provider_retries "
                    "WHERE reauthorization_id=$1",
                    first["reauthorization_id"],
                )
            )
            == first
        )
        assert (
            dict(
                await connection.fetchrow(
                    "SELECT * FROM harness_recovery_settlements WHERE receipt_id=$1",
                    first_settlement["receipt_id"],
                )
            )
            == first_settlement
        )
        assert (
            dict(
                await connection.fetchrow("SELECT * FROM harness_approval_consumption")
            )
            == paid
        )
        fresh = await acquire(
            connection,
            operation_id=operation["operation_id"],
            holder="third-worker",
            attempt_id="third-worker",
        )
        assert fresh.attempts == lease["attempts"] + 1
        assert fresh.max_attempts == lease["max_attempts"]
        assert fresh.runtime_deadline == deadline


@pytest.mark.parametrize(
    "fault",
    [
        "prior-proof",
        "prior-key",
        "prior-version",
        "current-key",
        "attempt-reset",
        "ceiling",
        "old-settlement",
        "undelivered",
        "prior-journal",
    ],
)
async def test_repeated_closure_refuses_changed_chain(repeated, fault):
    value = copy.deepcopy(repeated[-1])
    prior = value["predecessor"]
    if fault in {"prior-proof", "prior-journal"}:
        proof = json.loads(prior["proof"])
        if fault == "prior-proof":
            proof["settled_bootstrap"]["call"]["stage"] = "reconciled"
        else:
            proof["revoked_bootstrap"]["journals"][0]["retained_progress"][
                "components"
            ]["0"]["identity"]["uid"] = "changed"
        prior["proof"] = json.dumps(proof)
    elif fault == "prior-key":
        prior["replacement_key"] = "foreign"
    elif fault == "prior-version":
        prior["restored_version"] += 1
    elif fault == "current-key":
        value["call"]["idempotency_key"] = "foreign"
    elif fault == "attempt-reset":
        value["lease"]["attempts"] -= 1
    elif fault == "ceiling":
        value["lease"]["max_attempts"] += 1
    elif fault == "old-settlement":
        value["settlement"]["receipt_id"] = json.loads(prior["proof"])[
            "settled_bootstrap"
        ]["settlement"]["receipt_id"]
    else:
        value["settlement"]["delivered_at"] = None
    with pytest.raises(OperationRefused):
        settled_bootstrap.validate(value)


@pytest.fixture
async def third_closed(pool, repeated):
    first, _, _, operation, lease, call, closure = repeated
    deadline = datetime.now(UTC) + timedelta(hours=3)

    async def authorize(*_):
        return deadline

    async def prove(*_):
        value = await component_proof(operation, lease, call, closure, prior=first)
        value["runtime_authority_expires_at"] = deadline.isoformat()
        return value

    async with pool.acquire() as connection:
        second = await recover(
            connection,
            _record(operation),
            reauthorization_id="second-human-decision",
            recovery_scope="unstarted-bootstrap",
            settled_bootstrap=True,
            authorize=authorize,
            prove_not_started=prove,
        )
        granted = await acquire(
            connection,
            operation_id=operation["operation_id"],
            holder="third-worker",
            attempt_id="third-worker",
        )
        current = _record(await connection.fetchrow("SELECT * FROM harness_operations"))
    await OperationExecutor(granted, connect=pool.acquire).record_intent(
        idempotency_key=second["replacement_key"],
        provider=call["provider"],
        operation_kind=call["operation_kind"],
        target=call["target"],
    )

    async def retained(connection, claim, result):
        await connection.execute(
            "INSERT INTO "
            "harness_recovery_settlements(receipt_id,operation_id,org_id,"
            "workspace_id,job_id,attempt_id,claim_holder,claim_attempt_id,"
            "claim_fence_token,payload_digest,accounting) "
            "VALUES('third-settlement',$1,$2,$3,$4,$5,$6,$7,$8,$9,$10::jsonb)",
            current.operation_id,
            current.org_id,
            current.workspace_id,
            current.job_id,
            current.attempt_id,
            claim.holder,
            claim.attempt_id,
            claim.fence_token,
            "c" * 64,
            json.dumps(
                {"action": "unknown", "budget": "retain", "release_permitted": False}
            ),
        )

    async with pool.acquire() as connection:
        await connection.execute(
            "UPDATE harness_operation_leases SET "
            "expires_at=now()-interval '1 second', "
            "runtime_deadline=now()-interval '1 second'"
        )
        result = await sweep_scoped_expired_leases(
            connection,
            principal=ResolvedPrincipal(
                current.org_id,
                current.workspace_id,
                "third-observer",
                frozenset({"workspace:recover"}),
            ),
            max_reconcile_attempts=1,
            on_settled=retained,
        )
        assert result.results[0].action == "unknown"
        await connection.execute(
            "UPDATE harness_recovery_settlements SET "
            "delivered_at=clock_timestamp() WHERE "
            "receipt_id='third-settlement'"
        )
        operation = dict(await connection.fetchrow("SELECT * FROM harness_operations"))
        lease = dict(
            await connection.fetchrow("SELECT * FROM harness_operation_leases")
        )
        calls = await active_call_rows(
            connection,
            operation["operation_id"],
            await connection.fetch("SELECT * FROM harness_provider_call_intent"),
        )
        closure = await settled_bootstrap.read(connection, operation, lease, calls[0])
    return operation, lease, dict(calls[0]), closure, second


@pytest.mark.parametrize(
    "fault",
    [None, "ancestor-receipt", "ancestor-settlement", "partial-uid", "invented-marker"],
)
async def test_three_native_closures_retain_partial_roster_and_all_history(
    pool, third_closed, fault
):
    operation, lease, call, closure, prior = third_closed
    assert len(settled_bootstrap_repeat.lineage(closure)) == 2
    value = await component_proof(operation, lease, call, closure, prior=prior)
    for target in (value, value["owner_evidence"]):
        current = next(
            j
            for j in target["revoked_bootstrap"]["journals"]
            if j["generation"] == target["revoked_bootstrap"]["current_generation"]
        )
        progress = current["retained_progress"]
        current["inherited_components"] = copy.deepcopy(progress["components"])
        progress["components"] = dict(list(progress["components"].items())[:2])
        progress.pop("component_inventory_complete")
        progress.pop("component_inventory_mode")
        progress["prerequisite_inventory"] = {
            "retained": "tested in native Gateway suite"
        }
        if fault == "partial-uid":
            next(iter(progress["components"].values()))["identity"]["uid"] = "foreign"
        if fault == "invented-marker":
            progress["component_inventory_complete"] = True
        current["revoked_progress_sha256"] = settled_bootstrap.digest(progress)
    if fault in {"partial-uid", "invented-marker"}:
        assert not settled_bootstrap_repeat.recoverable(value)
        return
    async with pool.acquire() as connection:
        if fault:
            statement = (
                "UPDATE harness_provider_retries SET "
                "source_attempt_id='foreign' WHERE "
                "source_fence=(SELECT min(source_fence) FROM "
                "harness_provider_retries)"
                if fault == "ancestor-receipt"
                else (
                    "UPDATE harness_recovery_settlements SET delivered_at=NULL "
                    "WHERE receipt_id NOT IN "
                    "('third-settlement','second-settlement')"
                )
            )
            with pytest.raises(asyncpg.RaiseError, match="immutable"):
                await connection.execute(statement)
            assert (
                await settled_bootstrap.read(connection, operation, lease, call)
                == closure
            )
            changed = copy.deepcopy(closure)
            previous = json.loads(changed["predecessor"]["proof"])
            if fault == "ancestor-receipt":
                previous["settled_bootstrap"]["predecessor"]["source_attempt_id"] = (
                    "foreign"
                )
            else:
                previous["settled_bootstrap"]["settlement"]["delivered_at"] = None
            changed["predecessor"]["proof"] = json.dumps(previous)
            with pytest.raises(OperationRefused):
                settled_bootstrap.validate(changed)
            return
        before = [
            dict(r)
            for r in await connection.fetch(
                "SELECT * FROM harness_provider_retries ORDER BY source_fence"
            )
        ]
        deadline = datetime.now(UTC) + timedelta(hours=3)

        async def authorize(*_):
            return deadline

        async def prove(*_):
            return {**value, "runtime_authority_expires_at": deadline.isoformat()}

        await recover(
            connection,
            _record(operation),
            reauthorization_id="third-human-decision",
            recovery_scope="unstarted-bootstrap",
            settled_bootstrap=True,
            authorize=authorize,
            prove_not_started=prove,
        )
        after = [
            dict(r)
            for r in await connection.fetch(
                "SELECT * FROM harness_provider_retries ORDER BY source_fence"
            )
        ]
        assert len(after) == 3 and after[:2] == before
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM harness_recovery_settlements"
            )
            == 3
        )
        fresh = await acquire(
            connection,
            operation_id=operation["operation_id"],
            holder="fourth-worker",
            attempt_id="fourth-worker",
        )
        assert (
            fresh.attempts == lease["attempts"] + 1
            and fresh.max_attempts == lease["max_attempts"]
        )
