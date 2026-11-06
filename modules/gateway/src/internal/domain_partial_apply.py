"""Credential-owner verification of retained partial apply facts under source locks."""

import json
import re
import uuid
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException
from sqlalchemy import select
from starlette.concurrency import run_in_threadpool

from src.internal.domain_current_identity import revalidate_original_humans
from src.internal.domain_operation_dispatch import paid_operation
from src.internal.domain_operation_store import aws_client, domain_connect, harness, operation_connect
from src.internal.domain_provider_authority import read as read_authority
from src.internal.domain_provider_authority import tables
from src.internal.domain_provider_recovery import expired_execution_proof
from src.internal.domain_provider_session import sealed_target
from src.shared.domain_partial_apply import IDENTITY, record_id, validate
from src.shared.domain_provider_contract import digest, reserved
from src.shared.models.audit import AuditLog


def require(value):
    if not value:
        raise HTTPException(409, "partial apply recovery evidence refused")


def read_evidence(identity, selected_id=None):
    table, _ = tables()
    key = selected_id or record_id(identity)
    require(isinstance(key, str) and re.fullmatch(r"sppa1:[a-f0-9]{64}", key))
    item = aws_client("dynamodb").get_item(TableName=table, Key={"record_id": {"S": key}}, ConsistentRead=True).get("Item")
    require(item is not None and set(item) == {"record_id", "document", "revision"})
    value = validate(json.loads(item["document"]["S"]))
    require(value["record_id"] == key and item["revision"]["S"] == digest(value) and all(value[k] == identity[k] for k in IDENTITY))
    return value


async def prove(db, store, binding, operation, lease, call, *, owner_evidence_id=None):
    request = harness("identity").decode_payload(operation["request_payload"])
    p = request.parameters
    lifecycle = json.loads(p["lifecycle_request"])
    steps = harness("execution_descriptors").parse_execution_steps(p["execution_steps"])
    require(
        request.action == "provision"
        and p.get("lifecycle_phase") == "apply-infrastructure"
        and lifecycle.get("mode") == "existing-account-managed"
        and json.loads(p["lifecycle_inputs"]).get("isolation_mode") == "dedicated"
        and not p.get("shared_membership")
        and len(steps) == 1
        and steps[0].step_id == steps[0].operation_kind == "apply-infrastructure"
        and steps[0].provider == "superplane-lifecycle"
        and steps[0].target == p["lifecycle_artifact_id"]
    )
    credential, account = sealed_target(operation, lifecycle["region"])
    require(reserved(credential[0]))
    identity = {k: operation[k] for k in ("org_id", "workspace_id", "operation_id", "plan_digest")}
    identity.update(source_key=call["idempotency_key"], source_fence=call["fence_token"])
    evidence = await run_in_threadpool(read_evidence, identity, owner_evidence_id)
    authority = await run_in_threadpool(read_authority, credential[0])
    require(
        evidence["source_version"] == operation["version"]
        and evidence["original_artifact_id"] == p["lifecycle_artifact_id"]
        and evidence["original_planning_operation_id"] == p["lifecycle_source_operation_id"]
        and evidence["credential_id"] == credential[0]
        and evidence["account_id"] == authority["account_id"] == account
        and evidence["region"] == authority["region"] == lifecycle["region"]
        and evidence["installation_id"] == authority["installation_id"]
        and evidence["adp_org_id"] == binding.adp_org_id
        and authority["org_id"] == operation["org_id"]
        and authority["workspace_id"] == operation["workspace_id"]
        and authority["subject"] == operation["requester"]
        and evidence["source_principal"] == call["attempt_id"]
        and (
            call["fence_token"] < lease["fence_token"]
            or evidence["source_principal"] == lease["holder"]
            and datetime.fromisoformat(evidence["source_acquired_at"]) == lease["acquired_at"]
        )
    )
    async with domain_connect(binding) as domain:
        require(
            await domain.fetchval(
                "SELECT EXISTS(SELECT 1 FROM workspaces WHERE id::text=$1 AND org_id::text=$2 "
                "AND provisioning_operation_id=$3 AND status='Provisioning' AND cluster_id IS NULL AND is_default=false)",
                operation["workspace_id"],
                binding.org_id,
                operation["operation_id"],
            )
        )
        require(
            not await domain.fetchval(
                "SELECT EXISTS(SELECT 1 FROM workspace_lifecycle_artifacts WHERE source_operation_id=$1)",
                operation["operation_id"],
            )
        )
        artifact = await domain.fetchrow(
            "SELECT * FROM workspace_lifecycle_artifacts WHERE artifact_id=$1 AND org_id=$2 AND workspace_id=$3",
            evidence["original_artifact_id"],
            operation["org_id"],
            operation["workspace_id"],
        )
        require(
            artifact is not None
            and digest({k: v for k, v in dict(artifact).items() if k not in {"artifact_id", "created_at"}}) == evidence["original_artifact_id"]
        )
        metadata = json.loads(artifact["artifact_metadata_json"])
        observation = evidence["preparation_boundary"]["observation"]
        require(
            artifact["source_operation_id"] == evidence["original_planning_operation_id"]
            and metadata["files"] == observation["source_files"]
            and metadata["module_sha256"] == observation["module_sha256"]
        )
    mappings = await expired_execution_proof(store, binding, operation, lease, source_principal=evidence["source_principal"])
    checked = datetime.fromisoformat(evidence["checked_at"])
    require(
        (call["fence_token"] < lease["fence_token"] or checked >= lease["runtime_deadline"] + timedelta(seconds=900))
        and all(checked >= datetime.fromisoformat(m["expired_at"]) + timedelta(seconds=900) for m in mappings)
    )
    invocation, attempt = evidence["source_principal"].rsplit("#", 1)
    record = await run_in_threadpool(store.authority.load_execution, invocation_id=invocation, tenant_id=binding.adp_org_id)
    raw = await run_in_threadpool(store._read, "TENANT#" + binding.adp_org_id, "EXEC#" + invocation)
    require(
        attempt == "1"
        and record is not None
        and record.workload_binding == evidence["workload_binding"]
        and raw is not None
        and raw.get("pod_name", {}).get("S") == evidence["source_pod_name"]
    )
    rows = (
        (
            await db.execute(
                select(AuditLog).where(
                    AuditLog.org_id == binding.adp_org_id,
                    AuditLog.event_type.in_(("paid_domain_provider_session_issued", "paid_provider_session_issued")),
                    AuditLog.details["operation_id"].as_string() == operation["operation_id"],
                )
            )
        )
        .scalars()
        .all()
    )
    require(bool(rows))
    for row in rows:
        expiry = datetime.fromisoformat(row.details["expires_at"])
        require(
            row.event_type == "paid_domain_provider_session_issued"
            and row.details["credential_id"] == credential[0]
            and expiry.tzinfo is not None
            and expiry <= checked <= datetime.now(UTC)
        )
    require(await run_in_threadpool(read_evidence, evidence) == evidence)
    require(await run_in_threadpool(read_authority, credential[0]) == authority)
    return {
        "evidence_kind": "partial-apply-state-v1",
        "apply_started": True,
        "original_authority_expired": True,
        "issued_sessions_expired": True,
        "owner_evidence_id": evidence["record_id"],
        "owner_evidence": evidence,
        "issuance_audit_ids": sorted(str(r.id) for r in rows),
        "mappings": mappings,
        "source_key": call["idempotency_key"],
        "checked_at": datetime.now(UTC).isoformat(),
    }


async def source_facts(connection, db, store, binding, *, operation_id, requester, expected_version, owner_evidence_id=None):
    await connection.fetchrow(
        "SELECT operation_id FROM harness_operations WHERE operation_id=$1 AND org_id=$2 FOR UPDATE", operation_id, binding.org_id
    )
    operation = await paid_operation(binding, operation_id, connection=connection)
    lease = await connection.fetchrow("SELECT * FROM harness_operation_leases WHERE operation_id=$1 FOR UPDATE", operation_id)
    now = await connection.fetchval("SELECT clock_timestamp()")
    require(
        operation["requester"] == requester
        and operation["version"] == expected_version
        and operation["state"] == "running"
        and not operation["cancel_requested_at"]
        and not operation["cleanup_required"]
        and operation["reservation_state"] == operation["budget_state"] == "confirmed"
        and lease is not None
        and lease["closed_at"] is None
        and lease["holder"] is not None
        and lease["expires_at"] <= now
        and lease["runtime_deadline"] <= now
    )
    calls = await connection.fetch("SELECT * FROM harness_provider_call_intent WHERE operation_id=$1", operation_id)
    require(len(calls) == 1)
    call = calls[0]
    try:
        await harness("provider_retry").verify_partial_source_claim(connection, lease, call)
    except harness("identity").OperationRefused:
        raise HTTPException(409, "partial apply recovery evidence refused") from None
    steps = harness("execution_plan").admitted_steps(harness("store")._record(operation))
    require(
        len(steps) == 1
        and call["idempotency_key"] == harness("execution_plan").step_key(harness("store")._record(operation), steps[0])
        and call["stage"] == "intended"
        and call["outcome"] is None
        and call["provider_ref"] is None
        and all(call[k] == operation[k] for k in ("org_id", "workspace_id", "job_id"))
    )
    for table in ("harness_provider_report", "harness_allocation_resource", "harness_recovery_settlements", "harness_provider_retries"):
        require(not await connection.fetchval("SELECT EXISTS(SELECT 1 FROM " + table + " WHERE operation_id=$1)", operation_id))
    await revalidate_original_humans(operation, adp_org_id=binding.adp_org_id)
    return await prove(db, store, binding, operation, lease, call, owner_evidence_id=owner_evidence_id)


async def observe(db, store, binding, body):
    require(binding.current_identity_enforced)
    async with operation_connect(binding) as connection, connection.transaction():
        proof = await source_facts(
            connection,
            db,
            store,
            binding,
            operation_id=body.operation_id,
            requester=body.requester,
            expected_version=body.expected_version,
            owner_evidence_id=getattr(body, "owner_evidence_id", None),
        )
        require(proof["owner_evidence"]["workspace_id"] == body.workspace_id)
        return proof


async def verify_preparation(binding, operation, db):
    """A new planning lease cannot borrow arbitrary partial-workspace credentials."""
    from types import SimpleNamespace

    from src.internal.domain_operation_runtime import bootstrap_store

    p = harness("identity").decode_payload(operation["request_payload"]).parameters
    if p.get("lifecycle_phase") != "prepare-partial-apply":
        return
    require(operation["max_resource_units"] == operation["max_cost_micros"] == 0)
    proof = await observe(
        db,
        bootstrap_store(),
        binding,
        SimpleNamespace(
            operation_id=p["partial_apply_source_operation_id"],
            org_id=operation["org_id"],
            workspace_id=operation["workspace_id"],
            requester=operation["requester"],
            expected_version=int(p["partial_apply_source_version"]),
            owner_evidence_id=p["partial_apply_owner_evidence_id"],
        ),
    )
    evidence = proof["owner_evidence"]
    async with operation_connect(binding) as connection:
        original = await paid_operation(binding, evidence["operation_id"], connection=connection)
    previous = dict(harness("identity").decode_payload(original["request_payload"]).parameters)
    identity = [operation["org_id"], operation["workspace_id"], original["operation_id"], evidence["source_fence"]]
    if evidence["version"] == 2:
        identity.append(evidence["record_id"])
    scope = digest(identity)
    identifier = str(uuid.uuid5(uuid.NAMESPACE_URL, "superplane-partial-apply:" + scope))
    expected = {
        k: v
        for k, v in previous.items()
        if k
        not in {
            "lifecycle_artifact_id",
            "lifecycle_source_operation_id",
            "lifecycle_source_job_id",
            "lifecycle_source_attempt_id",
            "lifecycle_source_payload_digest",
        }
    }
    expected.update(
        lifecycle_phase="prepare-partial-apply",
        allocation_id=str(uuid.uuid5(uuid.NAMESPACE_URL, "superplane-partial-apply-allocation:" + identifier)),
        max_resource_units="0",
        max_cost_micros="0",
        partial_apply_source_operation_id=original["operation_id"],
        partial_apply_source_plan_digest=original["plan_digest"],
        partial_apply_source_version=str(original["version"]),
        partial_apply_source_fence=str(evidence["source_fence"]),
        partial_apply_source_artifact_id=previous["lifecycle_artifact_id"],
        partial_apply_original_allocation_id=previous["allocation_id"],
        partial_apply_owner_evidence_id=evidence["record_id"],
        partial_apply_owner_evidence_sha256=digest(evidence),
        partial_apply_snapshot_sha256=digest(evidence["snapshot"]),
    )
    descriptors = harness("execution_descriptors")
    expected["execution_steps"] = descriptors.encode_execution_steps(
        [
            descriptors.ExecutionStep(
                "prepare-partial-apply",
                "superplane-lifecycle",
                "prepare-partial-apply",
                digest({k: v for k, v in expected.items() if k != "execution_steps"}),
            )
        ]
    )
    require(dict(p) == expected and operation["idempotency_key"] == identifier)
    async with domain_connect(binding) as domain:
        row = await domain.fetchrow(
            "SELECT * FROM workspace_partial_apply_preparations WHERE operation_id=$1 AND org_id=$2 AND workspace_id=$3",
            operation["operation_id"],
            operation["org_id"],
            operation["workspace_id"],
        )
    require(
        row is not None
        and row["plan_digest"] == operation["plan_digest"]
        and row["source_operation_id"] == original["operation_id"]
        and row["source_version"] == original["version"]
        and row["source_fence"] == evidence["source_fence"]
        and row["owner_evidence_sha256"] == digest(evidence)
        and json.loads(row["owner_evidence_json"]) == evidence
    )
    if evidence["version"] == 2:
        await verify_expired_preparations(binding, operation, evidence)


async def verify_expired_preparations(binding, operation, evidence):
    """Refuse orphaned, active or started predecessors before granting authority."""
    async with domain_connect(binding) as domain:
        predecessors = await domain.fetch(
            "SELECT * FROM workspace_partial_apply_preparations WHERE org_id=$1 AND workspace_id=$2 "
            "AND source_operation_id=$3 AND operation_id<>$4 LIMIT 5",
            operation["org_id"],
            operation["workspace_id"],
            evidence["operation_id"],
            operation["operation_id"],
        )
        async with operation_connect(binding) as connection:
            admissions = await connection.fetch(
                "SELECT operation_id FROM harness_operations WHERE org_id=$1 AND workspace_id=$2 "
                "AND action='provision' AND request_payload::jsonb->'parameters'->>'lifecycle_phase'='prepare-partial-apply' "
                "AND request_payload::jsonb->'parameters'->>'partial_apply_source_operation_id'=$3 "
                "AND operation_id<>$4 LIMIT 5",
                operation["org_id"],
                operation["workspace_id"],
                evidence["operation_id"],
                operation["operation_id"],
            )
        require(len(predecessors) < 5 and len(admissions) < 5)
        require({r["operation_id"] for r in predecessors} == {r["operation_id"] for r in admissions})
        for previous in predecessors:
            old = json.loads(previous["owner_evidence_json"])
            validate(old, fresh=False)
            require(
                previous["owner_evidence_sha256"] == digest(old)
                and old["record_id"] != evidence["record_id"]
                and datetime.fromisoformat(old["expires_at"]) <= datetime.fromisoformat(evidence["checked_at"])
            )
            require(
                not await domain.fetchval(
                    "SELECT EXISTS(SELECT 1 FROM workspace_lifecycle_artifacts WHERE source_operation_id=$1)", previous["operation_id"]
                )
            )
            async with operation_connect(binding) as connection:
                require(
                    not await connection.fetchval(
                        "SELECT EXISTS(SELECT 1 FROM harness_provider_call_intent WHERE operation_id=$1)", previous["operation_id"]
                    )
                )
                require(
                    not await connection.fetchval(
                        "SELECT EXISTS(SELECT 1 FROM harness_operation_leases WHERE operation_id=$1 "
                        "AND holder IS NOT NULL AND expires_at>clock_timestamp())",
                        previous["operation_id"],
                    )
                )


async def reviewed_artifact(binding, operation, evidence, artifact_id, *, fresh=True):
    """Verify immutable artifact bytes' digest and the real successful producer."""
    validate(evidence, fresh=fresh)
    async with domain_connect(binding) as domain:
        artifact = await domain.fetchrow(
            "SELECT * FROM workspace_lifecycle_artifacts WHERE artifact_id=$1 AND org_id=$2 AND workspace_id=$3",
            artifact_id,
            operation["org_id"],
            operation["workspace_id"],
        )
        require(artifact is not None)
        require(digest({k: v for k, v in dict(artifact).items() if k not in {"artifact_id", "created_at"}}) == artifact_id)
        registration = await domain.fetchrow(
            "SELECT * FROM workspace_partial_apply_preparations WHERE operation_id=$1", artifact["source_operation_id"]
        )
    require(
        registration is not None
        and registration["source_operation_id"] == operation["operation_id"]
        and registration["owner_evidence_sha256"] == digest(evidence)
        and json.loads(registration["owner_evidence_json"]) == evidence
    )
    async with operation_connect(binding) as connection:
        prep = await paid_operation(binding, artifact["source_operation_id"], connection=connection)
    require(
        prep["state"] == "succeeded"
        and prep["request_payload"] == artifact["source_request_payload"]
        and prep["plan_digest"] == artifact["source_payload_digest"] == registration["plan_digest"]
        and prep["requester"] == operation["requester"]
        and all(prep[k] == artifact["source_" + k] for k in ("job_id", "attempt_id"))
        and prep["org_id"] == operation["org_id"]
        and prep["workspace_id"] == operation["workspace_id"]
        and prep["max_resource_units"] == prep["max_cost_micros"] == 0
    )
    if fresh:
        require(datetime.now(UTC) - timedelta(hours=1) <= artifact["created_at"] <= datetime.now(UTC))
    p = harness("identity").decode_payload(prep["request_payload"]).parameters
    metadata = json.loads(artifact["artifact_metadata_json"])
    repair = metadata["partial_repair"]
    require(
        p.get("lifecycle_phase") == "prepare-partial-apply"
        and p.get("partial_apply_source_operation_id") == operation["operation_id"]
        and p.get("partial_apply_source_plan_digest") == operation["plan_digest"]
        and p.get("partial_apply_owner_evidence_sha256") == digest(evidence)
        and metadata["next_phase"] == "partial-apply-repair"
        and repair["snapshot"] == evidence["snapshot"]
        and repair["source_operation_id"] == operation["operation_id"]
        and repair["source_artifact_id"] == evidence["original_artifact_id"]
        and repair["owner_evidence_id"] == evidence["record_id"]
        and repair["owner_evidence_sha256"] == digest(evidence)
    )
    result = {
        "artifact_id": artifact_id,
        "preparation_operation_id": prep["operation_id"],
        "plan_file_sha256": metadata["plan_file_sha256"],
        "plan_json_sha256": metadata["plan_json_sha256"],
        "snapshot_sha256": digest(evidence["snapshot"]),
        "changes": repair["changes"],
    }
    harness("provider_retry").validate_partial_repair_plan(evidence, result)
    return result


async def decision_terms(binding, body, operation):
    async with domain_connect(binding) as domain:
        renewal = await domain.fetchrow("SELECT * FROM operation_reauthorizations WHERE reauthorization_id=$1", body.reauthorization_id)
        require(renewal is not None and renewal["scope"] == "partial-apply-repair")
        terms = json.loads(renewal["amendment_json"])
        registration = await domain.fetchrow(
            "SELECT * FROM workspace_partial_apply_preparations WHERE operation_id=$1", terms["repair"]["preparation_operation_id"]
        )
    require(registration is not None)
    evidence = json.loads(registration["owner_evidence_json"])
    require(await run_in_threadpool(read_evidence, evidence) == evidence)
    repair = await reviewed_artifact(binding, operation, evidence, terms["repair"]["artifact_id"])
    require(terms == {"version": 1, "owner_evidence_id": evidence["record_id"], "owner_evidence_sha256": digest(evidence), "repair": repair})
    return terms


async def recover(binding, body, store, db, *, authorize_producer):
    from src.internal.domain_operation_approval import current_approval

    require(binding.current_identity_enforced)
    principal = harness("identity").ResolvedPrincipal(body.org_id, body.workspace_id, body.requester, frozenset({"workspace:provision"}))

    async def authorize(operation, paid):
        authorize_producer()
        current = await paid_operation(binding, body.operation_id)
        require(current["budget_state"] == paid["reservation_state"] == "confirmed")
        current.update(
            reauthorization_id=body.reauthorization_id,
            recovery_expected_version=body.expected_version,
            recovery_scope="partial-apply-repair",
            partial_apply_review=await decision_terms(binding, body, current),
        )
        async with domain_connect(binding) as domain:
            deadline = await current_approval(domain, current, harness("identity").decode_payload(operation["request_payload"]))
        await revalidate_original_humans(current, adp_org_id=binding.adp_org_id)
        return deadline

    async def evidence(operation, lease, call):
        current = await paid_operation(binding, body.operation_id)
        terms = await decision_terms(binding, body, current)
        proof = await prove(db, store, binding, current, lease, call, owner_evidence_id=terms["owner_evidence_id"])
        require(terms["owner_evidence_id"] == proof["owner_evidence_id"] and terms["owner_evidence_sha256"] == digest(proof["owner_evidence"]))
        proof["repair"] = terms["repair"]
        return proof

    async with operation_connect(binding) as connection:
        receipt = await harness("provider_retry").recover_uncredentialed(
            connection,
            principal=principal,
            operation_id=body.operation_id,
            expected_version=body.expected_version,
            reauthorization_id=body.reauthorization_id,
            authorize=authorize,
            prove_not_started=evidence,
            recovery_scope="partial-apply-repair",
        )
    return {
        "operation_id": receipt["operation_id"],
        "reauthorization_id": receipt["reauthorization_id"],
        "restored_version": receipt["restored_version"],
        "status": "recovery-recorded",
    }


async def verify_repair_delivery(binding, operation):
    if operation.get("recovery_scope") != "partial-apply-repair":
        return
    proof = json.loads(operation["provider_recovery_proof"])
    repair = harness("provider_retry").partial_repair_identity(proof)
    require(await reviewed_artifact(binding, operation, proof["owner_evidence"], repair["artifact_id"], fresh=False) == repair)
