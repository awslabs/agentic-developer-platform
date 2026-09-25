"""Workspace CRUD endpoints — scoped to org_id from JWT."""

import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.middleware.auth import get_current_org
from app.models.cluster import Cluster
from app.models.workspace import STATUS_ACTIVE, Workspace
from app.schemas.workspace import (
    CreateWorkspaceRequest,
    EligibleClusterListResponse,
    EligibleClusterResponse,
    KubeconfigResponse,
    WorkspaceDeleteResponse,
    WorkspaceListResponse,
    WorkspaceResponse,
)
from app.services.provisioning import (
    ProvisioningError,
    ProvisioningRefused,
    get_operation_facade,
    observe,
    start_teardown,
    summarize,
)
from app.services.quota import enforce_workspace_creation_quota

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/workspaces", tags=["workspaces"])

# Heartbeat staleness threshold — if no heartbeat for this long, mark as Degraded
HEARTBEAT_STALE_THRESHOLD = timedelta(minutes=5)

# Default budget guardrails for research workspaces (when not explicitly set)
RESEARCH_DEFAULT_BUDGET: dict[str, Decimal | int] = {
    "max_daily_usd": Decimal("100.00"),
    "max_gpus": 8,
}


def _make_display_name(name: str, isolation_mode: str) -> str:
    """Generate display name with isolation mode tag (e.g. 'ml-research [research]')."""
    if isolation_mode == "research":
        return f"{name} [research]"
    return name


def _effective_health_status(cluster: Cluster) -> str:
    """Compute effective health status, degrading if heartbeat is stale.

    If the last heartbeat is older than HEARTBEAT_STALE_THRESHOLD, the cluster
    is considered Degraded regardless of the last reported health status.
    """
    if cluster.last_heartbeat is None:
        return cluster.health_status or "Unknown"

    now = datetime.now(timezone.utc)
    heartbeat_age = now - cluster.last_heartbeat
    if heartbeat_age > HEARTBEAT_STALE_THRESHOLD:
        logger.info(
            "Cluster %s heartbeat stale (age=%s). Marking as Degraded (was %s).",
            cluster.id,
            heartbeat_age,
            cluster.health_status,
        )
        return "Degraded"

    return cluster.health_status or "Unknown"


def _workspace_to_response(
    ws: Workspace, cluster: Cluster | None = None
) -> WorkspaceResponse:
    """Convert a Workspace model + optional Cluster to a response schema."""
    cluster_health = None
    if cluster:
        cluster_health = _effective_health_status(cluster)

    return WorkspaceResponse(
        id=ws.id,
        org_id=ws.org_id,
        name=ws.name,
        isolation_mode=ws.isolation_mode,
        display_name=_make_display_name(ws.name, ws.isolation_mode),
        status=ws.status,
        provisioning_operation_id=ws.provisioning_operation_id,
        is_default=ws.is_default,
        budget_max_daily_usd=ws.budget_max_daily_usd,
        budget_max_hourly_usd=ws.budget_max_hourly_usd,
        budget_max_gpus=ws.budget_max_gpus,
        agent_iam_role_arn=ws.agent_iam_role_arn,
        cluster_health=cluster_health,
        last_heartbeat=cluster.last_heartbeat if cluster else None,
        created_at=ws.created_at,
        updated_at=ws.updated_at,
    )


def _operation_request(body: CreateWorkspaceRequest) -> str:
    from app.services.onboarding import placement_document

    return json.dumps(
        placement_document(body, exclude={"operation_id", "approval_id"}),
        sort_keys=True,
        separators=(",", ":"),
    )


async def _workspace_for_operation(
    db: AsyncSession,
    org_id: uuid.UUID,
    body: CreateWorkspaceRequest,
    operation_request: str,
    *,
    for_update: bool = False,
) -> Workspace | None:
    query = (
        select(Workspace)
        .where(
            Workspace.org_id == org_id,
            Workspace.operation_id == body.operation_id,
        )
        .execution_options(populate_existing=True)
    )
    if for_update:
        query = query.with_for_update()
    workspace = await db.scalar(query)
    if workspace is None:
        return None
    if workspace.operation_request_json != operation_request:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Operation ID was already used for a different workspace request",
        )
    return workspace


async def _reconcile_workspace_provisioning(
    db: AsyncSession,
    org_id: uuid.UUID,
    body: CreateWorkspaceRequest,
    operation_request: str,
) -> WorkspaceResponse:
    workspace = await _workspace_for_operation(
        db, org_id, body, operation_request, for_update=True
    )
    if workspace is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Workspace operation disappeared before it could be reconciled",
        )
    if workspace.status == "Failed":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "create_operation_failed",
                "message": "Workspace operation failed; inspect the workspace and use a new operation ID for a new request",
            },
        )
    if workspace.status != "Provisioning":
        return _workspace_to_response(workspace)

    opening = workspace.provisioning_operation_id is None
    try:
        if opening:
            raise ProvisioningError(
                "Legacy workspace has no admitted operation; reconcile its original request before retrying"
            )
        else:
            progress = await _observe_workspace(
                workspace, workspace.provisioning_operation_id
            )
    except ProvisioningRefused as exc:
        if opening:
            workspace.status = "Failed"
            await db.commit()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    except ProvisioningError as exc:
        logger.error("Provisioning unavailable for workspace %s: %s", workspace.id, exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc

    if progress.is_conclusive_failure:
        workspace.status = "Failed"
    # A completed preparation/apply phase is not workspace readiness. Bootstrap
    # registration owns Active after scoped credentials and observations verify it.
    await db.commit()
    await db.refresh(workspace)
    logger.info("Workspace %s provisioning: %s", workspace.id, summarize(progress))
    if workspace.status == "Failed":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "create_operation_failed",
                "message": "Workspace provisioning operation reported failure",
            },
        )
    return _workspace_to_response(workspace)


async def _observe_workspace(workspace, operation_id):
    """Scope org-level replay to the registered workspace, restoring the context."""
    from dataclasses import replace
    from app.adapters.operation_authority_source import (
        acting_principal,
        set_acting_principal,
        reset_acting_principal,
    )

    caller = acting_principal()
    if (
        caller is None
        or caller.org_id != str(workspace.org_id)
        or caller.workspace_id not in ("", str(workspace.id))
    ):
        raise ProvisioningRefused("workspace observation principal mismatch")
    token = set_acting_principal(replace(caller, workspace_id=str(workspace.id)))
    try:
        return await observe(operation_id)
    finally:
        reset_acting_principal(token)


def _teardown_request_id(org_id: uuid.UUID, workspace_id: uuid.UUID) -> str:
    return str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"adp:superplane:{org_id}:workspace:{workspace_id}:teardown",
        )
    )


async def _reconcile_workspace_teardown(
    workspace: Workspace,
    org_id: uuid.UUID,
    db: AsyncSession,
    *,
    open_if_missing: bool,
) -> None:
    await db.refresh(workspace, with_for_update=True)
    if workspace.status != "Teardown":
        return
    try:
        if workspace.teardown_operation_id:
            progress = await _observe_workspace(
                workspace, workspace.teardown_operation_id
            )
        elif open_if_missing:
            progress = await start_teardown(
                operation_id=_teardown_request_id(org_id, workspace.id),
                workspace_id=str(workspace.id),
                org_id=str(org_id),
                workspace_name=workspace.name,
            )
            workspace.teardown_operation_id = progress.operation_id
        else:
            return
    except ProvisioningError:
        if open_if_missing:
            raise
        return

    if progress.is_conclusive_success:
        workspace.status = "Deleted"
    elif progress.is_conclusive_failure:
        workspace.status = "Failed"
    await db.commit()
    await db.refresh(workspace)
    logger.info("Workspace %s teardown: %s", workspace.id, summarize(progress))


@router.post("", response_model=WorkspaceResponse, status_code=status.HTTP_201_CREATED)
async def create_workspace(
    body: CreateWorkspaceRequest,
    org_id: uuid.UUID = Depends(get_current_org),
    db: AsyncSession = Depends(get_session),
) -> WorkspaceResponse:
    """Admit an approved plan before creating its workspace and ownership grant."""
    from app.adapters.operation_authority_source import (
        acting_principal,
        GrantBackedAuthority,
    )
    from app.database import async_session_factory
    from app.models.workspace_grant import WorkspaceGrantRecord
    from app.services.onboarding import normalized_request, preview
    from app.services.provisioning import start_planned_provision
    from harness_jobs.identity import OperationRequest

    body = normalized_request(body)
    operation_request = _operation_request(body)
    existing = await _workspace_for_operation(db, org_id, body, operation_request)
    if existing is not None:
        return await _reconcile_workspace_provisioning(
            db, org_id, body, operation_request
        )
    if get_operation_facade() is None:
        raise HTTPException(503, "Workspace provisioning service is unavailable")
    if not body.plan_revision:
        raise HTTPException(422, "A reviewed workspace plan revision is required")
    await enforce_workspace_creation_quota(org_id, db)
    try:
        plan = await preview(db, org_id, body)
        if plan["revision"] != body.plan_revision:
            raise HTTPException(
                409, "Workspace plan changed; review and approve the current revision"
            )
        approved_request = plan["approval_request"]
        if body.approval_id is not None:
            authority = GrantBackedAuthority(async_session_factory)
            principal = await authority.resolve(
                org_id=str(org_id),
                workspace_id=plan["workspace_id"],
                permission="workspace:provision",
            )
            context = await authority.approval_for(
                principal=principal,
                request=OperationRequest(
                    action="provision",
                    idempotency_key=str(body.operation_id),
                    parameters=approved_request["parameters"],
                ),
            )
            if context.record is None or context.record.approval_id != str(
                body.approval_id
            ):
                raise ProvisioningRefused(
                    "approval reference does not match this request"
                )
        progress = await start_planned_provision(
            operation_id=str(body.operation_id),
            workspace_id=plan["workspace_id"],
            org_id=str(org_id),
            parameters=approved_request["parameters"],
        )
    except ProvisioningRefused as error:
        raise HTTPException(403, str(error)) from None
    except ProvisioningError:
        raise HTTPException(
            503, "Workspace admission is unavailable; retain the request identity"
        ) from None
    caller = acting_principal()
    if caller is None:
        raise HTTPException(503, "Workspace ownership principal is unavailable")
    workspace = Workspace(
        id=uuid.UUID(plan["workspace_id"]),
        org_id=org_id,
        name=body.name,
        operation_id=body.operation_id,
        operation_request_json=operation_request,
        provisioning_operation_id=progress.operation_id,
        isolation_mode=body.isolation_mode,
        quotas_json=body.quotas_json,
        aws_account_id=uuid.UUID(plan["cloud_account_id"])
        if plan.get("cloud_account_id")
        else None,
        budget_max_daily_usd=body.budget_max_daily_usd,
        budget_max_gpus=body.budget_max_gpus,
        status={
            "succeeded": "Provisioning",
            "failed": "Failed",
            "cancelled": "Failed",
            "unknown": "Unknown",
        }.get(progress.state, "Provisioning"),
    )
    db.add(workspace)
    try:
        await db.flush()
        db.add(
            WorkspaceGrantRecord(
                workspace_id=workspace.id,
                org_id=org_id,
                principal=caller.subject,
                principal_type=caller.account_type,
                permissions="workspace:administer",
            )
        )
        await db.commit()
    except IntegrityError:
        await db.rollback()
        existing = await _workspace_for_operation(db, org_id, body, operation_request)
        if existing is None:
            raise
        return await _reconcile_workspace_provisioning(
            db, org_id, body, operation_request
        )
    await db.refresh(workspace)
    return _workspace_to_response(workspace).model_copy(
        update={"operation_state": progress.state}
    )


@router.get("", response_model=WorkspaceListResponse)
async def list_workspaces(
    org_id: uuid.UUID = Depends(get_current_org),
    db: AsyncSession = Depends(get_session),
) -> WorkspaceListResponse:
    """List all workspaces for the authenticated organization.

    Research workspaces appear with a [research] tag in the display_name.
    """
    result = await db.execute(
        select(Workspace)
        .where(Workspace.org_id == org_id)
        .order_by(Workspace.created_at.desc())
    )
    workspaces = result.scalars().all()
    return WorkspaceListResponse(
        workspaces=[_workspace_to_response(ws) for ws in workspaces],
        total=len(workspaces),
    )


@router.get("/eligible-clusters", response_model=EligibleClusterListResponse)
async def list_eligible_clusters(
    org_id: uuid.UUID = Depends(get_current_org),
    db: AsyncSession = Depends(get_session),
) -> EligibleClusterListResponse:
    """List clusters the caller's organization may select for shared placement.

    Issue #6048. Registered BEFORE ``/{workspace_id}`` so this literal path
    segment is not swallowed by that route's dynamic parameter. Returns only
    clusters explicitly ``sharing_enabled`` under the caller's own
    authenticated organization — never another organization's, even one in
    the same AWS account. See ``app/services/cluster_sharing.py``.
    """
    from app.services.cluster_sharing import (
        list_eligible_clusters as resolve_eligible_clusters,
    )

    eligible = await resolve_eligible_clusters(db, org_id)
    return EligibleClusterListResponse(
        clusters=[
            EligibleClusterResponse(
                id=cluster.id,
                name=cluster.name,
                cluster_arn=cluster.cluster_arn,
                platform_eligible=cluster.platform_eligible,
                member_count=cluster.member_count,
            )
            for cluster in eligible
        ]
    )


@router.get("/{workspace_id}", response_model=WorkspaceResponse)
async def get_workspace(
    workspace_id: uuid.UUID,
    org_id: uuid.UUID = Depends(get_current_org),
    db: AsyncSession = Depends(get_session),
) -> WorkspaceResponse:
    """Get workspace details including cluster health from last heartbeat."""
    result = await db.execute(
        select(Workspace).where(
            Workspace.id == workspace_id, Workspace.org_id == org_id
        )
    )
    workspace = result.scalar_one_or_none()

    if workspace is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Workspace not found"
        )

    await _reconcile_workspace_teardown(workspace, org_id, db, open_if_missing=False)

    # Fetch associated cluster health if available
    cluster: Cluster | None = None
    if workspace.cluster_id:
        cluster_result = await db.execute(
            select(Cluster).where(Cluster.id == workspace.cluster_id)
        )
        cluster = cluster_result.scalar_one_or_none()

    return _workspace_to_response(workspace, cluster)


@router.delete("/{workspace_id}", response_model=WorkspaceDeleteResponse)
async def delete_workspace(
    workspace_id: uuid.UUID,
    org_id: uuid.UUID = Depends(get_current_org),
    db: AsyncSession = Depends(get_session),
) -> WorkspaceDeleteResponse:
    """Teardown a workspace — updates status and triggers teardown workflow."""
    result = await db.execute(
        select(Workspace)
        .where(Workspace.id == workspace_id, Workspace.org_id == org_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    workspace = result.scalar_one_or_none()

    if workspace is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Workspace not found"
        )

    # Prevent deletion of the platform's default workspace
    if workspace.is_default:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cannot delete the platform's default workspace. "
            "The default workspace is managed by the platform and cannot be removed via CLI.",
        )

    if workspace.status == "Deleted":
        return WorkspaceDeleteResponse(id=workspace.id, status="Deleted")
    if workspace.status == "Teardown":
        try:
            await _reconcile_workspace_teardown(
                workspace, org_id, db, open_if_missing=True
            )
        except ProvisioningError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
            ) from exc
        if workspace.status == "Failed":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Workspace teardown operation reported failure",
            )
        return WorkspaceDeleteResponse(id=workspace.id, status=workspace.status)

    # Captured before the transition so a refused or unavailable teardown can be
    # rolled back to the status the workspace actually had.
    previous_status = workspace.status

    if get_operation_facade() is None:
        raise HTTPException(503, "Workspace provisioning service is unavailable")

    workspace.status = "Teardown"
    await db.commit()

    # Begin teardown under an authorized operation. Teardown is the same authority
    # question as provisioning with the opposite effect, so it goes through the
    # same facade and the same permission rather than a weaker local check.
    try:
        await _reconcile_workspace_teardown(
            workspace,
            org_id,
            db,
            open_if_missing=True,
        )
    except ProvisioningRefused as exc:
        # Restore the prior status: the workspace was not torn down, and leaving it
        # in Teardown would make a refused request look like one in progress.
        await db.refresh(workspace, with_for_update=True)
        if workspace.status == "Teardown" and workspace.teardown_operation_id is None:
            workspace.status = previous_status
        await db.commit()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    except ProvisioningError as exc:
        # Admission may have committed before its reply was lost. Keep the
        # durable teardown intent, blocking new use until the same request is
        # reconciled. Restoring Active here would advertise capacity being removed.
        logger.error("Teardown unavailable for workspace %s: %s", workspace.id, exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc

    if workspace.status == "Failed":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Workspace teardown operation reported failure",
        )

    return WorkspaceDeleteResponse(id=workspace.id, status=workspace.status)


@router.post("/{workspace_id}/kubeconfig", response_model=KubeconfigResponse)
async def generate_kubeconfig(
    workspace_id: uuid.UUID,
    org_id: uuid.UUID = Depends(get_current_org),
    db: AsyncSession = Depends(get_session),
) -> KubeconfigResponse:
    """Generate a scoped kubeconfig for direct cluster access.

    Exceptional path: brokered API operations are the default for workload access. The
    exported config carries no credential — it uses an ``aws eks get-token`` exec plugin,
    so the holder must still be authorized to assume the workspace role — and it always
    pins the cluster's CA so clients verify the cluster's identity.
    """
    from app.services.eks_auth import EksAuthError, describe_cluster_ca
    from app.services.kubeconfig import KubeconfigError
    from app.services.kubeconfig import generate_kubeconfig as gen_kubeconfig
    from app.services.proxy import ProxyError

    result = await db.execute(
        select(Workspace).where(
            Workspace.id == workspace_id, Workspace.org_id == org_id
        )
    )
    workspace = result.scalar_one_or_none()

    if workspace is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Workspace not found"
        )

    if workspace.status not in {STATUS_ACTIVE, "Active"}:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Workspace is not active (status: {workspace.status}). Kubeconfig requires an active workspace.",
        )

    if not workspace.cluster_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No cluster associated with this workspace",
        )

    # Fetch cluster details
    cluster_result = await db.execute(
        select(Cluster).where(Cluster.id == workspace.cluster_id)
    )
    cluster = cluster_result.scalar_one_or_none()

    if cluster is None or not cluster.endpoint:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cluster endpoint not available",
        )

    # Account, region and EKS cluster name all come from the cluster's ARN, which is
    # authoritative: the workspace cluster need not be in the control plane's own region,
    # and its real EKS name need not match the display name on the row.
    from app.services.proxy import (
        _get_aws_account_from_cluster,
        _get_cluster_name_from_arn,
        _get_region_from_cluster,
        _get_workspace_external_id,
        assume_role_for_cluster,
    )

    workspace_aws_account_id = _get_aws_account_from_cluster(cluster)

    if not workspace_aws_account_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot determine AWS account for kubeconfig generation",
        )

    eks_cluster_name = _get_cluster_name_from_arn(cluster)
    cluster_region = _get_region_from_cluster(cluster)

    # Read the tenant's ExternalId from their stored cloud-account record (U16a, #5051)
    # so the exported config's exec plugin can satisfy a trust policy that requires it.
    external_id = await _get_workspace_external_id(workspace, db)

    # Resolve the cluster CA from EKS, which is authoritative for it. Generation is
    # refused if it cannot be resolved rather than emitting a config that would connect
    # without verifying the cluster's identity.
    try:
        credentials = assume_role_for_cluster(
            workspace_aws_account_id,
            workspace.name,
            session_suffix="kubeconfig",
            external_id=external_id,
        )
        cluster_ca_cert = describe_cluster_ca(
            cluster_name=eks_cluster_name,
            credentials=credentials,
            region=cluster_region,
        )
    except (EksAuthError, ProxyError) as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Cannot generate kubeconfig: {exc}",
        ) from exc

    try:
        kubeconfig_yaml, expires_at = gen_kubeconfig(
            cluster_endpoint=cluster.endpoint,
            cluster_ca_cert=cluster_ca_cert,
            cluster_name=eks_cluster_name,
            workspace_aws_account_id=workspace_aws_account_id,
            workspace_name=workspace.name,
            region=cluster_region,
            external_id=external_id,
        )
    except KubeconfigError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(exc),
        ) from exc
    except RuntimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
    except Exception as exc:
        logger.error(
            "Kubeconfig generation failed for workspace %s: %s", workspace_id, exc
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Failed to generate kubeconfig. Check server logs for details.",
        ) from exc

    return KubeconfigResponse(kubeconfig=kubeconfig_yaml, expires_at=expires_at)
