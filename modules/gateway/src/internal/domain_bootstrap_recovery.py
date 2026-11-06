"""Reconcile an original bootstrap at an independently proven no-effect boundary."""

import json
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from starlette.concurrency import run_in_threadpool

from src.internal.domain_operation_store import aws_client, domain_connect, harness, operation_connect
from src.internal.domain_provider_authority import read as read_authority
from src.internal.domain_provider_authority import tables
from src.internal.domain_provider_recovery import expired_execution_proof, require
from src.internal.domain_provider_session import sealed_target
from src.shared import domain_bootstrap_components_repeat as repeated_components
from src.shared.domain_bootstrap_acquisition import KIND as ACQUISITION_KIND
from src.shared.domain_bootstrap_acquisition import journal_identity as acquisition_identity
from src.shared.domain_bootstrap_boundary import validate_attempt_extension
from src.shared.domain_bootstrap_components import KIND as COMPONENTS_KIND
from src.shared.domain_bootstrap_components import journal_identity as components_identity
from src.shared.domain_bootstrap_denial import KIND as DENIED_KIND
from src.shared.domain_bootstrap_denial import journal_identity, match_revoked
from src.shared.domain_bootstrap_evidence import chain
from src.shared.domain_bootstrap_recovery import validate
from src.shared.domain_bootstrap_retained import KIND as RETAINED_KIND
from src.shared.domain_bootstrap_revoked import KINDS as REVOKED_KINDS
from src.shared.domain_bootstrap_revoked import NAMESPACE_KIND, retained_identity
from src.shared.domain_bootstrap_revoked import journal_identity as registrar_identity
from src.shared.domain_provider_contract import digest, reserved
from src.shared.models.audit import AuditLog


async def cleanup_authority(db, store, binding, operation, lease, call):
    """Read-only owner preflight. Expiry alone never authorizes a retry."""
    request = harness("identity").decode_payload(operation["request_payload"])
    lifecycle = json.loads(request.parameters.get("lifecycle_request", "{}"))
    credential, _ = sealed_target(operation, lifecycle.get("region"))
    closure = None
    if operation["state"] == "unknown":
        async with operation_connect(binding) as connection:
            closure = await harness("settled_bootstrap").read(connection, operation, lease, call)
    require(
        request.action == "provision"
        and request.parameters.get("lifecycle_phase") == "bootstrap-workspace"
        and lifecycle.get("mode") == "existing-account-managed"
        and not request.parameters.get("shared_membership")
        and (
            closure is not None
            or (
                operation["state"] == "running"
                and call["stage"] == "intended"
                and call["outcome"] is None
                and call["fence_token"] == lease["fence_token"]
                and call["attempt_id"] == lease["holder"]
            )
        )
        and reserved(credential[0])
    )
    mappings = await expired_execution_proof(store, binding, operation, lease, source_principal=call["attempt_id"] if closure else None)
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
    # Closed leases deliberately clear live deadlines. The immutable execution
    # grant mappings bound the original authority; never synthesize a live lease.
    expirations = [] if closure else [lease["runtime_deadline"], lease["expires_at"]]
    expirations.extend(datetime.fromisoformat(m["expired_at"]) for m in mappings)
    for row in rows:
        expiry = datetime.fromisoformat(row.details["expires_at"])
        require(
            row.event_type == "paid_domain_provider_session_issued" and row.details["credential_id"] == credential[0] and expiry.tzinfo is not None
        )
        expirations.append(expiry)
    guard = max(expirations) + timedelta(seconds=900)
    require(datetime.now(UTC) >= guard)
    return {
        "operation_id": operation["operation_id"],
        "source_principal": call["attempt_id"] if closure else lease["holder"],
        **({"settled_bootstrap": closure, "source_acquired_at": closure["source_audit"]["recorded_at"]} if closure else {}),
        "source_fence": call["fence_token"],
        "source_key": call["idempotency_key"],
        "earliest_owner_evidence_at": guard.isoformat(),
        "original_authority_expired": True,
        "issued_sessions_expired": True,
        "attempts": lease["attempts"],
        "max_attempts": lease["max_attempts"],
        "mappings": mappings,
        "issuance_audit_ids": sorted(str(row.id) for row in rows),
    }


async def cleanup_snapshot(binding, operation, *, complete=False):
    """Read the whole protected workspace before owner-native cleanup."""
    request = harness("identity").decode_payload(operation["request_payload"])
    async with domain_connect(binding) as connection:
        async with connection.transaction(readonly=True):
            require(
                await connection.fetchval(
                    "SELECT EXISTS(SELECT 1 FROM workspaces WHERE id::text=$1 AND org_id::text=$2 "
                    "AND provisioning_operation_id=$3 AND status='Provisioning' AND cluster_id IS NULL)",
                    operation["workspace_id"],
                    binding.org_id,
                    operation["operation_id"],
                )
            )
            rows = await connection.fetch("SELECT * FROM workspace_bootstrap_authority WHERE workspace_id::text=$1", operation["workspace_id"])
            require(len(rows) == 1)
            row = dict(rows[0])
            identity = journal_identity(row, complete=complete)
            require(all(identity[k] == operation[k] for k in ("org_id", "workspace_id", "operation_id")))
            for table in ("workspace_lifecycle_effects", "cluster_memberships"):
                require(
                    not await connection.fetchval("SELECT EXISTS(SELECT 1 FROM " + table + " WHERE workspace_id::text=$1)", operation["workspace_id"])
                )
            require(
                not await connection.fetchval(
                    "SELECT EXISTS(SELECT 1 FROM workspace_lifecycle_artifacts WHERE source_operation_id=$1)", operation["operation_id"]
                )
            )
            require(
                await connection.fetchval(
                    "SELECT count(*) FROM workspace_bootstrap_reservations WHERE workspace_id::text=$1", operation["workspace_id"]
                )
                == (0 if complete else 1)
            )
            artifact = await connection.fetchrow(
                "SELECT * FROM workspace_lifecycle_artifacts WHERE artifact_id=$1 AND org_id=$2 AND workspace_id=$3",
                request.parameters["lifecycle_artifact_id"],
                operation["org_id"],
                operation["workspace_id"],
            )
            require(artifact is not None)
            metadata = json.loads(artifact["artifact_metadata_json"])
            outputs = {k: v["value"] for k, v in metadata["outputs"].items()}
            require(metadata.get("next_phase") == "bootstrap-workspace" and outputs.get("cluster_endpoint_public_access") is True)
            require(outputs.get("cluster_arn") == row["cluster_arn"])
            # Only stable protected fields leave this read; driver observations
            # compare this snapshot again before every native cleanup boundary.
            return {
                k: row[k]
                for k in ("workspace_id", "operation_id", "org_id", "cluster_arn", "generation", "claim", "plan_json", "progress_json", "revoked")
            }


def read_evidence(identity):
    table, _ = tables()
    client = aws_client("dynamodb")

    def read_item(key):
        return client.get_item(TableName=table, Key={"record_id": {"S": key}}, ConsistentRead=True).get("Item")

    records = chain(read_item, identity, validate_history=lambda value: validate(value, require_fresh=False))
    require(records)
    return validate(records[-1])


async def registrar_snapshot(
    binding,
    operation,
    source_key,
    source_fence,
    *,
    complete=True,
    retained_namespace=False,
    acquisition=False,
    components=False,
    reader_components=False,
):
    """Account for every retained journal and at most one new registrar prefix."""
    request = harness("identity").decode_payload(operation["request_payload"])
    async with operation_connect(binding) as connection:
        retry = harness("provider_retry")
        await retry.active_call_rows(connection, operation["operation_id"], [])
        history = await retry.receipts_for(connection, operation["operation_id"])
        if reader_components:
            await harness("settled_bootstrap_repeat").predecessor(connection, operation, history)
    previous = dict(history[-1]) if history else None
    if reader_components:
        require(
            not retained_namespace
            and not acquisition
            and not components
            and 1 <= len(history) <= harness("settled_bootstrap_repeat").MAX_PREDECESSORS
            and operation["state"] == "unknown"
        )
        require(json.loads(previous["proof"])["evidence_kind"] in {COMPONENTS_KIND, repeated_components.KIND})
    if components:
        require(complete and not retained_namespace and not acquisition and previous is None and operation["state"] == "unknown")
    baseline, predecessor = [], None
    if previous is not None:
        proof = json.loads(previous["proof"])
        require(previous["replacement_key"] == source_key and previous["source_fence"] < source_fence)
        require(previous["restored_version"] == operation["version"] - int(reader_components) and previous["recovery_scope"] == "unstarted-bootstrap")
        require(all(previous[k] == operation[k] for k in ("org_id", "workspace_id", "plan_digest")))
        baseline = retry.bootstrap_journals(proof)
        if reader_components:
            anchor = (
                baseline[0]
                if proof["evidence_kind"] == COMPONENTS_KIND
                else harness("settled_bootstrap_repeat").component_anchor(proof["settled_bootstrap"])
            )
        predecessor = {"reauthorization_id": previous["reauthorization_id"], "source_fence": previous["source_fence"], "proof_sha256": digest(proof)}
    async with domain_connect(binding) as connection:
        async with connection.transaction(readonly=True):
            require(
                await connection.fetchval(
                    "SELECT EXISTS(SELECT 1 FROM workspaces WHERE id::text=$1 AND org_id::text=$2 "
                    "AND provisioning_operation_id=$3 AND status='Provisioning' AND cluster_id IS NULL)",
                    operation["workspace_id"],
                    binding.org_id,
                    operation["operation_id"],
                )
            )
            rows = [
                {
                    k: r[k]
                    for k in ("workspace_id", "operation_id", "org_id", "cluster_arn", "generation", "claim", "plan_json", "progress_json", "revoked")
                }
                for r in await connection.fetch("SELECT * FROM workspace_bootstrap_authority WHERE workspace_id::text=$1", operation["workspace_id"])
            ]
            require(1 <= len(rows) <= 20 and len({r["generation"] for r in rows}) == len(rows))
            require(not components or len(rows) == 1)
            artifact = await connection.fetchrow(
                "SELECT * FROM workspace_lifecycle_artifacts WHERE artifact_id=$1 AND org_id=$2 AND workspace_id=$3",
                request.parameters["lifecycle_artifact_id"],
                operation["org_id"],
                operation["workspace_id"],
            )
            require(artifact is not None)
            metadata = json.loads(artifact["artifact_metadata_json"])
            outputs = {k: v["value"] for k, v in metadata["outputs"].items()}
            require(metadata.get("next_phase") == "bootstrap-workspace" and outputs.get("cluster_endpoint_public_access") is True)
            require(all(outputs.get("cluster_arn") == row["cluster_arn"] for row in rows))
            old = {j["generation"]: j for j in baseline}
            require(len(old) == len(baseline))
            new = [r for r in rows if r["generation"] not in old]
            require(len(new) <= 1 and (complete or len(new) == 1))
            actual = []
            for row in rows:
                require(all(row[k] == operation[k] for k in ("operation_id", "org_id", "workspace_id")))
                if row["generation"] in old:
                    identity = (
                        components_identity(row)
                        if reader_components and row["generation"] == anchor["generation"]
                        else repeated_components.transition(row, anchor, complete=True)
                        if reader_components
                        else retained_identity(row)
                    )
                    require(identity == old[row["generation"]])
                else:
                    identity = (
                        repeated_components.transition(row, anchor, complete=row["revoked"] if not complete else True)
                        if reader_components
                        else components_identity(row)
                        if components
                        else acquisition_identity(row, complete=complete)
                        if acquisition
                        else registrar_identity(row, complete=complete, retained_namespace=retained_namespace)
                    )
                actual.append(identity)
            require({r["generation"] for r in rows}.issuperset(old))
            require(
                await connection.fetchval(
                    "SELECT count(*) FROM workspace_bootstrap_reservations WHERE workspace_id::text=$1", operation["workspace_id"]
                )
                == (0 if complete else 1)
            )
            for table in ("workspace_lifecycle_effects", "cluster_memberships"):
                require(
                    not await connection.fetchval("SELECT EXISTS(SELECT 1 FROM " + table + " WHERE workspace_id::text=$1)", operation["workspace_id"])
                )
            require(
                not await connection.fetchval(
                    "SELECT EXISTS(SELECT 1 FROM workspace_lifecycle_artifacts WHERE source_operation_id=$1)", operation["operation_id"]
                )
            )
    value = {
        "journals": sorted(actual, key=lambda j: j["generation"]),
        "current_generation": new[0]["generation"] if new else None,
        "predecessor": predecessor,
    }
    require(value["current_generation"] is not None or predecessor is not None)
    return {"revoked_bootstrap": value, "journal": new[0] if new else None, "journals": sorted(rows, key=lambda r: r["generation"])}


async def retained_revocation(binding, operation, source_key, source_fence, journal):
    """Bind the retained journal to the immutable retry that launched this call.

    The earlier owner evidence may be expired. Its consumed receipt remains
    history, never new authority. Every retained predecessor must link back to
    an exact denied-first-grant receipt and its unchanged revoked journal.
    """
    async with operation_connect(binding) as connection:
        retry = harness("provider_retry")
        await retry.active_call_rows(connection, operation["operation_id"], [])
        history = await retry.receipts_for(connection, operation["operation_id"])
        rows = [row for row in history if row["replacement_key"] == source_key]
    require(len(rows) == 1)
    prior = dict(rows[0])
    require(prior == dict(history[-1]))
    proof = json.loads(prior["proof"])
    owner = harness("provider_retry").bootstrap_boundary_identity(proof)
    require(
        prior["recovery_scope"] == "unstarted-bootstrap"
        and prior["source_fence"] < source_fence
        and prior["restored_version"] == operation["version"]
        and all(prior[k] == operation[k] for k in ("org_id", "workspace_id", "plan_digest"))
        and proof["evidence_kind"] in {DENIED_KIND, RETAINED_KIND}
        and all(owner[k] == operation[k] for k in ("operation_id", "org_id", "workspace_id", "plan_digest"))
    )
    # active_call_rows independently checks every source call and linked proof.
    # Walk the retained suffix to its actual denied-grant anchor, not an arbitrary
    # earlier receipt with a convenient journal identity.
    anchor = proof
    for item in reversed(history[:-1]):
        if anchor["evidence_kind"] != RETAINED_KIND:
            break
        anchor = json.loads(item["proof"])
    require(anchor["evidence_kind"] == DENIED_KIND)
    match_revoked(journal, anchor["owner_evidence"])
    return {
        "reauthorization_id": prior["reauthorization_id"],
        "source_fence": prior["source_fence"],
        "proof_sha256": digest(proof),
        "journal": journal_identity(journal, complete=True),
    }


async def untouched(binding, operation, request, evidence=None):
    """Read protected domain journals; caller absence claims are never accepted."""
    async with domain_connect(binding) as connection:
        require(
            await connection.fetchval(
                "SELECT EXISTS(SELECT 1 FROM workspaces WHERE id::text=$1 AND org_id::text=$2 "
                "AND provisioning_operation_id=$3 AND status='Provisioning' AND cluster_id IS NULL)",
                operation["workspace_id"],
                binding.org_id,
                operation["operation_id"],
            )
        )
        # A journal never satisfies the original absent-journal barrier. The
        # distinct denied-first-grant boundary requires one exact retained,
        # positively revoked journal independently qualified by the owner.
        denied = evidence is not None and evidence["evidence_kind"] == DENIED_KIND
        retained = evidence is not None and evidence["evidence_kind"] == RETAINED_KIND
        registrar = evidence is not None and evidence["evidence_kind"] in {*REVOKED_KINDS, COMPONENTS_KIND, repeated_components.KIND}
        if registrar:
            snapshot = await registrar_snapshot(
                binding,
                operation,
                evidence["source_key"],
                evidence["source_fence"],
                retained_namespace=evidence["evidence_kind"] == NAMESPACE_KIND,
                acquisition=evidence["evidence_kind"] == ACQUISITION_KIND,
                components=evidence["evidence_kind"] == COMPONENTS_KIND,
                reader_components=evidence["evidence_kind"] == repeated_components.KIND,
            )
            require(snapshot["revoked_bootstrap"] == evidence["revoked_bootstrap"])
        elif denied or retained:
            rows = await connection.fetch("SELECT * FROM workspace_bootstrap_authority WHERE workspace_id::text=$1", operation["workspace_id"])
            require(len(rows) == 1)
            if retained:
                require(
                    await retained_revocation(binding, operation, evidence["source_key"], evidence["source_fence"], dict(rows[0]))
                    == evidence["retained_revocation"]
                )
            else:
                match_revoked(dict(rows[0]), evidence)
        else:
            require(
                not await connection.fetchval(
                    "SELECT EXISTS(SELECT 1 FROM workspace_bootstrap_authority WHERE workspace_id::text=$1)", operation["workspace_id"]
                )
            )
        for table in ("workspace_bootstrap_reservations", "workspace_lifecycle_effects", "cluster_memberships"):
            require(
                not await connection.fetchval(
                    "SELECT EXISTS(SELECT 1 FROM " + table + " WHERE workspace_id::text=$1)",
                    operation["workspace_id"],
                )
            )
        require(
            not await connection.fetchval(
                "SELECT EXISTS(SELECT 1 FROM workspace_lifecycle_artifacts WHERE source_operation_id=$1)",
                operation["operation_id"],
            )
        )
        artifact = await connection.fetchrow(
            "SELECT * FROM workspace_lifecycle_artifacts WHERE artifact_id=$1 AND org_id=$2 AND workspace_id=$3",
            request.parameters["lifecycle_artifact_id"],
            operation["org_id"],
            operation["workspace_id"],
        )
        require(artifact is not None)
        metadata = json.loads(artifact["artifact_metadata_json"])
        outputs = {key: value["value"] for key, value in metadata["outputs"].items()}
        require(metadata.get("next_phase") == "bootstrap-workspace" and outputs.get("cluster_endpoint_public_access") is True)
        require(outputs.get("account_id") == json.loads(request.parameters["lifecycle_request"])["target_account_id"])
        if denied:
            require(outputs.get("cluster_arn") == evidence["denied_grant"]["cluster_arn"])
        if retained:
            require(outputs.get("cluster_arn") == evidence["retained_revocation"]["journal"]["cluster_arn"])
        if registrar:
            require(all(outputs.get("cluster_arn") == j["cluster_arn"] for j in evidence["revoked_bootstrap"]["journals"]))
    return digest({k: artifact[k] for k in ("artifact_id", "source_operation_id", "artifact_metadata_json", "source_payload_digest")})


async def prove(db, store, binding, operation, lease, call, *, settled=False):
    request = harness("identity").decode_payload(operation["request_payload"])
    p = request.parameters
    steps = harness("execution_descriptors").parse_execution_steps(p.get("execution_steps", ""))
    lifecycle = json.loads(p.get("lifecycle_request", "{}"))
    require(
        request.action == "provision"
        and p.get("runtime_config_sha256")
        and p.get("lifecycle_artifact_id")
        and not p.get("shared_membership")
        and lifecycle.get("mode") == "existing-account-managed"
        and len(steps) == 1
        and steps[0].provider == "superplane-lifecycle"
        and steps[0].step_id == steps[0].operation_kind == "bootstrap-workspace"
        and steps[0].target == p["lifecycle_artifact_id"]
    )
    credential, account = sealed_target(operation, lifecycle.get("region"))
    require(reserved(credential[0]))
    identity = {key: operation[key] for key in ("org_id", "workspace_id", "operation_id", "plan_digest")}
    identity.update(source_key=call["idempotency_key"], source_fence=call["fence_token"])
    evidence = await run_in_threadpool(read_evidence, identity)
    closure = None
    if settled:
        async with operation_connect(binding) as connection:
            closure = await harness("settled_bootstrap").read(connection, operation, lease, call)
        require(evidence["evidence_kind"] in {ACQUISITION_KIND, COMPONENTS_KIND, repeated_components.KIND})
    require(evidence.get("settled_bootstrap") == closure)
    authority = await run_in_threadpool(read_authority, credential[0])
    if "attempt_extension" in evidence:
        if evidence["evidence_kind"] == ACQUISITION_KIND:
            from src.shared.domain_bootstrap_acquisition import extension_boundary

            extension_boundary(evidence)
        validate_attempt_extension(
            evidence["attempt_extension"],
            subject=operation["requester"],
            attempts=lease["attempts"],
            ceiling=lease["max_attempts"],
            acquisition=evidence["evidence_kind"] == ACQUISITION_KIND,
        )
    mappings = await expired_execution_proof(store, binding, operation, lease, source_principal=call["attempt_id"])
    checked = datetime.fromisoformat(evidence["checked_at"])
    require(
        evidence["credential_id"] == credential[0]
        and evidence["adp_org_id"] == binding.adp_org_id
        and evidence["installation_id"] == authority["installation_id"]
        and evidence["account_id"] == authority["account_id"] == account
        and evidence["region"] == authority["region"] == lifecycle["region"]
        and authority["org_id"] == operation["org_id"]
        and authority["workspace_id"] == operation["workspace_id"]
        and authority["subject"] == operation["requester"]
        and evidence["source_principal"] == call["attempt_id"]
        # Harness verifies every intervening authenticated observation claim
        # under the operation lock; observers never replace the source producer.
        and (
            call["fence_token"] < lease["fence_token"]
            or evidence["source_principal"] == lease["holder"]
            and datetime.fromisoformat(evidence["source_acquired_at"]) == lease["acquired_at"]
        )
        and evidence["policy_sha256"] == p["lifecycle_policy_sha256"]
        and (call["fence_token"] < lease["fence_token"] or checked >= lease["runtime_deadline"] + timedelta(seconds=900))
        and all(checked >= datetime.fromisoformat(m["expired_at"]) + timedelta(seconds=900) for m in mappings)
    )
    invocation, attempt = evidence["source_principal"].rsplit("#", 1)
    record = await run_in_threadpool(store.authority.load_execution, invocation_id=invocation, tenant_id=binding.adp_org_id)
    raw = await run_in_threadpool(store._read, "TENANT#" + binding.adp_org_id, "EXEC#" + invocation)
    require(record is not None and attempt == "1" and record.workload_binding == evidence["workload_binding"])
    require(raw is not None and raw.get("pod_name", {}).get("S") == evidence["source_pod_name"])
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
    for row in rows:
        expiry = datetime.fromisoformat(row.details["expires_at"])
        require(
            row.event_type == "paid_domain_provider_session_issued"
            and row.details["credential_id"] == credential[0]
            and expiry.tzinfo is not None
            and expiry + timedelta(seconds=900) <= checked <= datetime.now(UTC)
        )
    artifact_digest = await untouched(binding, operation, request, evidence)
    if settled:
        async with domain_connect(binding) as connection:
            journals = await connection.fetch("SELECT * FROM workspace_bootstrap_authority WHERE workspace_id::text=$1", operation["workspace_id"])
        if evidence["evidence_kind"] == repeated_components.KIND:
            harness("settled_bootstrap_repeat").journals_bound(closure, [dict(j) for j in journals])
        else:
            require(len(journals) == 1)
            harness("settled_bootstrap").journal_bound(closure, dict(journals[0]))
    require(await run_in_threadpool(read_evidence, identity) == evidence)
    require(await run_in_threadpool(read_authority, credential[0]) == authority)
    require(await untouched(binding, operation, request, evidence) == artifact_digest)
    denied = evidence["evidence_kind"] == DENIED_KIND
    registrar = evidence["evidence_kind"] in {*REVOKED_KINDS, COMPONENTS_KIND, repeated_components.KIND}
    created = registrar and evidence["revoked_bootstrap"]["current_generation"] is not None
    return {
        "evidence_kind": evidence["evidence_kind"],
        **({"settled_bootstrap": closure} if settled else {}),
        **({"attempt_extension": evidence["attempt_extension"]} if "attempt_extension" in evidence else {}),
        "bootstrap_mutations_started": denied or created,
        **({"bootstrap_grants_created": False, "bootstrap_journal_revoked": True, "denied_grant": evidence["denied_grant"]} if denied else {}),
        **({"retained_revocation": evidence["retained_revocation"]} if evidence["evidence_kind"] == RETAINED_KIND else {}),
        **(
            {
                "revoked_bootstrap": evidence["revoked_bootstrap"],
                "bootstrap_grants_created": created,
                "bootstrap_journal_revoked": True,
                "bootstrap_components_started": evidence["evidence_kind"]
                in {NAMESPACE_KIND, ACQUISITION_KIND, COMPONENTS_KIND, repeated_components.KIND},
                **(
                    {"bootstrap_components_retained": True, "bootstrap_admission_started": False}
                    if evidence["evidence_kind"] in {COMPONENTS_KIND, repeated_components.KIND}
                    else {}
                ),
            }
            if registrar
            else {}
        ),
        "original_authority_expired": True,
        "issued_sessions_expired": True,
        "owner_evidence_id": evidence["record_id"],
        "owner_evidence": evidence,
        **({"provenance_assumption": evidence["provenance_assumption"]} if "provenance_assumption" in evidence else {}),
        "source_artifact_id": p["lifecycle_artifact_id"],
        "source_artifact_sha256": artifact_digest,
        "issuance_audit_ids": sorted(str(row.id) for row in rows),
        "mappings": mappings,
        "credential_id": credential[0],
        "account_id": account,
        "region": lifecycle["region"],
        "source_key": call["idempotency_key"],
        "checked_at": datetime.now(UTC).isoformat(),
    }
