"""Current paid source reconstruction for dedicated managed workspace removal."""

import json
from dataclasses import asdict

from harness_jobs.identity import decode_payload, payload_digest

from app.adapters.operation_authority_source import GrantBackedAuthority
from app.database import async_session_factory
from app.services.provisioning import (
    ProvisioningRefused,
    ProvisioningUnavailable,
    _start,
)
from workspace_provisioning.artifacts import digest, read_artifact
from workspace_provisioning.lifecycle_policy import policy_digest
from workspace_provisioning.retirement_managed_access import (
    compile_managed_access_review,
    managed_recipe_inputs,
    require_managed_control_source,
)
from workspace_provisioning.retirement_request import retirement_request


async def require_runtime(org_id):
    from app.operation_activation import require_installed_lifecycle_binding

    try:
        from workspace_provisioning.retirement_composer import NATIVE_RETIREMENT_VERSION
    except ImportError:
        raise ProvisioningUnavailable(
            "native retirement runtime is unavailable"
        ) from None
    if NATIVE_RETIREMENT_VERSION != 1:
        raise ProvisioningUnavailable("native retirement runtime is unavailable")
    await require_installed_lifecycle_binding(str(org_id))


async def preview(composition, db, org_id, workspace_id, request_id):
    from app.services.retirement import retirement_facts

    await require_runtime(org_id)
    (
        workspace,
        principal,
        source,
        bootstrap,
        inventory,
        _,
        policy,
        runtime,
    ) = await retirement_facts(
        composition, db, org_id, workspace_id, access_review=True
    )
    if inventory.cluster_ownership != "adp-created":
        raise ProvisioningUnavailable("only dedicated managed retirement is composed")
    original_request_id = workspace.operation_id
    async with composition.domain_connect() as connection:
        rows = await connection.fetch(
            "SELECT a.artifact_id FROM workspace_lifecycle_control_operations c "
            "JOIN workspace_lifecycle_artifacts a ON a.source_operation_id=c.operation_id "
            "AND a.org_id=c.org_id AND a.workspace_id=c.workspace_id "
            "WHERE c.org_id=$1 AND c.workspace_id=$2 AND c.source_bootstrap_operation_id=$3 "
            "AND c.request_id=$4 AND c.phase='prepare-retirement-access' LIMIT 2",
            str(org_id),
            str(workspace_id),
            source.operation_id,
            str(request_id),
        )
    if len(rows) != 1:
        raise ProvisioningUnavailable(
            "complete the original approved cleanup preparation first"
        )
    row = await read_artifact(
        composition.domain_connect,
        artifact_id=rows[0]["artifact_id"],
        org_id=str(org_id),
        workspace_id=str(workspace_id),
        require_fresh=False,
    )
    parameters = json.loads(row["parameters_json"])
    if parameters.get("retirement_prepare_destroy") != "v1":
        raise ProvisioningUnavailable(
            "cleanup evidence has no reviewed managed destroy"
        )
    plan = compile_managed_access_review(
        inventory,
        runtime,
        original_allocation_id=parameters["original_allocation_id"],
        bootstrap_artifact_id=bootstrap["artifact_id"],
        retirement_request_id=str(request_id),
        prepare_destroy=True,
        **managed_recipe_inputs(inventory, runtime),
    )
    async with composition.operation_connect() as connection:
        await require_managed_control_source(
            connection,
            plan=plan,
            access_artifact=row,
            paid_operation_id=source.admitted_request().parameters[
                "lifecycle_source_operation_id"
            ],
        )
    request, deletion = retirement_request(inventory, plan, row, source, policy)
    from app.services.lifecycle_evidence import (
        cleanup_preparation,
        workspace_pointer_matches,
    )

    preparation = await cleanup_preparation(
        composition.operation_connect,
        row=row,
        plan=plan,
        request=request,
        source_operation_id=source.operation_id,
    )
    if not await workspace_pointer_matches(
        db, org_id, workspace_id, original_request_id, source.operation_id
    ):
        raise ProvisioningUnavailable("workspace provisioning identity changed")
    review = {
        "request_id": str(request_id),
        "workspace_id": str(workspace_id),
        "source_operation_id": source.operation_id,
        "source_payload_digest": source.plan_digest,
        "lifecycle_artifact_id": bootstrap["artifact_id"],
        "account_id": inventory.cluster_arn.split(":")[4],
        "region": inventory.cluster_arn.split(":")[3],
        "inventory_sha256": digest(asdict(inventory)),
        "lifecycle_policy_sha256": policy_digest(policy),
        "runtime_config_sha256": digest(runtime),
        "steps": [asdict(step) for step in deletion.steps],
        "preserved": list(deletion.preserved),
        "admission_available": True,
        "blocked_reason": None,
        "revision": payload_digest(request),
        "cleanup_preparation": preparation,
        "approval_request": {
            "workspace_id": str(workspace_id),
            "action": request.action,
            "idempotency_key": request.idempotency_key,
            "parameters": dict(request.parameters),
        },
    }
    return workspace, principal, request, review


async def admit(
    composition, db, org_id, workspace_id, request_id, revision, approval_id
):
    from app.services.retirement import _workspace

    await require_runtime(org_id)
    workspace, principal = await _workspace(db, org_id, workspace_id)
    await db.refresh(workspace, with_for_update=True)
    async with composition.operation_connect() as connection:
        prior = await connection.fetchrow(
            "SELECT o.*,c.approval_id FROM harness_operations o JOIN harness_approval_consumption c USING(operation_id) "
            "WHERE o.org_id=$1 AND o.workspace_id=$2 AND o.action='teardown' "
            "AND o.request_payload::jsonb->'parameters'->>'lifecycle_phase'='retire-workspace' "
            "AND o.request_payload::jsonb->'parameters'->>'retirement_source_operation_id'=$3 "
            "ORDER BY o.created_at LIMIT 1",
            str(org_id),
            str(workspace_id),
            workspace.provisioning_operation_id,
        )
    if prior is not None:
        request = decode_payload(prior["request_payload"])
        if (
            request.idempotency_key != str(request_id)
            or payload_digest(request) != prior["plan_digest"]
            or revision != prior["plan_digest"]
            or str(approval_id) != prior["approval_id"]
        ):
            raise ProvisioningRefused("recover the original approved removal request")
        operation_id, state = prior["operation_id"], prior["state"]
    else:
        if (
            workspace.status not in {"Active", "active"}
            or workspace.teardown_operation_id
        ):
            raise ProvisioningRefused("workspace already has a retirement operation")
        workspace, principal, request, review = await preview(
            composition, db, org_id, workspace_id, request_id
        )
        if review["revision"] != revision:
            raise ProvisioningRefused("retirement review changed; review it again")
        approval = await GrantBackedAuthority(async_session_factory).approval_for(
            principal=principal, request=request
        )
        if approval.record is None or approval.record.approval_id != str(approval_id):
            raise ProvisioningRefused(
                "retirement approval differs from its exact saved plan"
            )
        progress = await _start(
            operation_id=request.idempotency_key,
            action=request.action,
            workspace_id=str(workspace_id),
            org_id=str(org_id),
            parameters=dict(request.parameters),
        )
        operation_id, state = progress.operation_id, progress.state
    if workspace.teardown_operation_id not in {None, operation_id}:
        raise ProvisioningRefused("workspace is bound to another retirement")
    workspace.teardown_operation_id = operation_id
    if workspace.status in {"Active", "active"}:
        workspace.status = "Teardown"
    await db.commit()
    return {
        "request_id": str(request_id),
        "workspace_id": str(workspace_id),
        "operation_id": operation_id,
        "phase": "retire-workspace",
        "state": state,
        "retryable": False,
        "retirement_complete": False,
        "original_allocation_id": request.parameters["original_allocation_id"],
        "control_allocation_id": request.parameters["control_allocation_id"],
    }
