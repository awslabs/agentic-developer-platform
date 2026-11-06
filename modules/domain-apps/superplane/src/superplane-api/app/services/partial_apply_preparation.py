"""Review and register a distinct preparation without moving the workspace owner."""

from harness_jobs.allocation import allocation_id_for
from harness_jobs.identity import decode_payload, payload_digest
from workspace_provisioning.artifacts import canonical, digest
from workspace_provisioning.partial_apply_plan import require
from workspace_provisioning.partial_apply_request import (
    preparation_request,
    registration_values,
    source_admission,
    expired_predecessors,
)

from app.adapters.operation_authority_source import GrantBackedAuthority
from app.database import async_session_factory
from app.services.lifecycle_proposals import workspace_scope
from app.services.provisioning import start_planned_provision


async def preview(composition, db, org_id, workspace_id, *, owner_evidence_id=None):
    workspace, principal = await workspace_scope(db, org_id, workspace_id)
    require(
        workspace.status == "Provisioning"
        and workspace.cluster_id is None
        and not workspace.is_default,
        "workspace is not the original partial apply",
    )
    async with composition.operation_connect() as connection:
        source, paid = await source_admission(
            connection,
            operation_id=workspace.provisioning_operation_id,
            org_id=str(org_id),
            workspace_id=str(workspace_id),
        )
    require(principal.subject == paid["requester"], "original requester required")
    require(composition.dispatcher is not None, "protected producer unavailable")
    proof = await composition.dispatcher.transport.post(
        "/partial-apply-observation",
        {
            "domain": "superplane",
            "org_id": str(org_id),
            "workspace_id": str(workspace_id),
            "operation_id": source.operation_id,
            "requester": principal.subject,
            "expected_version": source.version,
            **({"owner_evidence_id": owner_evidence_id} if owner_evidence_id else {}),
        },
        distinguish_denial=True,
    )
    evidence = proof["owner_evidence"]
    request = preparation_request(source, evidence)
    return (
        workspace,
        principal,
        source,
        evidence,
        request,
        {
            "workspace_id": str(workspace_id),
            "source_operation_id": source.operation_id,
            "request_id": request.idempotency_key,
            "revision": payload_digest(request),
            "phase": "prepare-partial-apply",
            "original_allocation_id": allocation_id_for(source),
            "description": "Prepare a fresh plan against the retained partial state. This approves planning only; infrastructure repair needs a separate exact-plan decision.",
            "retained_resources": evidence["snapshot"]["resources"],
            "state_sha256": evidence["snapshot"]["sha256"],
            "max_resource_units": 0,
            "max_cost_micros": 0,
            "approval_request": {
                "workspace_id": str(workspace_id),
                "action": "provision",
                "idempotency_key": request.idempotency_key,
                "parameters": dict(request.parameters),
            },
        },
    )


async def admit(
    composition,
    db,
    org_id,
    workspace_id,
    request_id,
    revision,
    approval_id,
    *,
    owner_evidence_id=None,
):
    from app.operation_activation import require_installed_lifecycle_binding

    await require_installed_lifecycle_binding(str(org_id))
    workspace, principal = await workspace_scope(db, org_id, workspace_id)
    await db.refresh(workspace, with_for_update=True)
    async with composition.operation_connect() as connection:
        history = await connection.fetch(
            "SELECT o.*,c.approval_id,c.requester FROM harness_operations o JOIN harness_approval_consumption c USING(operation_id) "
            "WHERE o.org_id=$1 AND o.workspace_id=$2 AND o.action='provision' "
            "AND o.request_payload::jsonb->'parameters'->>'lifecycle_phase'='prepare-partial-apply' "
            "AND o.request_payload::jsonb->'parameters'->>'partial_apply_source_operation_id'=$3 LIMIT 6",
            str(org_id),
            str(workspace_id),
            workspace.provisioning_operation_id,
        )
    require(len(history) <= 5, "preparation inspection limit reached")
    rows = [r for r in history if r["idempotency_key"] == str(request_id)]
    require(len(rows) <= 1, "preparation identity is ambiguous")
    if rows:
        record = dict(rows[0])
        require(record["requester"] == principal.subject, "original requester required")
        request = decode_payload(record["request_payload"])
        require(
            request.idempotency_key == str(request_id)
            and record["plan_digest"] == revision
            and record["approval_id"] == str(approval_id)
            and payload_digest(request) == revision,
            "source already has a different preparation; retain original request",
        )
        async with composition.domain_connect() as domain:
            registered = await domain.fetchrow(
                "SELECT * FROM workspace_partial_apply_preparations WHERE operation_id=$1",
                record["operation_id"],
            )
        if registered is not None:
            async with (
                composition.operation_connect() as connection,
                composition.domain_connect() as domain,
            ):
                await registration_values(
                    connection,
                    domain,
                    operation_id=record["operation_id"],
                    org_id=str(org_id),
                    workspace_id=str(workspace_id),
                )
            await db.commit()
            return receipt(record, workspace)
    _, principal, source, evidence, request, review = await preview(
        composition, db, org_id, workspace_id, owner_evidence_id=owner_evidence_id
    )
    require(
        request.idempotency_key == str(request_id) and review["revision"] == revision,
        "preparation review changed",
    )
    if not rows:
        require(len(history) < 5, "preparation inspection limit reached")
        async with (
            composition.operation_connect() as connection,
            composition.domain_connect() as domain,
        ):
            await expired_predecessors(connection, domain, source, evidence)
        approval = await GrantBackedAuthority(async_session_factory).approval_for(
            principal=principal, request=request
        )
        require(
            approval.record is not None
            and approval.record.approval_id == str(approval_id),
            "exact preparation approval required",
        )
        progress = await start_planned_provision(
            operation_id=request.idempotency_key,
            workspace_id=str(workspace_id),
            org_id=str(org_id),
            parameters=dict(request.parameters),
        )
        record = {"operation_id": progress.operation_id, "state": progress.state}
    values = {
        "operation_id": record["operation_id"],
        "org_id": str(org_id),
        "workspace_id": str(workspace_id),
        "source_operation_id": source.operation_id,
        "source_fence": evidence["source_fence"],
        "source_version": source.version,
        "plan_digest": revision,
        "original_allocation_id": allocation_id_for(source),
        "owner_evidence_sha256": digest(evidence),
        "owner_evidence_json": canonical(evidence),
    }
    async with (
        composition.operation_connect() as connection,
        composition.domain_connect() as domain,
        domain.transaction(),
    ):
        await domain.execute(
            "INSERT INTO workspace_partial_apply_preparations ("
            + ",".join(values)
            + ") VALUES ("
            + ",".join("$" + str(n) for n in range(1, len(values) + 1))
            + ") ON CONFLICT DO NOTHING",
            *values.values(),
        )
        registered, saved = await registration_values(
            connection,
            domain,
            operation_id=record["operation_id"],
            org_id=str(org_id),
            workspace_id=str(workspace_id),
        )
        require(
            saved == evidence and all(registered[k] == v for k, v in values.items()),
            "preparation registration conflict",
        )
    # Original apply continues to own the workspace and its infrastructure budget.
    await db.commit()
    return receipt(record, workspace)


def receipt(record, workspace):
    return {
        "workspace_id": str(workspace.id),
        "preparation_operation_id": record["operation_id"],
        "source_operation_id": workspace.provisioning_operation_id,
        "state": record["state"],
        "phase": "prepare-partial-apply",
        "workspace_ready": False,
    }
