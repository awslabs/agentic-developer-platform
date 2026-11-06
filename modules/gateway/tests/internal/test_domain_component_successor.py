# ruff: noqa: F811 -- imported real PostgreSQL fixtures are injected by name.
"""The second component journal binds native closures and its exact predecessor."""

import copy
import hashlib
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from harness_jobs.execution import OperationExecutor
from harness_jobs.execution_plan import admitted_steps
from harness_jobs.identity import ResolvedPrincipal
from harness_jobs.leases import acquire
from harness_jobs.provider_retry import active_call_rows
from harness_jobs.recovery import sweep_scoped_expired_leases
from harness_jobs.settled_bootstrap import digest, read
from harness_jobs.settled_bootstrap_repeat import journals_bound
from harness_jobs.store import _record
from superplane_bootstrap.grant_plan import BootstrapRelease, compile_grants
from superplane_bootstrap.kube_grants import _digest

from src.internal import domain_bootstrap_recovery as bootstrap
from src.internal.domain_operation_dispatch import paid_operation
from src.internal.domain_operation_store import binding_for
from src.shared.domain_bootstrap_boundary import qualified_source
from src.shared.domain_bootstrap_components_repeat import KIND, transition
from src.shared.domain_bootstrap_evidence import record_id
from src.shared.domain_bootstrap_recovery import validate
from tests.internal.test_domain_settled_bootstrap import bootstrap_interrupted as bootstrap_interrupted
from tests.internal.test_domain_settled_bootstrap import denied_bootstrap as denied_bootstrap
from tests.internal.test_domain_settled_bootstrap import kubernetes as kubernetes
from tests.internal.test_domain_settled_bootstrap import paid as paid
from tests.internal.test_domain_settled_bootstrap import pg_server as pg_server
from tests.internal.test_domain_settled_bootstrap import pg_url as pg_url
from tests.internal.test_domain_settled_bootstrap import recovery as recovery
from tests.internal.test_domain_settled_bootstrap import settled_bootstrap as settled_bootstrap
from tests.internal.test_domain_settled_bootstrap import store as store
from tests.internal.test_domain_settled_bootstrap import unstarted_bootstrap as unstarted_bootstrap


def successor_row(old, *, claim="f" * 64):
    row = copy.deepcopy(old)
    row.update(claim=claim)
    row["generation"] = hashlib.sha256((row["operation_id"] + ":" + row["claim"]).encode()).hexdigest()
    previous = json.loads(old["progress_json"])
    old_plan = json.loads(old["plan_json"])
    target = SimpleNamespace(
        account_id=row["cluster_arn"].split(":")[4], cluster_arn=row["cluster_arn"], workspace_id=row["workspace_id"], is_adopted=False
    )
    journal = SimpleNamespace(target=target, generation=row["generation"], original_allocation_id=old_plan["grants"][17]["original_allocation_id"])
    plan = compile_grants(
        journal,
        BootstrapRelease(
            "superplane-system",
            "superplane-controller",
            "superplane-controller",
            "v1.35",
            ("nodepools.superplane.ai", "superplanenodes.superplane.ai"),
            reader_renewal=True,
        ),
        {actor: f"arn:aws:iam::{target.account_id}:role/{actor}" for actor in ("registrar", "installer", "supervisor")},
    )
    progress = copy.deepcopy(previous)
    for spec in plan["grants"]:
        if spec["key"] == "workspace-namespace":
            progress[spec["key"]]["phase"] = "adopted"
            continue
        if spec["kind"].startswith("eks-"):
            identity = {k: spec[k] for k in ("generation", "groups", "username")}
            identity["arn"] = row["cluster_arn"].replace(":cluster/", ":access-entry/") + f"/role/{target.account_id}/{spec['actor']}/successor"
            if spec["kind"] == "eks-policy":
                identity.update(policy_arn=spec["policy_arn"], scope=spec["scope"], associated_at="2026-10-01T00:00:00Z")
        else:
            identity = {"uid": spec["key"] + "-next", "generation": row["generation"], "digest": _digest(spec["body"])}
        progress[spec["key"]] = {"phase": "revoked", "identity": identity}
    progress["management_observation"]["registration_claim"] = row["claim"]
    progress["system_workload_baseline"]["generation"] = row["generation"]
    next(p for p in progress["prerequisite_inventory"]["prerequisites"] if p["kind"] == "EksAccessEntry")["identifier"] = progress[
        "supervisor-entry"
    ]["identity"]["arn"]
    row.update(plan_json=json.dumps(plan), progress_json=json.dumps(progress))
    return row


@pytest.fixture
async def successor(settled_bootstrap):
    paid, body, evidence = settled_bootstrap
    result = await paid.post("/recover-settled-bootstrap", body, role="producer")
    assert result.status_code == 200, result.text
    async with paid.connect() as connection:
        prior = dict(await connection.fetchrow("SELECT * FROM harness_provider_retries"))
        original = dict(await connection.fetchrow("SELECT * FROM workspace_bootstrap_authority"))
        row = successor_row(original)
        await connection.execute(
            "INSERT INTO workspace_bootstrap_authority VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9)",
            *[
                row[k]
                for k in ("workspace_id", "operation_id", "org_id", "cluster_arn", "generation", "claim", "revoked", "plan_json", "progress_json")
            ],
        )
        granted = await acquire(connection, operation_id=body["operation_id"], holder="second-worker", attempt_id="second-worker")
        record = _record(await connection.fetchrow("SELECT * FROM harness_operations"))
    step = admitted_steps(record)[0]
    await OperationExecutor(granted, connect=paid.connect).record_intent(
        idempotency_key=prior["replacement_key"],
        provider=step.provider,
        operation_kind=step.operation_kind,
        target=step.target,
    )

    async def retained(connection, claim, result):
        accounting = {
            "action": "unknown",
            "budget": "retain",
            "release_permitted": False,
            "partial_lifecycle": {
                "phase": "bootstrap-workspace",
                "workflow_complete": False,
                "source_artifact_id": step.target,
                "authority_generations": [
                    {"generation": j["generation"], "claim": j["claim"], "plan_sha256": digest(j["plan_json"])} for j in (original, row)
                ],
            },
        }
        await connection.execute(
            "INSERT INTO harness_recovery_settlements(receipt_id,operation_id,org_id,workspace_id,job_id,attempt_id,"
            "claim_holder,claim_attempt_id,claim_fence_token,payload_digest,accounting) "
            "VALUES('second-close',$1,$2,$3,$4,$5,$6,$7,$8,$9,$10::jsonb)",
            record.operation_id,
            record.org_id,
            record.workspace_id,
            record.job_id,
            record.attempt_id,
            claim.holder,
            claim.attempt_id,
            claim.fence_token,
            digest(accounting),
            json.dumps(accounting),
        )

    async with paid.connect() as connection:
        await connection.execute("UPDATE harness_operation_leases SET expires_at=now()-interval '1 hour',runtime_deadline=now()-interval '1 hour'")
        result = await sweep_scoped_expired_leases(
            connection,
            principal=ResolvedPrincipal(record.org_id, record.workspace_id, "second-observer", frozenset({"workspace:recover"})),
            max_reconcile_attempts=1,
            on_settled=retained,
        )
        assert result.results[0].action == "unknown"
        await connection.execute("UPDATE harness_recovery_settlements SET delivered_at=clock_timestamp() WHERE receipt_id='second-close'")
        operation = dict(await connection.fetchrow("SELECT * FROM harness_operations"))
        lease = dict(await connection.fetchrow("SELECT * FROM harness_operation_leases"))
        calls = await active_call_rows(connection, operation["operation_id"], await connection.fetch("SELECT * FROM harness_provider_call_intent"))
        closure = await read(connection, operation, lease, calls[0])
    binding = binding_for("superplane", body["org_id"])
    operation = await paid_operation(binding, body["operation_id"])
    observed = await bootstrap.registrar_snapshot(binding, operation, calls[0]["idempotency_key"], calls[0]["fence_token"], reader_components=True)
    current = journals_bound(closure, observed["journals"])
    assert current["generation"] == row["generation"]
    image = "qualified@sha256:c1c26cc85a737b818b7d2b70483de8650483d0aa6edc18be1a80728c6f5e9a6c"
    source = qualified_source(image)
    value = {
        **evidence,
        "evidence_kind": KIND,
        "settled_bootstrap": closure,
        "source_key": calls[0]["idempotency_key"],
        "source_fence": calls[0]["fence_token"],
        "source_principal": "second-worker",
        "source_acquired_at": closure["source_audit"]["recorded_at"],
        "source_image": image,
        "source_revision": source["revision"],
        "source_hashes": source["hashes"],
        "revoked_bootstrap": observed["revoked_bootstrap"],
    }
    value["record_id"] = record_id(value)
    return paid, binding, operation, dict(calls[0]), original, row, value


@pytest.mark.parametrize("settled_bootstrap", ["components-same-cap"], indirect=True)
@pytest.mark.parametrize(
    "fault",
    [
        None,
        "old-journal",
        "component",
        "extra-component",
        "retain-marker",
        "generation",
        "still-granted",
        "artifact",
        "effect",
        "membership",
        "result",
    ],
)
async def test_successor_snapshot_refuses_changed_ownership_or_history(successor, fault):
    paid, binding, operation, call, old, row, evidence = successor
    assert validate(evidence) is evidence
    async with paid.connect() as connection:
        changed = copy.deepcopy(old if fault == "old-journal" else row)
        progress = json.loads(changed["progress_json"])
        if fault in {"old-journal", "component"}:
            next(iter(progress["components"].values()))["identity"]["uid"] = "changed"
        elif fault == "extra-component":
            progress["components"]["foreign"] = {}
        elif fault == "retain-marker":
            progress["retain_workspace"] = False
        elif fault == "still-granted":
            progress["supervisor-entry"]["phase"] = "granted"
        elif fault == "generation":
            progress["management_observation"]["registration_claim"] = "foreign"
        elif fault == "artifact":
            await connection.execute("UPDATE workspace_lifecycle_artifacts SET artifact_metadata_json='{}'")
        elif fault in {"effect", "membership"}:
            table = "workspace_lifecycle_effects" if fault == "effect" else "cluster_memberships"
            await connection.execute("INSERT INTO " + table + " VALUES($1)", operation["workspace_id"])
        elif fault == "result":
            await connection.execute("INSERT INTO workspace_lifecycle_artifacts(source_operation_id) VALUES($1)", operation["operation_id"])
        if fault not in {None, "artifact", "effect", "membership", "result"}:
            await connection.execute(
                "UPDATE workspace_bootstrap_authority SET progress_json=$1 WHERE generation=$2", json.dumps(progress), changed["generation"]
            )

    async def inspect():
        return await bootstrap.registrar_snapshot(binding, operation, call["idempotency_key"], call["fence_token"], reader_components=True)

    if fault:
        with pytest.raises((ValueError, KeyError, HTTPException)):
            await inspect()
    else:
        observed = await inspect()
        assert observed["journals"] == sorted([old, row], key=lambda j: j["generation"])
        assert journals_bound(evidence["settled_bootstrap"], observed["journals"]) == row
        prior = next(j for j in evidence["revoked_bootstrap"]["journals"] if j["generation"] == old["generation"])
        transition(row, prior, complete=True)


@pytest.mark.parametrize("settled_bootstrap", ["components-same-cap"], indirect=True)
@pytest.mark.parametrize("fault", [None, "owner", "proof", "source", "journal"])
async def test_approved_assumption_stays_bound_to_real_successor(successor, monkeypatch, fault):
    from harness_jobs import bootstrap_provenance_assumption as assumption

    _, _, _, _, _, _, evidence = successor
    current = next(j for j in evidence["revoked_bootstrap"]["journals"] if j["generation"] == evidence["revoked_bootstrap"]["current_generation"])
    monkeypatch.setattr(assumption, "CLOSURE_SHA256", digest(evidence["settled_bootstrap"]))
    monkeypatch.setattr(assumption, "SOURCE_BINDING_SHA256", digest({k: evidence[k] for k in assumption.SOURCE_FIELDS}))
    monkeypatch.setattr(assumption, "JOURNAL_BINDING_SHA256", digest({k: current[k] for k in assumption.JOURNAL_FIELDS}))
    metadata = assumption.metadata(assumption.CLOSURE_SHA256)
    evidence[assumption.FIELD] = metadata
    evidence["source_audit"][assumption.FIELD] = metadata
    proof = {"owner_evidence": evidence, assumption.FIELD: metadata}
    if fault == "owner":
        evidence.pop(assumption.FIELD)
    elif fault == "proof":
        proof.pop(assumption.FIELD)
    elif fault == "source":
        evidence["source_pod_uid"] = "different-pod"
    elif fault == "journal":
        current["claim"] = "different-claim"
    if fault:
        with pytest.raises(ValueError):
            assumption.validate_proof(proof)
    else:
        assert validate(evidence) is evidence
        assert assumption.validate_proof(proof) == metadata
        assert metadata["executed_stdin_verified"] is False


@pytest.mark.parametrize("settled_bootstrap", ["components-same-cap"], indirect=True)
@pytest.mark.parametrize("fault", [None, "source", "uid", "empty", "extra", "complete-marker", "baseline-marker", "inherited-uid"])
async def test_partial_reader_preserves_only_actual_markers_and_qualified_roster(successor, fault):
    from src.shared.domain_bootstrap_components_repeat import validate as validate_reader

    _, _, _, _, old, row, value = successor
    progress = json.loads(row["progress_json"])
    progress["components"] = dict(list(progress["components"].items())[:2])
    for key in ("component_inventory_complete", "component_inventory_mode", "management_observation", "system_workload_baseline"):
        progress.pop(key)
    if fault == "uid":
        next(iter(progress["components"].values()))["identity"]["uid"] = "foreign"
    elif fault == "empty":
        progress["components"] = {}
    elif fault == "extra":
        progress["components"]["foreign"] = {}
    elif fault == "complete-marker":
        progress["component_inventory_complete"] = True
    elif fault == "baseline-marker":
        progress["system_workload_baseline"] = {}
    row["progress_json"] = json.dumps(progress)
    previous = next(j for j in value["revoked_bootstrap"]["journals"] if j["generation"] == old["generation"])
    if fault not in {None, "source", "inherited-uid"}:
        with pytest.raises((KeyError, ValueError)):
            transition(row, previous, complete=True)
        return
    identity = transition(row, previous, complete=True)
    value["revoked_bootstrap"]["journals"] = sorted([previous, identity], key=lambda j: j["generation"])
    if fault != "source":
        value["source_image"] = "qualified@sha256:12be55191d1a95e3d116e54c3651638b1c4132ddea2c98e745505053a2e58bf9"
        source = qualified_source(value["source_image"])
        value.update(source_revision=source["revision"], source_hashes=source["hashes"])
    if fault == "inherited-uid":
        next(iter(identity["inherited_components"].values()))["identity"]["uid"] = "foreign"
    if fault:
        with pytest.raises(ValueError):
            validate_reader(value)
    else:
        assert validate_reader(value) == identity
        assert len(identity["inherited_components"]) == 5 and len(identity["retained_progress"]["components"]) == 2
        assert set(progress) - {g["key"] for g in identity["retained_plan"]["grants"]} == {
            "phase",
            "complete",
            "components",
            "prerequisite_inventory",
        }


@pytest.mark.parametrize("settled_bootstrap", ["components-same-cap"], indirect=True)
async def test_three_generation_native_snapshot_uses_oldest_complete_anchor(successor):
    from datetime import UTC, datetime, timedelta

    from harness_jobs.provider_retry import recover_uncredentialed
    from harness_jobs.settled_bootstrap_repeat import component_anchor

    paid, binding, operation, call, original, previous_row, evidence = successor
    deadline = datetime.now(UTC) + timedelta(hours=3)
    async with paid.connect() as connection:
        first = dict(await connection.fetchrow("SELECT * FROM harness_provider_retries"))
    proof = json.loads(first["proof"])
    proof.update(
        evidence_kind=KIND,
        owner_evidence=evidence,
        owner_evidence_id=evidence["record_id"],
        revoked_bootstrap=evidence["revoked_bootstrap"],
        settled_bootstrap=evidence["settled_bootstrap"],
        runtime_authority_expires_at=deadline.isoformat(),
    )

    async def authorize(*_):
        return deadline

    async def prove(*_):
        return proof

    async with paid.connect() as connection:
        receipt = await recover_uncredentialed(
            connection,
            principal=ResolvedPrincipal(operation["org_id"], operation["workspace_id"], operation["requester"], frozenset({"workspace:provision"})),
            operation_id=operation["operation_id"],
            expected_version=operation["version"],
            reauthorization_id="second-native-test-decision",
            recovery_scope="unstarted-bootstrap",
            settled_bootstrap=True,
            authorize=authorize,
            prove_not_started=prove,
        )
        row = successor_row(original, claim="9" * 64)
        progress = json.loads(row["progress_json"])
        progress["components"] = dict(list(progress["components"].items())[:2])
        for key in ("component_inventory_complete", "component_inventory_mode", "management_observation", "system_workload_baseline"):
            progress.pop(key)
        row["progress_json"] = json.dumps(progress)
        await connection.execute(
            "INSERT INTO workspace_bootstrap_authority VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9)",
            *[
                row[k]
                for k in ("workspace_id", "operation_id", "org_id", "cluster_arn", "generation", "claim", "revoked", "plan_json", "progress_json")
            ],
        )
        granted = await acquire(connection, operation_id=operation["operation_id"], holder="third-worker", attempt_id="third-worker")
        record = _record(await connection.fetchrow("SELECT * FROM harness_operations"))
    await OperationExecutor(granted, connect=paid.connect).record_intent(
        idempotency_key=receipt["replacement_key"], provider=call["provider"], operation_kind=call["operation_kind"], target=call["target"]
    )

    async def retained(connection, claim, result):
        accounting = {
            "action": "unknown",
            "budget": "retain",
            "release_permitted": False,
            "partial_lifecycle": {
                "phase": "bootstrap-workspace",
                "workflow_complete": False,
                "source_artifact_id": call["target"],
                "authority_generations": [
                    {"generation": j["generation"], "claim": j["claim"], "plan_sha256": digest(j["plan_json"])} for j in (original, previous_row, row)
                ],
            },
        }
        await connection.execute(
            "INSERT INTO harness_recovery_settlements(receipt_id,operation_id,org_id,workspace_id,job_id,attempt_id,"
            "claim_holder,claim_attempt_id,claim_fence_token,payload_digest,accounting) "
            "VALUES('third-close',$1,$2,$3,$4,$5,$6,$7,$8,$9,$10::jsonb)",
            record.operation_id,
            record.org_id,
            record.workspace_id,
            record.job_id,
            record.attempt_id,
            claim.holder,
            claim.attempt_id,
            claim.fence_token,
            digest(accounting),
            json.dumps(accounting),
        )

    async with paid.connect() as connection:
        await connection.execute("UPDATE harness_operation_leases SET expires_at=now()-interval '1 hour',runtime_deadline=now()-interval '1 hour'")
        result = await sweep_scoped_expired_leases(
            connection,
            principal=ResolvedPrincipal(record.org_id, record.workspace_id, "third-observer", frozenset({"workspace:recover"})),
            max_reconcile_attempts=1,
            on_settled=retained,
        )
        assert result.results[0].action == "unknown"
        await connection.execute("UPDATE harness_recovery_settlements SET delivered_at=clock_timestamp() WHERE receipt_id='third-close'")
        op = dict(await connection.fetchrow("SELECT * FROM harness_operations"))
        lease = dict(await connection.fetchrow("SELECT * FROM harness_operation_leases"))
        calls = await active_call_rows(connection, op["operation_id"], await connection.fetch("SELECT * FROM harness_provider_call_intent"))
        closure = await read(connection, op, lease, calls[0])
    op = await paid_operation(binding, op["operation_id"])
    observed = await bootstrap.registrar_snapshot(binding, op, calls[0]["idempotency_key"], calls[0]["fence_token"], reader_components=True)
    assert len(observed["journals"]) == 3 and journals_bound(closure, observed["journals"]) == row
    current = next(j for j in observed["revoked_bootstrap"]["journals"] if j["generation"] == row["generation"])
    assert current["inherited_components"] == component_anchor(closure)["retained_progress"]["components"]
    assert len(current["retained_progress"]["components"]) == 2
    async with paid.connect() as connection:
        await connection.execute("DELETE FROM workspace_bootstrap_authority WHERE generation=$1", previous_row["generation"])
    with pytest.raises(HTTPException, match="409"):
        await bootstrap.registrar_snapshot(binding, op, calls[0]["idempotency_key"], calls[0]["fence_token"], reader_components=True)
