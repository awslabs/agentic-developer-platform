"""Proxy endpoints — nodes and deployments on child clusters.

Routes:
    GET  /workspaces/{id}/nodes                     # List nodes
    POST /workspaces/{id}/deployments               # Create deployment
    GET  /workspaces/{id}/deployments               # List deployments
    DELETE /workspaces/{id}/deployments/{dep_id}     # Delete deployment
"""

import json
import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.middleware.auth import get_current_org
from app.models.deployment import Deployment
from app.schemas.proxy import (
    CreateDeploymentRequest,
    DeploymentCreateResponse,
    DeploymentDeleteResponse,
    DeploymentInfo,
    DeploymentListResponse,
    NodeInfo,
    NodeListResponse,
)
from app.services.deployment_identity import bind_manifest
from app.services.proxy import (
    ProxyError,
    apply_deployment_via_k8s,
    create_deployment_manifest,
    delete_deployment_via_k8s,
    get_deployment_uid_via_k8s,
    get_k8s_clients,
    get_workspace_cluster,
    list_deployments_via_k8s,
    list_nodes_via_k8s,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/workspaces", tags=["proxy"])


def _handle_proxy_error(exc: ProxyError) -> HTTPException:
    """Convert ProxyError to HTTPException."""
    return HTTPException(status_code=exc.status_code, detail=exc.message)


def _operation_request(body: CreateDeploymentRequest) -> str:
    return json.dumps(
        body.model_dump(mode="json", exclude={"operation_id"}),
        sort_keys=True,
        separators=(",", ":"),
    )


def _deployment_create_response(deployment: Deployment) -> DeploymentCreateResponse:
    request = json.loads(deployment.operation_request_json or "{}")
    return DeploymentCreateResponse(
        name=deployment.name,
        namespace=request.get("namespace", "default"),
        replicas=deployment.desired_replicas,
        status=deployment.status,
        deployment_id=deployment.id,
    )


async def _deployment_for_operation(
    db: AsyncSession,
    org_id: uuid.UUID,
    workspace_id: uuid.UUID,
    body: CreateDeploymentRequest,
    operation_request: str,
    *,
    for_update: bool = False,
) -> Deployment | None:
    query = (
        select(Deployment)
        .where(
            Deployment.org_id == org_id,
            Deployment.operation_id == body.operation_id,
        )
        .execution_options(populate_existing=True)
    )
    if for_update:
        query = query.with_for_update()
    deployment = await db.scalar(query)
    if deployment is None:
        return None
    if (
        deployment.workspace_id != workspace_id
        or deployment.operation_request_json != operation_request
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Operation ID was already used for a different deployment request",
        )
    return deployment


def _deployment_target(workspace, cluster, namespace: str) -> str:
    """Pin the server-resolved destination before creating anything remotely."""
    return json.dumps(
        {
            "cluster_id": str(cluster.id),
            "cluster_arn": cluster.eks_cluster_arn,
            "endpoint": cluster.endpoint,
            "namespace": namespace,
            "workspace_namespace": workspace.namespace_name,
            "workspace_name": workspace.name,
            "cloud_account_id": str(workspace.aws_account_id)
            if workspace.aws_account_id
            else None,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _require_deployment_target(deployment, workspace, cluster, namespace):
    recorded = json.loads(deployment.operation_target_json or "{}")
    manifest = recorded.pop("manifest", None)
    if (
        deployment.cluster_id != cluster.id
        or not isinstance(manifest, dict)
        or recorded != json.loads(_deployment_target(workspace, cluster, namespace))
    ):
        raise HTTPException(
            status_code=409,
            detail="Deployment destination is missing or changed; refusing to replay against another target",
        )


def _terminal_create_response(deployment):
    if deployment.status == "Failed":
        raise HTTPException(
            status_code=409,
            detail={
                "error": "create_operation_failed",
                "message": "Deployment operation failed; inspect the deployment and use a new operation ID for a new request",
            },
        )
    if deployment.status not in {"Pending", "Unknown"}:
        return _deployment_create_response(deployment)
    return None


def _create_manifest(body):
    return create_deployment_manifest(
        name=body.name,
        model_name=body.model_name,
        precision=body.precision,
        serving_framework=body.serving_framework,
        replicas=body.replicas,
        gpu_per_replica=body.gpu_per_replica,
        tensor_parallel_size=body.tensor_parallel_size,
        max_model_len=body.max_model_len,
        namespace=body.namespace,
    )


def _bound_deployment_manifest(deployment):
    # A retry after an API release must use the original image/defaults, not
    # regenerate a different request under the original idempotency key.
    return bind_manifest(
        json.loads(deployment.operation_target_json)["manifest"],
        org_id=deployment.org_id,
        workspace_id=deployment.workspace_id,
        operation_id=deployment.operation_id,
        deployment_id=deployment.id,
    )


# --- Node endpoints ---


@router.get("/{workspace_id}/nodes", response_model=NodeListResponse)
async def list_workspace_nodes(
    workspace_id: uuid.UUID,
    org_id: uuid.UUID = Depends(get_current_org),
    db: AsyncSession = Depends(get_session),
) -> NodeListResponse:
    """List nodes from the workspace's child cluster via K8s API proxy.

    Assumes cross-account IAM role, creates K8s client, and forwards the request.
    """
    try:
        core_api, _, workspace, cluster = await get_k8s_clients(
            workspace_id, org_id, db
        )
        nodes = list_nodes_via_k8s(core_api)

        return NodeListResponse(
            workspace_id=workspace_id,
            nodes=[NodeInfo(**n) for n in nodes],
            total=len(nodes),
        )
    except ProxyError as exc:
        raise _handle_proxy_error(exc)


# --- Deployment endpoints ---


@router.post(
    "/{workspace_id}/deployments",
    response_model=DeploymentCreateResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_deployment(
    workspace_id: uuid.UUID,
    body: CreateDeploymentRequest,
    org_id: uuid.UUID = Depends(get_current_org),
    db: AsyncSession = Depends(get_session),
) -> DeploymentCreateResponse:
    """Create a model deployment on the workspace's child cluster.

    Generates a vLLM/SGLang K8s Deployment manifest and applies it to the child cluster.
    Also records the deployment in the control plane database.
    """
    operation_request = _operation_request(body)
    existing = await _deployment_for_operation(
        db, org_id, workspace_id, body, operation_request
    )
    if existing is not None:
        response = _terminal_create_response(existing)
        if response is not None:
            return response

    try:
        workspace, cluster = await get_workspace_cluster(
            workspace_id, org_id, db, for_update=True
        )
    except ProxyError as exc:
        raise _handle_proxy_error(exc)

    # The workspace lock also excludes deletion and registration replacement.
    # Another request may have completed while this one waited for that lock.
    existing = await _deployment_for_operation(
        db, org_id, workspace_id, body, operation_request, for_update=True
    )
    if existing is not None:
        response = _terminal_create_response(existing)
        if response is not None:
            return response
    if existing is None:
        target = json.loads(_deployment_target(workspace, cluster, body.namespace))
        target["manifest"] = _create_manifest(body)
        deployment = Deployment(
            cluster_id=cluster.id,
            org_id=org_id,
            workspace_id=workspace_id,
            name=body.name,
            operation_id=body.operation_id,
            operation_request_json=operation_request,
            operation_target_json=json.dumps(
                target, sort_keys=True, separators=(",", ":")
            ),
            model_name=body.model_name,
            precision=body.precision,
            serving_framework=body.serving_framework,
            desired_replicas=body.replicas,
            gpu_per_replica=body.gpu_per_replica,
            tensor_parallel_size=body.tensor_parallel_size,
            max_model_len=body.max_model_len,
            status="Pending",
        )
        db.add(deployment)
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
            existing = await _deployment_for_operation(
                db, org_id, workspace_id, body, operation_request
            )
            if existing is None:
                raise
            response = _terminal_create_response(existing)
            if response is not None:
                return response
            deployment = existing
        else:
            await db.refresh(deployment)
    else:
        deployment = existing

    # The intent is committed before credentials/provider I/O. Reacquire the
    # canonical rows after that commit and never adopt a changed destination.
    try:
        workspace, cluster = await get_workspace_cluster(
            workspace_id, org_id, db, for_update=True
        )
    except ProxyError as exc:
        raise _handle_proxy_error(exc)
    deployment = await _deployment_for_operation(
        db, org_id, workspace_id, body, operation_request, for_update=True
    )
    if deployment is None:
        raise HTTPException(409, "Deployment intent disappeared before execution")
    response = _terminal_create_response(deployment)
    if response is not None:
        return response
    _require_deployment_target(deployment, workspace, cluster, body.namespace)
    try:
        _, apps_api, _, _ = await get_k8s_clients(workspace_id, org_id, db)
    except ProxyError as exc:
        raise _handle_proxy_error(exc)

    manifest = _bound_deployment_manifest(deployment)
    try:
        result = apply_deployment_via_k8s(
            apps_api, manifest, expected_uid=deployment.provider_uid
        )
    except ProxyError as exc:
        deployment.status = "Unknown"
        await db.commit()
        raise _handle_proxy_error(exc)

    deployment.status = result["status"]
    deployment.provider_uid = result["provider_uid"]
    await db.commit()
    return DeploymentCreateResponse(
        name=result["name"],
        namespace=result["namespace"],
        replicas=result["replicas"],
        status=result["status"],
        deployment_id=deployment.id,
    )


@router.get("/{workspace_id}/deployments", response_model=DeploymentListResponse)
async def list_deployments(
    workspace_id: uuid.UUID,
    namespace: str = "default",
    org_id: uuid.UUID = Depends(get_current_org),
    db: AsyncSession = Depends(get_session),
) -> DeploymentListResponse:
    """List model deployments on the workspace's child cluster."""
    try:
        _, apps_api, workspace, cluster = await get_k8s_clients(
            workspace_id, org_id, db
        )
        deployments = list_deployments_via_k8s(apps_api, namespace=namespace)

        return DeploymentListResponse(
            workspace_id=workspace_id,
            deployments=[DeploymentInfo(**d) for d in deployments],
            total=len(deployments),
        )
    except ProxyError as exc:
        raise _handle_proxy_error(exc)


@router.delete(
    "/{workspace_id}/deployments/{dep_id}",
    response_model=DeploymentDeleteResponse,
)
async def delete_deployment(
    workspace_id: uuid.UUID,
    dep_id: str,
    namespace: str = "default",
    org_id: uuid.UUID = Depends(get_current_org),
    db: AsyncSession = Depends(get_session),
) -> DeploymentDeleteResponse:
    """Delete a deployment from the workspace's child cluster.

    dep_id can be either the deployment name (string) or a UUID from the DB.
    """
    try:
        workspace, cluster = await get_workspace_cluster(
            workspace_id, org_id, db, for_update=True
        )

        # Resolve deployment name: could be a UUID (DB record) or a K8s name
        deployment_name = dep_id
        dep_record = None
        try:
            dep_uuid = uuid.UUID(dep_id)
            # Look up in DB to get the K8s deployment name
            dep_result = await db.execute(
                select(Deployment)
                .where(
                    Deployment.id == dep_uuid,
                    Deployment.workspace_id == workspace_id,
                    Deployment.org_id == org_id,
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            dep_record = dep_result.scalar_one_or_none()
            if dep_record:
                deployment_name = dep_record.name
        except ValueError:
            # Not a UUID — treat as K8s deployment name directly
            pass

        if dep_record is None:
            candidates = (
                await db.scalars(
                    select(Deployment)
                    .where(
                        Deployment.name == deployment_name,
                        Deployment.workspace_id == workspace_id,
                        Deployment.org_id == org_id,
                    )
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            ).all()
            candidates = [
                candidate
                for candidate in candidates
                if json.loads(candidate.operation_request_json or "{}").get(
                    "namespace", "default"
                )
                == namespace
            ]
            live = [
                candidate for candidate in candidates if candidate.status != "Deleted"
            ]
            if len(live) > 1:
                raise HTTPException(
                    409,
                    "Deployment name has multiple operation records; use its deployment ID",
                )
            dep_record = live[0] if live else (candidates[0] if candidates else None)

        expected_manifest = None
        if dep_record is not None and dep_record.operation_id is not None:
            _require_deployment_target(dep_record, workspace, cluster, namespace)
            expected_manifest = _bound_deployment_manifest(dep_record)
            if dep_record.status == "Deleted":
                return DeploymentDeleteResponse(
                    name=deployment_name, namespace=namespace, status="Deleted"
                )
            # Commit the tombstone intent before deletion. A lost reply cannot
            # let a create retry resurrect the workload on a replacement worker.
            dep_record.status = "Deleting"
            await db.commit()
            workspace, cluster = await get_workspace_cluster(
                workspace_id, org_id, db, for_update=True
            )
            await db.refresh(dep_record, with_for_update=True)
            _require_deployment_target(dep_record, workspace, cluster, namespace)

        _, apps_api, _, _ = await get_k8s_clients(workspace_id, org_id, db)
        if expected_manifest is not None and dep_record.provider_uid is None:
            observed_uid = get_deployment_uid_via_k8s(apps_api, expected_manifest)
            if observed_uid is None:
                dep_record.status = "Deleted"
                await db.commit()
                return DeploymentDeleteResponse(
                    name=deployment_name, namespace=namespace, status="Deleted"
                )
            # A create reply may have been lost before its UID was recorded.
            # Persist the UID before deletion so a lost delete reply cannot make
            # the next worker adopt a replacement with copied annotations.
            dep_record.provider_uid = observed_uid
            await db.commit()
            workspace, cluster = await get_workspace_cluster(
                workspace_id, org_id, db, for_update=True
            )
            await db.refresh(dep_record, with_for_update=True)
            _require_deployment_target(dep_record, workspace, cluster, namespace)
            if dep_record.status == "Deleted":
                return DeploymentDeleteResponse(
                    name=deployment_name, namespace=namespace, status="Deleted"
                )
        # Delete from child cluster
        result = delete_deployment_via_k8s(
            apps_api,
            deployment_name,
            namespace=namespace,
            expected_uid=dep_record.provider_uid if dep_record is not None else None,
            expected_manifest=expected_manifest,
            absent_ok=dep_record is not None and dep_record.operation_id is not None,
        )

        # Update DB record if exists
        if dep_record:
            dep_record.status = "Deleted"
            await db.commit()

        return DeploymentDeleteResponse(**result)
    except ProxyError as exc:
        raise _handle_proxy_error(exc)
