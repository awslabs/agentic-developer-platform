"""Deployment owner: quota intent, exact approval/admission and durable progress."""

import json
import uuid

from harness_jobs.identity import decode_payload, encode_payload, payload_digest
from sqlalchemy import select
from superplane_executor.deployment_plan import (
    DeploymentPreview,
    MODEL_FIELDS,
    compact,
    document_digest,
    teardown_request,
)
from superplane_executor.deployment_registry import registration_values

from app.config import settings
from app.models.controller_deployment import ControllerDeploymentOperation
from app.models.deployment import Deployment
from app.schemas.proxy import CreateBatchRequest
from app.services.controller_deployments import (
    admit_controller_deployment,
    preview_controller_deployment,
    require_current_approval,
)
from app.services.provisioning import ProvisioningRefused, ProvisioningUnavailable
from app.services.proxy import get_workspace_cluster
from app.services.quota import reserve_deployment_gpus
from app.services.workspace_namespace import resolve_workspace_namespace


def composition(request):
    result = getattr(
        getattr(getattr(request, "app", None), "state", None), "trust_composition", None
    )
    try:
        connect = getattr(result, "operation_connect", None)
    except RuntimeError:
        # The real composition refuses access to this property until its
        # operation authority is installed. Preserve that unavailable outcome.
        connect = None
    if not callable(connect):
        raise ProvisioningUnavailable(
            "governed controller operation store is unavailable"
        )
    return result


def request_document(body):
    if isinstance(body, CreateBatchRequest):
        return {
            "name": body.name,
            "profile_id": body.profile_id,
            "kind": "batch",
            **body.batch_options.model_dump(),
        }
    return {
        "name": body.name,
        "profile_id": body.profile_id,
        **({"expected_namespace":body.expected_namespace} if body.expected_namespace is not None else {}),
        **{key: getattr(body, key) for key in MODEL_FIELDS},
    }


async def preview_create(db, org_id, workspace_id, body):
    if not body.profile_id:
        raise ProvisioningUnavailable(
            "select an installed controller deployment profile"
        )
    workspace, _ = await get_workspace_cluster(workspace_id, org_id, db)
    namespace = resolve_workspace_namespace(workspace)
    if getattr(body, "expected_namespace", None) is not None and body.expected_namespace != namespace:
        raise ProvisioningRefused("Requested namespace does not match the workspace-owned namespace")
    return await preview_controller_deployment(
        db,
        policy_path=settings.superplane_controller_profiles_file,
        org_id=org_id,
        workspace_id=workspace_id,
        request_id=body.operation_id,
        profile_id=body.profile_id,
        name=body.name,
        model_options={}
        if isinstance(body, CreateBatchRequest)
        else {key: getattr(body, key) for key in MODEL_FIELDS},
        workload_kind="batch" if isinstance(body, CreateBatchRequest) else "serving",
        batch_options=body.batch_options.model_dump()
        if isinstance(body, CreateBatchRequest)
        else None,
    )


def stored_preview(deployment):
    if (
        not deployment.controller_request_payload
        or not deployment.controller_approval_id
    ):
        raise ProvisioningRefused(
            "legacy deployment has no governed request; reconcile ownership before mutation"
        )
    try:
        request = decode_payload(deployment.controller_request_payload)
        document = json.loads(deployment.operation_request_json)
        target = json.loads(deployment.operation_target_json)
        if (
            request.action != "provision"
            or request.idempotency_key != str(deployment.operation_id)
            or request.parameters["controller_deployment_id"] != str(deployment.id)
            or request.parameters["controller_request_sha256"]
            != document_digest(document)
            or request.parameters["controller_target_sha256"] != document_digest(target)
            or target["namespace"] != deployment.namespace
            or target["controller_plan"]["workload"]["kind"] != deployment.workload_kind
        ):
            raise ValueError("original intent changed")
        return DeploymentPreview(str(deployment.id), request, document, target)
    except (KeyError, TypeError, ValueError):
        raise ProvisioningRefused(
            "original controller deployment request is invalid"
        ) from None


def require_target(deployment, workspace, cluster):
    target = json.loads(deployment.operation_target_json or "{}")
    if (
        deployment.cluster_id != cluster.id
        or deployment.namespace != resolve_workspace_namespace(workspace)
        or any(
            target.get(key) != value
            for key, value in {
                "cluster_id": str(cluster.id),
                "cluster_arn": cluster.eks_cluster_arn,
                "endpoint": cluster.endpoint,
                "namespace": deployment.namespace,
            }.items()
        )
    ):
        raise ProvisioningRefused(
            "deployment destination changed; refusing to retarget the original request"
        )


async def create(request, db, org_id, workspace_id, body):
    from app.operation_activation import require_admission_enabled

    require_admission_enabled()
    owner = composition(request)
    if not body.approval_id or not body.plan_revision:
        raise ProvisioningRefused(
            "review the deployment preview and supply its human approval and plan revision"
        )
    workspace, cluster = await get_workspace_cluster(
        workspace_id, org_id, db, for_update=True
    )
    namespace = resolve_workspace_namespace(workspace)
    if getattr(body, "expected_namespace", None) is not None and body.expected_namespace != namespace:
        raise ProvisioningRefused("Requested namespace does not match the workspace-owned namespace")
    intent = await db.scalar(
        select(Deployment)
        .where(
            Deployment.org_id == org_id,
            Deployment.operation_id == body.operation_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    document = request_document(body)
    batch = isinstance(body, CreateBatchRequest)
    replicas = 1 if batch else body.replicas
    gpu_count = body.batch_options.gpu_count if batch else body.gpu_per_replica
    if intent is not None:
        if (
            intent.workspace_id != workspace_id
            or intent.workload_kind != ("batch" if batch else "serving")
            or intent.operation_request_json != compact(document)
            or intent.controller_approval_id != str(body.approval_id)
        ):
            raise ProvisioningRefused(
                "operation ID was already used for a different deployment request"
            )
        review = stored_preview(intent)
        require_target(intent, workspace, cluster)
        if payload_digest(review.request) != body.plan_revision:
            raise ProvisioningRefused(
                "replay must name the original reviewed deployment revision"
            )
        if intent.status in {"Deleting", "Deleted", "CancelledBeforeDispatch"}:
            return await progress(owner, db, intent)
    else:
        review = await preview_create(db, org_id, workspace_id, body)
        if payload_digest(review.request) != body.plan_revision:
            raise ProvisioningRefused(
                "deployment profile or target changed since review"
            )
        await require_current_approval(
            org_id=org_id,
            workspace_id=workspace_id,
            request=review.request,
            approval_id=body.approval_id,
        )
        # This is the maintained namespace/quota owner. Its durable Pending row
        # precedes shared admission, and remains reserved across a lost reply.
        intent = await reserve_deployment_gpus(
            workspace_id,
            org_id,
            replicas * gpu_count,
            db,
            deployment_kwargs={
                "id": uuid.UUID(review.deployment_id),
                "cluster_id": cluster.id,
                "namespace": namespace,
                "name": body.name,
                "workload_kind": "batch" if batch else "serving",
                "operation_id": body.operation_id,
                "operation_request_json": compact(review.deployment_request),
                "operation_target_json": compact(review.deployment_target),
                "controller_request_payload": encode_payload(review.request),
                "controller_approval_id": str(body.approval_id),
                "desired_replicas": replicas,
                "gpu_per_replica": gpu_count,
                **(
                    {}
                    if batch
                    else {
                        key: getattr(body, key)
                        for key in MODEL_FIELDS
                        if key not in {"replicas", "gpu_per_replica"}
                    }
                ),
            },
            request=request,
        )
        workspace, cluster = await get_workspace_cluster(
            workspace_id, org_id, db, for_update=True
        )
        await db.refresh(intent, with_for_update=True)
        require_target(intent, workspace, cluster)
    if payload_digest(review.request) != body.plan_revision:
        raise ProvisioningRefused(
            "replay must name the original reviewed deployment revision"
        )
    await admit_controller_deployment(
        owner,
        db,
        org_id=org_id,
        workspace_id=workspace_id,
        preview=review,
        approval_id=body.approval_id,
        revision=body.plan_revision,
    )
    await db.commit()
    return await progress(owner, db, intent)


async def intent_for(
    db,
    org_id,
    workspace_id,
    deployment_id,
    *,
    for_update=False,
    workload_kind="serving",
):
    intent = await db.scalar(
        select(Deployment)
        .where(
            Deployment.id == deployment_id,
            Deployment.org_id == org_id,
            Deployment.workspace_id == workspace_id,
        )
        .with_for_update()
        if for_update
        else select(Deployment).where(
            Deployment.id == deployment_id,
            Deployment.org_id == org_id,
            Deployment.workspace_id == workspace_id,
        )
    )
    if intent is None or intent.workload_kind != workload_kind:
        raise ProvisioningRefused("deployment is not available in this workspace")
    return intent


async def preview_delete(
    request, db, org_id, workspace_id, deployment_id, body, *, workload_kind="serving"
):
    owner = composition(request)
    workspace, cluster = await get_workspace_cluster(workspace_id, org_id, db)
    intent = await intent_for(
        db, org_id, workspace_id, deployment_id, workload_kind=workload_kind
    )
    if intent.status == "CancelledBeforeDispatch":
        raise ProvisioningRefused(
            "workload was cancelled before dispatch; no teardown is required"
        )
    original = stored_preview(intent)
    require_target(intent, workspace, cluster)
    async with owner.operation_connect() as connection:
        from superplane_executor.cleanup_binding import source_for, validate

        source = await source_for(
            connection,
            org_id=str(org_id),
            workspace_id=str(workspace_id),
            deployment_id=str(deployment_id),
        )
        if decode_payload(source["request_payload"]) != original.request:
            raise ProvisioningRefused("original deployment source request changed")
        planned = teardown_request(
            original.request,
            org_id=str(org_id),
            workspace_id=str(workspace_id),
            request_id=str(body.operation_id),
            source_operation_id=source["operation_id"],
        )
        await validate(connection, source, planned)
        await registration_values(
            connection,
            operation_id=source["operation_id"],
            org_id=str(org_id),
            workspace_id=str(workspace_id),
            deployment_id=str(deployment_id),
        )
    planned = teardown_request(
        original.request,
        org_id=str(org_id),
        workspace_id=str(workspace_id),
        request_id=str(body.operation_id),
        source_operation_id=source["operation_id"],
    )
    return DeploymentPreview(
        str(deployment_id),
        planned,
        original.deployment_request,
        original.deployment_target,
    )


async def delete(
    request, db, org_id, workspace_id, deployment_id, body, *, workload_kind="serving"
):
    from app.operation_activation import require_admission_enabled

    require_admission_enabled()
    owner = composition(request)
    if body is None or not body.approval_id or not body.plan_revision:
        raise ProvisioningRefused(
            "review teardown and supply its request ID, human approval and plan revision"
        )
    workspace, cluster = await get_workspace_cluster(
        workspace_id, org_id, db, for_update=True
    )
    intent = await intent_for(
        db,
        org_id,
        workspace_id,
        deployment_id,
        for_update=True,
        workload_kind=workload_kind,
    )
    require_target(intent, workspace, cluster)
    review = await preview_delete(
        request,
        db,
        org_id,
        workspace_id,
        deployment_id,
        body,
        workload_kind=workload_kind,
    )
    await admit_controller_deployment(
        owner,
        db,
        org_id=org_id,
        workspace_id=workspace_id,
        preview=review,
        approval_id=body.approval_id,
        revision=body.plan_revision,
    )
    if intent.status != "Deleted":
        intent.status = "Deleting"
    await db.commit()
    return await progress(owner, db, intent)


async def progress(owner, db, intent):
    """Report paid progress and original provider UIDs; never free reservations."""
    operation_id, state, provider_uid = None, None, intent.provider_uid
    source_operation_id = None
    cancellation_requested = False
    registrations = (
        await db.scalars(
            select(ControllerDeploymentOperation).where(
                ControllerDeploymentOperation.deployment_id == str(intent.id),
                ControllerDeploymentOperation.org_id == str(intent.org_id),
                ControllerDeploymentOperation.workspace_id == str(intent.workspace_id),
            )
        )
    ).all()
    if registrations:
        selected = next(
            (item for item in registrations if item.action == "teardown"),
            registrations[0],
        )
        source_operation_id = selected.source_operation_id or selected.operation_id
        async with owner.operation_connect() as connection:
            row = await connection.fetchrow(
                "SELECT operation_id,state,cancel_requested_at IS NOT NULL AS cancellation_requested "
                "FROM harness_operations WHERE operation_id=$1 "
                "AND org_id=$2 AND workspace_id=$3 AND plan_digest=$4",
                selected.operation_id,
                selected.org_id,
                selected.workspace_id,
                selected.plan_digest,
            )
            if row is None:
                raise ProvisioningUnavailable(
                    "original deployment progress is unavailable"
                )
            operation_id, state = row["operation_id"], row["state"]
            cancellation_requested = row["cancellation_requested"]
            references = await connection.fetch(
                "SELECT provider_reference FROM harness_allocation_resource WHERE org_id=$1 "
                "AND workspace_id=$2 AND allocation_id=$3 AND provider='aws' AND operation_id=$4",
                selected.org_id,
                selected.workspace_id,
                selected.allocation_id,
                selected.source_operation_id or selected.operation_id,
            )
        kind = "Job" if intent.workload_kind == "batch" else "Deployment"
        prefix = f"kubernetes:{kind}:{intent.namespace}:{intent.name}:"
        uids = {
            row["provider_reference"][len(prefix) :]
            for row in references
            if row["provider_reference"].startswith(prefix)
        }
        if len(uids) == 1 and (provider_uid is None or provider_uid in uids):
            provider_uid = next(iter(uids))
        elif uids:
            provider_uid = None
    status = intent.status
    # Success reports the reviewed workflow outcome, not a fresh live health or
    # a quota release. Only the trusted absence projection writes Deleted.
    if status not in {"Deleted", "Deleting", "CancelledBeforeDispatch"}:
        status = {
            "pending": "Pending",
            "running": "Provisioning",
            "succeeded": "Created",
            "failed": "NeedsRecovery",
            "cancelled": "NeedsRecovery",
            "unknown": "Unknown",
        }.get(state, status)
    return {
        "name": intent.name,
        "namespace": intent.namespace,
        "replicas": intent.desired_replicas,
        "status": status,
        "deployment_id": intent.id,
        "operation_id": operation_id,
        "source_operation_id": source_operation_id,
        "operation_state": state,
        "provider_uid": provider_uid,
        "cancellation_requested": cancellation_requested,
        "cleanup_status": "not-required"
        if intent.status == "CancelledBeforeDispatch"
        else ("confirmed" if intent.status == "Deleted" else "unconfirmed"),
    }
