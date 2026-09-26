"""Human approval and durable registration of a separate cleanup allocation."""

from dataclasses import asdict

from harness_jobs.allocation import allocation_id_for
from harness_jobs.identity import decode_payload, payload_digest
from workspace_provisioning.retirement_access_authority import access_request
from workspace_provisioning.retirement_access_plan import PHASE, compile_access_plan

from app.adapters.operation_authority_source import GrantBackedAuthority
from app.database import async_session_factory
from app.services.provisioning import ProvisioningRefused, start_planned_provision
from app.services.retirement import _workspace, retirement_facts


async def preview_access(composition, db, org_id, workspace_id, retirement_request_id):
    (
        workspace,
        principal,
        source,
        _,
        inventory,
        deletion,
        policy,
        runtime,
    ) = await retirement_facts(composition, db, org_id, workspace_id)
    if workspace.status not in {"Active", "active"}:
        raise ProvisioningRefused("cleanup access requires an active workspace")
    plan = compile_access_plan(
        inventory,
        runtime,
        original_allocation_id=allocation_id_for(source),
        retirement_request_id=str(retirement_request_id),
    )
    request = access_request(plan, source, policy)
    review = {
        "retirement_request_id": str(retirement_request_id),
        "request_id": request.idempotency_key,
        "workspace_id": str(workspace_id),
        "phase": PHASE,
        "revision": payload_digest(request),
        "source_operation_id": source.operation_id,
        "allocation_id": plan.allocation_id,
        "original_allocation_id": plan.original_allocation_id,
        "inventory_sha256": plan.inventory_sha256,
        "access_plan": asdict(plan),
        "authority": {
            "registrar": {
                "policy": "AmazonEKSAdminPolicy",
                "namespaces": list(plan.registrar_namespaces),
                "description": "Temporary namespace administrator authority, including reading secrets, until exact cleanup grants are revoked.",
            },
            "cleaner": {
                "description": "Get and delete the named owned objects; list pods, services, secrets, deployments and jobs in the workspace namespace.",
            },
        },
        "preserved": list(deletion.preserved),
        "max_resource_units": 0,
        "max_cost_micros": 0,
        "approval_request": {
            "workspace_id": str(workspace_id),
            "action": request.action,
            "idempotency_key": request.idempotency_key,
            "parameters": dict(request.parameters),
        },
    }
    return workspace, principal, request, review


async def _register(composition, db, workspace, record, request):
    from app.adapters.lifecycle_control_registry import register_control_operation

    if (
        workspace.status not in {"Active", "active"}
        or workspace.provisioning_operation_id
        != request.parameters["retirement_source_operation_id"]
    ):
        raise ProvisioningRefused("cleanup source is no longer the active bootstrap")
    async with composition.operation_connect() as connection, connection.transaction():
        await register_control_operation(
            connection,
            operation_id=record["operation_id"],
            org_id=str(workspace.org_id),
            workspace_id=str(workspace.id),
            source_bootstrap_operation_id=workspace.provisioning_operation_id,
            request_id=request.parameters["retirement_request_id"],
        )
    # Release the workspace lock only after the separate registration is durable.
    # An interrupted caller can repeat this exact admission without repurchasing it.
    await db.commit()
    return {
        "retirement_request_id": request.parameters["retirement_request_id"],
        "request_id": request.idempotency_key,
        "control_operation_id": record["operation_id"],
        "workspace_id": str(workspace.id),
        "phase": PHASE,
        "state": record["state"],
        "retryable": False,
    }


async def admit_access(
    composition, db, org_id, workspace_id, retirement_request_id, revision, approval_id
):
    from app.operation_activation import require_admission_enabled

    require_admission_enabled()
    workspace, _ = await _workspace(db, org_id, workspace_id)
    # Serialize against other lifecycle requests, including API replicas whose
    # shared admission committed but whose domain transaction was interrupted.
    await db.refresh(workspace, with_for_update=True)
    async with composition.operation_connect() as connection:
        existing = await connection.fetchrow(
            "SELECT o.*,c.approval_id FROM harness_operations o "
            "JOIN harness_approval_consumption c USING(operation_id) "
            "WHERE o.org_id=$1 AND o.workspace_id=$2 AND o.action='provision' "
            "AND o.request_payload::jsonb->'parameters'->>'lifecycle_phase'=$3 "
            "AND o.request_payload::jsonb->'parameters'->>'retirement_source_operation_id'=$4 "
            "ORDER BY o.operation_id LIMIT 1",
            str(org_id),
            str(workspace_id),
            PHASE,
            workspace.provisioning_operation_id,
        )
    if existing is not None:
        request = decode_payload(existing["request_payload"])
        if (
            request.parameters.get("retirement_request_id")
            != str(retirement_request_id)
            or payload_digest(request) != existing["plan_digest"]
            or existing["plan_digest"] != revision
            or existing["approval_id"] != str(approval_id)
        ):
            raise ProvisioningRefused(
                "this bootstrap already has a cleanup allocation; recover its original request"
            )
        return await _register(composition, db, workspace, existing, request)

    workspace, principal, request, review = await preview_access(
        composition, db, org_id, workspace_id, retirement_request_id
    )
    if review["revision"] != revision:
        raise ProvisioningRefused("cleanup access review changed; review it again")
    approval = await GrantBackedAuthority(async_session_factory).approval_for(
        principal=principal, request=request
    )
    if approval.record is None or approval.record.approval_id != str(approval_id):
        raise ProvisioningRefused(
            "cleanup access approval differs from its exact recipe"
        )
    progress = await start_planned_provision(
        operation_id=request.idempotency_key,
        workspace_id=str(workspace_id),
        org_id=str(org_id),
        parameters=dict(request.parameters),
    )
    return await _register(
        composition,
        db,
        workspace,
        {"operation_id": progress.operation_id, "state": progress.state},
        request,
    )
