"""Public review and admission of exact recorded workspace lifecycle artifacts."""

import json
import hashlib

from harness_jobs.identity import OperationRequest, decode_payload, payload_digest
from sqlalchemy import select

from app.adapters.operation_authority_source import GrantBackedAuthority
from app.database import async_session_factory
from app.models.workspace import Workspace
from app.services.onboarding import policy_for
from app.services.provisioning import (
    ProvisioningRefused,
    ProvisioningUnavailable,
    start_planned_provision,
)


async def workspace_scope(db, org_id, workspace_id):
    workspace = await db.scalar(
        select(Workspace).where(
            Workspace.id == workspace_id, Workspace.org_id == org_id
        )
    )
    if workspace is None:
        raise ProvisioningRefused("workspace is not available")
    principal = await GrantBackedAuthority(async_session_factory).resolve(
        org_id=str(org_id),
        workspace_id=str(workspace_id),
        permission="workspace:provision",
    )
    if principal is None or workspace.status in {"Deleted", "Teardown"}:
        raise ProvisioningRefused("workspace lifecycle authority refused")
    return workspace, principal


async def verified_proposal(composition, org_id, workspace_id, artifact_id):
    from workspace_provisioning.artifacts import read_artifact

    artifact = await read_artifact(
        composition.operation_connect,
        artifact_id=artifact_id,
        org_id=str(org_id),
        workspace_id=str(workspace_id),
    )
    async with composition.operation_connect() as connection:
        original = await connection.fetchrow(
            "SELECT * FROM harness_operations WHERE operation_id=$1 AND org_id=$2 AND workspace_id=$3",
            artifact["source_operation_id"],
            str(org_id),
            str(workspace_id),
        )
    if original is None or original["state"] != "succeeded":
        raise ProvisioningRefused("source lifecycle phase has not completed")
    source = decode_payload(original["request_payload"])
    if (
        payload_digest(source) != original["plan_digest"]
        or original["job_id"] != artifact["source_job_id"]
        or original["attempt_id"] != artifact["source_attempt_id"]
        or dict(source.parameters) != json.loads(artifact["parameters_json"])
    ):
        raise ProvisioningRefused("lifecycle proposal original admission mismatch")
    return artifact


async def preview_continuation(
    composition, db, org_id, workspace_id, artifact_id, request_id
):
    from workspace_provisioning.artifacts import continuation_parameters, proposal
    from workspace_provisioning.runtime_config import validate_runtime_config
    from workspace_provisioning.lifecycle_policy import policy_digest

    workspace, principal = await workspace_scope(db, org_id, workspace_id)
    artifact = await verified_proposal(composition, org_id, workspace_id, artifact_id)
    policy = policy_for(org_id)
    parameters = continuation_parameters(artifact)
    original_request = json.loads(parameters["lifecycle_request"])
    mode = {
        "existing-account-managed": "managed",
        "bring-existing-cluster": "adopt",
        "new-account-managed": "new-account-managed",
    }.get(original_request.get("mode"))
    if (
        mode not in policy.permitted_modes
        or original_request.get("region") not in policy.permitted_regions
        or original_request.get("organization_id") != policy.aws_organization_id
        or original_request.get("management_account_id") != policy.management_account_id
        or original_request.get("workspace_id") != str(workspace_id)
    ):
        raise ProvisioningRefused("lifecycle proposal is outside current target policy")
    runtime = validate_runtime_config(policy.runtime)
    current_digest = hashlib.sha256(
        json.dumps(
            runtime, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()
    if parameters.get("runtime_config_sha256") != current_digest or parameters.get(
        "lifecycle_policy_sha256"
    ) != policy_digest(policy):
        raise ProvisioningRefused(
            "lifecycle runtime policy changed; prepare a new proposal"
        )
    account_id = artifact["account_id"]
    if (
        mode != "new-account-managed"
        and account_id not in policy.permitted_target_accounts
    ):
        raise ProvisioningRefused("lifecycle account is no longer authorized")
    credential_account = (
        policy.management_account_id if mode == "new-account-managed" else account_id
    )
    reference = policy.credential_references.get(credential_account)
    if reference is None:
        raise ProvisioningUnavailable(
            "recorded target account credential is not configured"
        )
    if any(
        parameters.get(key) != value for key, value in reference.model_dump().items()
    ):
        raise ProvisioningRefused(
            "lifecycle credential changed; prepare a fresh proposal"
        )
    parameters.update(
        **reference.model_dump(),
        provider="aws",
        provider_account_id=credential_account,
        aws_account_id=credential_account,
    )
    request = OperationRequest(
        action="provision", idempotency_key=str(request_id), parameters=parameters
    )
    return (
        workspace,
        principal,
        request,
        {
            **proposal(artifact),
            "request_id": str(request_id),
            "revision": payload_digest(request),
            "approval_request": {
                "workspace_id": str(workspace_id),
                "action": "provision",
                "idempotency_key": str(request_id),
                "parameters": parameters,
            },
        },
    )


async def continue_lifecycle(
    composition, db, org_id, workspace_id, artifact_id, request_id, approval_id
):
    from app.operation_activation import require_installed_lifecycle_binding

    await require_installed_lifecycle_binding(str(org_id))
    workspace, _ = await workspace_scope(db, org_id, workspace_id)
    async with composition.operation_connect() as connection:
        existing = await connection.fetchrow(
            "SELECT o.*,c.approval_id FROM harness_operations o JOIN harness_approval_consumption c USING(operation_id) "
            "WHERE o.org_id=$1 AND o.workspace_id=$2 AND o.idempotency_key=$3",
            str(org_id),
            str(workspace_id),
            str(request_id),
        )
    if existing is not None:
        admitted = decode_payload(existing["request_payload"])
        if (
            payload_digest(admitted) != existing["plan_digest"]
            or admitted.action != "provision"
            or admitted.parameters.get("lifecycle_artifact_id") != artifact_id
            or existing["approval_id"] != str(approval_id)
        ):
            raise ProvisioningRefused(
                "continuation identity was already used for different work"
            )
        # Recover an admission whose workspace transaction was lost. This read
        # does not require the original approval or artifact to remain unexpired.
        await db.refresh(workspace, with_for_update=True)
        if workspace.provisioning_operation_id == admitted.parameters.get(
            "lifecycle_source_operation_id"
        ):
            workspace.provisioning_operation_id = existing["operation_id"]
            workspace.status = "Provisioning"
            await db.commit()
        return {
            "request_id": str(request_id),
            "provisioning_operation_id": existing["operation_id"],
            "workspace_id": str(workspace_id),
            "state": existing["state"],
            "phase": admitted.parameters.get("lifecycle_phase"),
            "retryable": False,
        }
    workspace, principal, request, review = await preview_continuation(
        composition, db, org_id, workspace_id, artifact_id, request_id
    )
    approval = await GrantBackedAuthority(async_session_factory).approval_for(
        principal=principal, request=request
    )
    if approval.record is None or approval.record.approval_id != str(approval_id):
        raise ProvisioningRefused(
            "continuation approval does not match the exact recorded artifact"
        )
    # Serialize competing continuations across independent API replicas. Original
    # admission commits separately, so registration is rechecked by the dispatcher.
    await db.refresh(workspace, with_for_update=True)
    async with composition.operation_connect() as connection:
        current = await connection.fetchrow(
            "SELECT state,request_payload FROM harness_operations WHERE operation_id=$1 AND org_id=$2 AND workspace_id=$3",
            workspace.provisioning_operation_id,
            str(org_id),
            str(workspace_id),
        )
    if current is None:
        raise ProvisioningUnavailable("current lifecycle operation is unavailable")
    active_request = decode_payload(current["request_payload"])
    same_request = active_request.idempotency_key == str(request_id)
    if not same_request and (
        current["state"] != "succeeded"
        or workspace.provisioning_operation_id != review["source_operation_id"]
    ):
        raise ProvisioningRefused(
            "another lifecycle operation already owns this workspace"
        )
    # Admission uses the shared connection and can survive a failed workspace
    # commit. The locked source must have only one paid successor even when that
    # successor never registered; a different request cannot purchase it again.
    async with composition.operation_connect() as connection:
        successor = await connection.fetchrow(
            "SELECT o.idempotency_key,o.request_payload,o.plan_digest FROM harness_operations o "
            "JOIN harness_approval_consumption c USING(operation_id) "
            "WHERE o.org_id=$1 AND o.workspace_id=$2 AND o.action='provision' "
            "AND o.request_payload::jsonb->'parameters'->>'lifecycle_source_operation_id'=$3 "
            "AND o.idempotency_key<>$4 LIMIT 1",
            str(org_id),
            str(workspace_id),
            review["source_operation_id"],
            str(request_id),
        )
    if successor is not None:
        raise ProvisioningRefused(
            "this source already has an admitted continuation; recover its original request"
        )
    progress = await start_planned_provision(
        operation_id=str(request_id),
        workspace_id=str(workspace_id),
        org_id=str(org_id),
        parameters=dict(request.parameters),
    )
    workspace.provisioning_operation_id = progress.operation_id
    workspace.status = "Provisioning"
    await db.commit()
    return {
        "request_id": str(request_id),
        "provisioning_operation_id": progress.operation_id,
        "workspace_id": str(workspace_id),
        "state": progress.state,
        "phase": review["phase"],
        "retryable": False,
    }
