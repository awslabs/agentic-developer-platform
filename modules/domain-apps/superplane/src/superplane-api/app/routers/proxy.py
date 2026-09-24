"""Workspace observations and governed controller deployment operations."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.routing import APIRoute
from harness_jobs.identity import OperationRefused
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.middleware.auth import get_current_org
from app.models.deployment import Deployment
from app.schemas.proxy import (
    CancelWorkloadRequest,
    CreateBatchRequest,
    CreateDeploymentRequest,
    DeleteDeploymentRequest,
    DeploymentCreateResponse,
    DeploymentDeleteResponse,
    DeploymentInfo,
    DeploymentListResponse,
    NodeInfo,
    NodeListResponse,
)
from app.services import deployment_operations
from app.services.provisioning import ProvisioningRefused, ProvisioningUnavailable
from app.services.workspace_namespace import (
    NamespaceResolutionError,
    resolve_workspace_namespace,
)
from app.services.proxy import (
    ProxyError,
    get_k8s_clients,
    get_workspace_cluster,
    list_nodes_via_k8s,
)


class DeploymentRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def governed(request):
            try:
                return await handler(request)
            except (
                ProvisioningRefused,
                OperationRefused,
                NamespaceResolutionError,
            ) as error:
                raise HTTPException(409, str(error)) from None
            except ProvisioningUnavailable as error:
                raise HTTPException(503, str(error)) from None
            except ProxyError as error:
                raise _handle_proxy_error(error) from None

        return governed


router = APIRouter(prefix="/workspaces", tags=["proxy"], route_class=DeploymentRoute)


def _handle_proxy_error(exc):
    return HTTPException(status_code=exc.status_code, detail=exc.message)


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


@router.post("/{workspace_id}/deployments/preview")
async def preview_deployment(
    workspace_id: uuid.UUID,
    body: CreateDeploymentRequest,
    org_id: uuid.UUID = Depends(get_current_org),
    db: AsyncSession = Depends(get_session),
):
    review = await deployment_operations.preview_create(db, org_id, workspace_id, body)
    return review.public(str(workspace_id))


@router.get("/{workspace_id}/deployment-profiles")
async def deployment_profiles(
    workspace_id: uuid.UUID,
    request: Request,
    org_id: uuid.UUID = Depends(get_current_org),
    db: AsyncSession = Depends(get_session),
):
    from app.services.controller_deployments import serving_catalog

    return await serving_catalog(request, db, org_id, workspace_id)


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
    request: Request = None,
):
    result = await deployment_operations.create(request, db, org_id, workspace_id, body)
    return DeploymentCreateResponse(**result)


@router.get("/{workspace_id}/deployments", response_model=DeploymentListResponse)
async def list_deployments(
    workspace_id: uuid.UUID,
    org_id: uuid.UUID = Depends(get_current_org),
    db: AsyncSession = Depends(get_session),
    request: Request = None,
):
    workspace, _ = await get_workspace_cluster(workspace_id, org_id, db)
    namespace = resolve_workspace_namespace(workspace)
    records = (
        await db.scalars(
            select(Deployment).where(
                Deployment.workspace_id == workspace_id,
                Deployment.org_id == org_id,
                Deployment.namespace == namespace,
                Deployment.status != "Deleted",
                Deployment.workload_kind == "serving",
            )
        )
    ).all()
    owner = (
        deployment_operations.composition(request)
        if any(row.controller_request_payload for row in records)
        else None
    )
    items = []
    for row in records:
        if row.controller_request_payload:
            value = await deployment_operations.progress(owner, db, row)
        else:
            value = {
                "name": row.name,
                "namespace": namespace,
                "deployment_id": row.id,
                "status": row.status,
                "replicas": row.desired_replicas,
                "ready_replicas": row.actual_replicas,
                "provider_uid": row.provider_uid,
            }
        items.append(DeploymentInfo(**value))
    return DeploymentListResponse(
        workspace_id=workspace_id, deployments=items, total=len(items)
    )


@router.post("/{workspace_id}/deployments/{dep_id}/teardown-preview")
async def preview_deployment_teardown(
    workspace_id: uuid.UUID,
    dep_id: uuid.UUID,
    body: DeleteDeploymentRequest,
    request: Request,
    org_id: uuid.UUID = Depends(get_current_org),
    db: AsyncSession = Depends(get_session),
):
    review = await deployment_operations.preview_delete(
        request, db, org_id, workspace_id, dep_id, body
    )
    return review.public(str(workspace_id))


@router.delete(
    "/{workspace_id}/deployments/{dep_id}", response_model=DeploymentDeleteResponse
)
async def delete_deployment(
    workspace_id: uuid.UUID,
    dep_id: uuid.UUID,
    body: DeleteDeploymentRequest,
    request: Request,
    org_id: uuid.UUID = Depends(get_current_org),
    db: AsyncSession = Depends(get_session),
):
    result = await deployment_operations.delete(
        request, db, org_id, workspace_id, dep_id, body
    )
    return DeploymentDeleteResponse(**result)


def batch_result(value):
    """Admission progress is separate from Job completion and provider absence."""
    return {
        "job_id": value["deployment_id"],
        "name": value["name"],
        "namespace": value["namespace"],
        "status": value["status"],
        "operation_id": value["operation_id"],
        "operation_state": value["operation_state"],
        "provider_uid": value["provider_uid"],
        "execution_outcome": "unknown",
        "cleanup_status": value["cleanup_status"],
        "cancellation_requested": value["cancellation_requested"],
        "observed_cost_micros": None,
    }


@router.get("/{workspace_id}/batch-profiles")
async def batch_profiles(
    workspace_id: uuid.UUID,
    request: Request,
    org_id: uuid.UUID = Depends(get_current_org),
    db: AsyncSession = Depends(get_session),
):
    from app.services.controller_deployments import serving_catalog

    return await serving_catalog(
        request, db, org_id, workspace_id, workload_kind="batch"
    )


@router.post("/{workspace_id}/batch-jobs/preview")
async def preview_batch(
    workspace_id: uuid.UUID,
    body: CreateBatchRequest,
    org_id: uuid.UUID = Depends(get_current_org),
    db: AsyncSession = Depends(get_session),
):
    review = await deployment_operations.preview_create(db, org_id, workspace_id, body)
    return {**review.public(str(workspace_id)), "job_id": review.deployment_id}


@router.post("/{workspace_id}/batch-jobs", status_code=status.HTTP_201_CREATED)
async def create_batch(
    workspace_id: uuid.UUID,
    body: CreateBatchRequest,
    request: Request,
    org_id: uuid.UUID = Depends(get_current_org),
    db: AsyncSession = Depends(get_session),
):
    return batch_result(
        await deployment_operations.create(request, db, org_id, workspace_id, body)
    )


@router.get("/{workspace_id}/batch-jobs")
async def list_batch(
    workspace_id: uuid.UUID,
    request: Request,
    org_id: uuid.UUID = Depends(get_current_org),
    db: AsyncSession = Depends(get_session),
):
    workspace, _ = await get_workspace_cluster(workspace_id, org_id, db)
    namespace = resolve_workspace_namespace(workspace)
    rows = (
        await db.scalars(
            select(Deployment)
            .where(
                Deployment.workspace_id == workspace_id,
                Deployment.org_id == org_id,
                Deployment.namespace == namespace,
                Deployment.workload_kind == "batch",
            )
            .order_by(Deployment.created_at.desc(), Deployment.id)
            .limit(101)
        )
    ).all()
    owner = deployment_operations.composition(request) if rows else None
    jobs = [
        batch_result(await deployment_operations.progress(owner, db, row))
        for row in rows[:100]
    ]
    return {
        "workspace_id": str(workspace_id),
        "jobs": jobs,
        "truncated": len(rows) > 100,
    }


@router.get("/{workspace_id}/batch-jobs/{job_id}")
async def get_batch(
    workspace_id: uuid.UUID,
    job_id: uuid.UUID,
    request: Request,
    org_id: uuid.UUID = Depends(get_current_org),
    db: AsyncSession = Depends(get_session),
):
    workspace, cluster = await get_workspace_cluster(workspace_id, org_id, db)
    intent = await deployment_operations.intent_for(
        db, org_id, workspace_id, job_id, workload_kind="batch"
    )
    deployment_operations.require_target(intent, workspace, cluster)
    return batch_result(
        await deployment_operations.progress(
            deployment_operations.composition(request), db, intent
        )
    )


@router.post("/{workspace_id}/batch-jobs/{job_id}/teardown-preview")
async def preview_batch_teardown(
    workspace_id: uuid.UUID,
    job_id: uuid.UUID,
    body: DeleteDeploymentRequest,
    request: Request,
    org_id: uuid.UUID = Depends(get_current_org),
    db: AsyncSession = Depends(get_session),
):
    review = await deployment_operations.preview_delete(
        request, db, org_id, workspace_id, job_id, body, workload_kind="batch"
    )
    return {**review.public(str(workspace_id)), "job_id": review.deployment_id}


@router.delete("/{workspace_id}/batch-jobs/{job_id}")
async def delete_batch(
    workspace_id: uuid.UUID,
    job_id: uuid.UUID,
    body: DeleteDeploymentRequest,
    request: Request,
    org_id: uuid.UUID = Depends(get_current_org),
    db: AsyncSession = Depends(get_session),
):
    return batch_result(
        await deployment_operations.delete(
            request, db, org_id, workspace_id, job_id, body, workload_kind="batch"
        )
    )


@router.post("/{workspace_id}/batch-jobs/{job_id}/cancellation")
async def cancel_batch(
    workspace_id: uuid.UUID,
    job_id: uuid.UUID,
    body: CancelWorkloadRequest,
    request: Request,
    org_id: uuid.UUID = Depends(get_current_org),
    db: AsyncSession = Depends(get_session),
):
    from app.services.workload_cancellation import cancel

    result = await cancel(
        request, db, org_id, workspace_id, job_id, body.operation_id, kind="batch"
    )
    return {**result, "job_id": result["deployment_id"]}


@router.post("/{workspace_id}/deployments/{dep_id}/cancellation")
async def cancel_deployment(
    workspace_id: uuid.UUID,
    dep_id: uuid.UUID,
    body: CancelWorkloadRequest,
    request: Request,
    org_id: uuid.UUID = Depends(get_current_org),
    db: AsyncSession = Depends(get_session),
):
    from app.services.workload_cancellation import cancel

    return await cancel(
        request, db, org_id, workspace_id, dep_id, body.operation_id, kind="serving"
    )


@router.get("/{workspace_id}/batch-jobs/{job_id}/observation")
async def observe_batch(
    workspace_id: uuid.UUID,
    job_id: uuid.UUID,
    request: Request,
    logs: bool = False,
    pod_uid: str | None = Query(
        default=None, min_length=1, max_length=255, pattern="^[a-zA-Z0-9-]+$"
    ),
    org_id: uuid.UUID = Depends(get_current_org),
    db: AsyncSession = Depends(get_session),
):
    from app.services.workload_observations import observe

    return await observe(
        request,
        db,
        org_id,
        workspace_id,
        job_id,
        kind="batch",
        logs=logs,
        pod_uid=pod_uid,
    )


@router.get("/{workspace_id}/deployments/{dep_id}/observation")
async def observe_serving(
    workspace_id: uuid.UUID,
    dep_id: uuid.UUID,
    request: Request,
    logs: bool = False,
    pod_uid: str | None = Query(
        default=None, min_length=1, max_length=255, pattern="^[a-zA-Z0-9-]+$"
    ),
    org_id: uuid.UUID = Depends(get_current_org),
    db: AsyncSession = Depends(get_session),
):
    from app.services.workload_observations import observe

    return await observe(
        request,
        db,
        org_id,
        workspace_id,
        dep_id,
        kind="serving",
        logs=logs,
        pod_uid=pod_uid,
    )
