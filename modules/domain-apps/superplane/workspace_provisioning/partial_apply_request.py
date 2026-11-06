"""A separate zero-spend planning admission retains the original paid apply."""

import json
import re
import uuid
from datetime import datetime, timezone

from harness_jobs.allocation import allocation_id_for
from harness_jobs.identity import OperationRequest, decode_payload, payload_digest

from .artifacts import digest
from .execution_contract import ExecutionStep, encode_execution_steps
from .partial_apply_plan import require

PHASE = "prepare-partial-apply"


def preparation_identity(source, fence, evidence=None):
    identity = [source.org_id, source.workspace_id, source.operation_id, fence]
    if evidence is not None and evidence.get("version") == 2:
        identity.append(evidence["record_id"])
    scope = digest(identity)
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "superplane-partial-apply:" + scope))


def preparation_request(source, evidence):
    """Inputs must come from committed admission and protected owner evidence.

    This pure compiler is not an authority check. Gateway must independently
    validate the original lease, expiry, evidence and current human approval.
    """
    original = decode_payload(source.request_payload)
    previous = dict(original.parameters)
    require(
        source.state == "running"
        and original.action == "provision"
        and payload_digest(original) == source.plan_digest
        and previous.get("lifecycle_phase") == "apply-infrastructure",
        "preparation requires original partial apply admission",
    )
    request = json.loads(previous["lifecycle_request"])
    public = json.loads(previous["lifecycle_inputs"])
    require(
        request.get("mode") == "existing-account-managed"
        and public.get("isolation_mode") == "dedicated"
        and not previous.get("shared_membership")
        and request.get("workspace_id") == source.workspace_id,
        "preparation requires dedicated managed ownership",
    )
    require(
        all(
            evidence.get(k) == getattr(source, k)
            for k in ("operation_id", "org_id", "workspace_id", "plan_digest")
        )
        and evidence.get("source_version") == source.version
        and evidence.get("original_artifact_id")
        == previous.get("lifecycle_artifact_id")
        and evidence.get("evidence_kind") == "partial-apply-state-v1"
        and evidence.get("apply_started") is True
        and evidence.get("account_id")
        == previous.get("aws_account_id")
        == request.get("target_account_id")
        and evidence.get("region") == request.get("region")
        and evidence.get("credential_id") == previous.get("credential_id")
        and type(evidence.get("source_fence")) is int
        and 1 <= evidence["source_fence"] <= 32
        and isinstance(evidence.get("snapshot"), dict),
        "preparation evidence differs from original apply",
    )
    require(
        isinstance(evidence.get("record_id"), str)
        and re.fullmatch(r"sppa1:[a-f0-9]{64}", evidence["record_id"]),
        "protected partial apply evidence required",
    )
    identifier = preparation_identity(source, evidence["source_fence"], evidence)
    parameters = {
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
    parameters.update(
        lifecycle_phase=PHASE,
        allocation_id=str(
            uuid.uuid5(
                uuid.NAMESPACE_URL, "superplane-partial-apply-allocation:" + identifier
            )
        ),
        max_resource_units="0",
        max_cost_micros="0",
        partial_apply_source_operation_id=source.operation_id,
        partial_apply_source_plan_digest=source.plan_digest,
        partial_apply_source_version=str(source.version),
        partial_apply_source_fence=str(evidence["source_fence"]),
        partial_apply_source_artifact_id=previous["lifecycle_artifact_id"],
        partial_apply_original_allocation_id=allocation_id_for(source),
        partial_apply_owner_evidence_id=evidence["record_id"],
        partial_apply_owner_evidence_sha256=digest(evidence),
        partial_apply_snapshot_sha256=digest(evidence["snapshot"]),
    )
    # Keep the root preview and all budget/runtime/input commitments unchanged.
    # The new recipe descriptor binds the separate preparation, not a replay of
    # the original apply or an ordinary initial planning operation.
    recipe = digest({k: v for k, v in parameters.items() if k != "execution_steps"})
    parameters["execution_steps"] = encode_execution_steps(
        [ExecutionStep(PHASE, "superplane-lifecycle", PHASE, recipe)]
    )
    return OperationRequest("provision", identifier, parameters)


async def source_admission(connection, *, operation_id, org_id, workspace_id):
    from harness_jobs.store import _record

    row = await connection.fetchrow(
        "SELECT * FROM harness_operations WHERE operation_id=$1 AND org_id=$2 AND workspace_id=$3",
        operation_id,
        org_id,
        workspace_id,
    )
    require(row is not None, "original apply unavailable")
    source = _record(row)
    paid = await connection.fetchrow(
        "SELECT * FROM harness_approval_consumption WHERE operation_id=$1",
        operation_id,
    )
    require(
        paid is not None
        and paid["reservation_state"] == "confirmed"
        and all(
            paid[k] == getattr(source, k)
            for k in ("org_id", "workspace_id", "plan_digest")
        )
        and row["cancel_requested_at"] is None
        and not row["cleanup_required"],
        "original apply reservation or execution changed",
    )
    return source, dict(paid)


async def expired_predecessors(
    connection, domain, source, evidence, *, operation_id=None
):
    """A fresh human-approved inspection can replace only unstarted stale work.

    Every older admission and attempt remains intact. A provider intent, artifact,
    active lease, missing registration or still-fresh proof prevents succession.
    This grants no execution authority and does not recover the paid apply.
    """
    rows = await connection.fetch(
        "SELECT o.*,l.holder,l.expires_at AS lease_expiry,l.runtime_deadline "
        "FROM harness_operations o LEFT JOIN harness_operation_leases l USING(operation_id) "
        "WHERE o.org_id=$1 AND o.workspace_id=$2 AND o.action='provision' "
        "AND o.request_payload::jsonb->'parameters'->>'lifecycle_phase'='prepare-partial-apply' "
        "AND o.request_payload::jsonb->'parameters'->>'partial_apply_source_operation_id'=$3 LIMIT 6",
        source.org_id,
        source.workspace_id,
        source.operation_id,
    )
    rows = [r for r in rows if r["operation_id"] != operation_id]
    require(len(rows) < 5, "preparation inspection limit reached")
    if not rows:
        return
    require(
        evidence.get("version") == 2, "new preparation requires a new owner inspection"
    )
    checked = datetime.fromisoformat(evidence["checked_at"])
    now = datetime.now(timezone.utc)
    for row in rows:
        previous = await domain.fetchrow(
            "SELECT owner_evidence_json FROM workspace_partial_apply_preparations "
            "WHERE operation_id=$1 AND org_id=$2 AND workspace_id=$3 AND source_operation_id=$4",
            row["operation_id"],
            source.org_id,
            source.workspace_id,
            source.operation_id,
        )
        require(previous is not None, "previous preparation registration unavailable")
        old = json.loads(previous["owner_evidence_json"])
        require(
            old["record_id"] != evidence["record_id"]
            and datetime.fromisoformat(old["expires_at"]) <= checked <= now
            and (row["holder"] is None or row["lease_expiry"] <= now)
            and not await connection.fetchval(
                "SELECT EXISTS(SELECT 1 FROM harness_provider_call_intent WHERE operation_id=$1)",
                row["operation_id"],
            )
            and not await domain.fetchval(
                "SELECT EXISTS(SELECT 1 FROM workspace_lifecycle_artifacts WHERE source_operation_id=$1)",
                row["operation_id"],
            ),
            "previous preparation is fresh, active or has provider history",
        )


async def registration_values(
    connection, domain, *, operation_id, org_id, workspace_id
):
    """Reconstruct only from committed shared admissions and immutable evidence."""
    row = await connection.fetchrow(
        "SELECT * FROM harness_operations WHERE operation_id=$1 AND org_id=$2 AND workspace_id=$3",
        operation_id,
        org_id,
        workspace_id,
    )
    require(row is not None, "preparation admission unavailable")
    request = decode_payload(row["request_payload"])
    p = request.parameters
    source, paid = await source_admission(
        connection,
        operation_id=p["partial_apply_source_operation_id"],
        org_id=org_id,
        workspace_id=workspace_id,
    )
    registration = await domain.fetchrow(
        "SELECT * FROM workspace_partial_apply_preparations WHERE operation_id=$1 AND org_id=$2 AND workspace_id=$3",
        operation_id,
        org_id,
        workspace_id,
    )
    require(registration is not None, "preparation is not registered")
    evidence = json.loads(registration["owner_evidence_json"])
    expected = preparation_request(source, evidence)
    require(
        request == expected and payload_digest(request) == row["plan_digest"],
        "preparation changed original scope or evidence",
    )
    if evidence.get("version") == 2:
        await expired_predecessors(
            connection, domain, source, evidence, operation_id=operation_id
        )
    preparation_paid = await connection.fetchrow(
        "SELECT * FROM harness_approval_consumption WHERE operation_id=$1",
        operation_id,
    )
    require(
        preparation_paid is not None
        and preparation_paid["reservation_state"] in {"confirmed", "released"}
        and preparation_paid["max_resource_units"]
        == preparation_paid["max_cost_micros"]
        == 0
        and preparation_paid["requester"] == paid["requester"]
        and preparation_paid["plan_digest"] == row["plan_digest"]
        and preparation_paid["org_id"] == org_id
        and preparation_paid["workspace_id"] == workspace_id,
        "preparation must retain original requester without paid infrastructure",
    )
    require(
        await domain.fetchval(
            "SELECT EXISTS(SELECT 1 FROM workspaces WHERE id::text=$1 AND org_id::text=$2 "
            "AND provisioning_operation_id=$3 AND status='Provisioning' AND cluster_id IS NULL AND is_default=false)",
            workspace_id,
            org_id,
            source.operation_id,
        ),
        "workspace no longer belongs to original partial apply",
    )
    values = {
        "operation_id": operation_id,
        "org_id": org_id,
        "workspace_id": workspace_id,
        "source_operation_id": source.operation_id,
        "source_fence": evidence["source_fence"],
        "source_version": source.version,
        "plan_digest": row["plan_digest"],
        "owner_evidence_sha256": digest(evidence),
        "original_allocation_id": allocation_id_for(source),
    }
    require(
        all(registration[k] == v for k, v in values.items()),
        "preparation registration changed",
    )
    return dict(registration), evidence
