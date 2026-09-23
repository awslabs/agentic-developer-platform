"""Proxy endpoints — nodes and deployments on child clusters.

Routes:
    GET  /workspaces/{id}/nodes                     # List nodes
    POST /workspaces/{id}/deployments               # Create deployment
    GET  /workspaces/{id}/deployments               # List deployments
    DELETE /workspaces/{id}/deployments/{dep_id}     # Delete deployment
"""

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.middleware.auth import get_current_org
from app.models.deployment import Deployment
from app.models.workspace import Workspace
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
from app.services.quota import (
    release_deployment_reservation,
    reserve_deployment_gpus,
)
from app.services.workspace_namespace import (
    NamespaceResolutionError,
    resolve_workspace_namespace,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/workspaces", tags=["proxy"])


def _handle_proxy_error(exc: ProxyError) -> HTTPException:
    """Convert ProxyError to HTTPException."""
    return HTTPException(status_code=exc.status_code, detail=exc.message)


def _resolve_namespace_or_500(workspace: Workspace, workspace_id: uuid.UUID) -> str:
    """Resolve the workspace's namespace, or fail closed with an operator-facing 500.

    A 500 rather than a 4xx because an unresolvable namespace is a provisioning fault:
    nothing the requester could change would fix it. Failing closed is the point — the
    alternative, defaulting to the cluster's shared `default` namespace, is the
    cross-tenant exposure this change exists to remove (issue #5671, A15).
    """
    try:
        return resolve_workspace_namespace(workspace)
    except NamespaceResolutionError as exc:
        logger.error(
            "Namespace resolution failed for workspace %s: %s", workspace_id, exc
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)
        ) from exc


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
    request: Request,
    org_id: uuid.UUID = Depends(get_current_org),
    db: AsyncSession = Depends(get_session),
) -> DeploymentCreateResponse:
    """Create a model deployment on the workspace's child cluster.

    Generates a vLLM/SGLang K8s Deployment manifest and applies it to the child cluster.
    Also records the deployment in the control plane database.

    Quota is reserved BEFORE anything is provisioned, and the target namespace comes
    from the workspace record rather than the request (issue #5671, A15).
    """
    try:
        _, apps_api, workspace, cluster = await get_k8s_clients(
            workspace_id, org_id, db
        )

        # The namespace is the platform's decision, not the caller's.
        namespace = _resolve_namespace_or_500(workspace, workspace_id)

        # Reserve the capacity before provisioning. `reserve_deployment_gpus` both
        # refuses an over-budget request and writes the deployment row that holds the
        # reservation, under a per-workspace lock — so two concurrent requests cannot
        # both be approved against the same headroom. A refusal raises here, before any
        # manifest is built and before the cluster is contacted.
        deployment = await reserve_deployment_gpus(
            workspace_id,
            org_id,
            body.replicas * body.gpu_per_replica,
            db,
            request=request,
            deployment_kwargs={
                "cluster_id": cluster.id,
                "name": body.name,
                "namespace": namespace,
                "model_name": body.model_name,
                "precision": body.precision,
                "serving_framework": body.serving_framework,
                "desired_replicas": body.replicas,
                "gpu_per_replica": body.gpu_per_replica,
                "tensor_parallel_size": body.tensor_parallel_size,
                "max_model_len": body.max_model_len,
            },
        )

        try:
            manifest = create_deployment_manifest(
                name=body.name,
                model_name=body.model_name,
                precision=body.precision,
                serving_framework=body.serving_framework,
                replicas=body.replicas,
                gpu_per_replica=body.gpu_per_replica,
                tensor_parallel_size=body.tensor_parallel_size,
                max_model_len=body.max_model_len,
                namespace=namespace,
                workspace_id=workspace_id,
            )
            result = apply_deployment_via_k8s(
                apps_api, manifest, workspace_id=workspace_id
            )
        except Exception:
            # The reservation must not outlive a failed provisioning attempt, or the
            # workspace stays charged for capacity it never got.
            await release_deployment_reservation(deployment, db)
            raise

        deployment.status = result["status"]
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
    org_id: uuid.UUID = Depends(get_current_org),
    db: AsyncSession = Depends(get_session),
) -> DeploymentListResponse:
    """List model deployments on the workspace's child cluster.

    The `namespace` query parameter was removed (issue #5671, A15): it let a caller
    enumerate a neighbour's workloads on a shared cluster. Listing is scoped to the
    workspace's own namespace and its own ownership label.
    """
    try:
        _, apps_api, workspace, cluster = await get_k8s_clients(
            workspace_id, org_id, db
        )
        namespace = _resolve_namespace_or_500(workspace, workspace_id)
        deployments = list_deployments_via_k8s(
            apps_api, namespace=namespace, workspace_id=workspace_id
        )

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
    dep_id: uuid.UUID,
    org_id: uuid.UUID = Depends(get_current_org),
    db: AsyncSession = Depends(get_session),
) -> DeploymentDeleteResponse:
    """Delete a deployment from the workspace's child cluster.

    `dep_id` is the control plane's deployment id — a UUID, enforced by the path type
    (issue #5671, A15). It used to accept a raw Kubernetes object name too, which meant
    a caller could name any object on the cluster and, combined with a caller-supplied
    namespace, delete a workload belonging to another workspace. The target is now
    resolved from this workspace's OWN deployment record, and the ownership label on
    the cluster object is verified before it is removed.

    The `namespace` query parameter is gone for the same reason: the namespace comes
    from the record, so a delete cannot be pointed at another namespace.
    """
    try:
        _, apps_api, workspace, cluster = await get_k8s_clients(
            workspace_id, org_id, db
        )

        # Resolve the target through this workspace's own record. Scoping the lookup to
        # `workspace_id` is what makes another workspace's deployment id a 404 rather
        # than a successful deletion.
        dep_result = await db.execute(
            select(Deployment).where(
                Deployment.id == dep_id,
                Deployment.workspace_id == workspace_id,
            )
        )
        dep_record = dep_result.scalar_one_or_none()
        if dep_record is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Deployment not found for this workspace",
            )

        # Prefer the namespace recorded at creation — that is where the object actually
        # is. Fall back to resolving from the workspace for rows written before the
        # column existed.
        namespace = dep_record.namespace or _resolve_namespace_or_500(
            workspace, workspace_id
        )

        result = delete_deployment_via_k8s(
            apps_api,
            dep_record.name,
            namespace=namespace,
            workspace_id=workspace_id,
        )

        dep_record.status = "Deleted"
        await db.commit()

        return DeploymentDeleteResponse(**result)
    except ProxyError as exc:
        raise _handle_proxy_error(exc)
