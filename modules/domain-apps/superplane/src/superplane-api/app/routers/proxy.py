"""Proxy endpoints — nodes and deployments on child clusters.

Routes:
    GET  /workspaces/{id}/nodes                     # List nodes
    POST /workspaces/{id}/deployments               # Create deployment
    GET  /workspaces/{id}/deployments               # List deployments
    DELETE /workspaces/{id}/deployments/{dep_id}     # Delete deployment
"""

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
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
from app.services.proxy import (
    ProxyError,
    apply_deployment_via_k8s,
    create_deployment_manifest,
    delete_deployment_via_k8s,
    get_k8s_clients,
    list_deployments_via_k8s,
    list_nodes_via_k8s,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/workspaces", tags=["proxy"])


def _handle_proxy_error(exc: ProxyError) -> HTTPException:
    """Convert ProxyError to HTTPException."""
    return HTTPException(status_code=exc.status_code, detail=exc.message)


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
    try:
        _, apps_api, workspace, cluster = await get_k8s_clients(
            workspace_id, org_id, db
        )

        # Generate the K8s manifest
        manifest = create_deployment_manifest(
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

        # Apply to child cluster
        result = apply_deployment_via_k8s(apps_api, manifest)

        # Record in DB
        deployment = Deployment(
            cluster_id=cluster.id,
            org_id=org_id,
            workspace_id=workspace_id,
            name=body.name,
            model_name=body.model_name,
            precision=body.precision,
            serving_framework=body.serving_framework,
            desired_replicas=body.replicas,
            gpu_per_replica=body.gpu_per_replica,
            tensor_parallel_size=body.tensor_parallel_size,
            max_model_len=body.max_model_len,
            status=result["status"],
        )
        db.add(deployment)
        await db.commit()
        await db.refresh(deployment)

        return DeploymentCreateResponse(
            name=result["name"],
            namespace=result["namespace"],
            replicas=result["replicas"],
            status=result["status"],
            deployment_id=deployment.id,
        )
    except ProxyError as exc:
        raise _handle_proxy_error(exc)


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
        _, apps_api, workspace, cluster = await get_k8s_clients(
            workspace_id, org_id, db
        )

        # Resolve deployment name: could be a UUID (DB record) or a K8s name
        deployment_name = dep_id
        try:
            dep_uuid = uuid.UUID(dep_id)
            # Look up in DB to get the K8s deployment name
            dep_result = await db.execute(
                select(Deployment).where(
                    Deployment.id == dep_uuid,
                    Deployment.workspace_id == workspace_id,
                )
            )
            dep_record = dep_result.scalar_one_or_none()
            if dep_record:
                deployment_name = dep_record.name
        except ValueError:
            # Not a UUID — treat as K8s deployment name directly
            pass

        # Delete from child cluster
        result = delete_deployment_via_k8s(
            apps_api, deployment_name, namespace=namespace
        )

        # Update DB record if exists
        dep_result = await db.execute(
            select(Deployment).where(
                Deployment.name == deployment_name,
                Deployment.workspace_id == workspace_id,
            )
        )
        dep_record = dep_result.scalar_one_or_none()
        if dep_record:
            dep_record.status = "Deleted"
            await db.commit()

        return DeploymentDeleteResponse(**result)
    except ProxyError as exc:
        raise _handle_proxy_error(exc)
