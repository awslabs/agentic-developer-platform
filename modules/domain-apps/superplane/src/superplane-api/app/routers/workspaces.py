"""Workspace CRUD endpoints — scoped to org_id from JWT."""

import logging
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.middleware.auth import get_current_org
from app.models.cluster import Cluster
from app.models.workspace import Workspace
from app.schemas.workspace import (
    CreateWorkspaceRequest,
    KubeconfigResponse,
    WorkspaceDeleteResponse,
    WorkspaceListResponse,
    WorkspaceResponse,
)
from app.services.github import trigger_bootstrap, trigger_teardown
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


@router.post("", response_model=WorkspaceResponse, status_code=status.HTTP_201_CREATED)
async def create_workspace(
    body: CreateWorkspaceRequest,
    org_id: uuid.UUID = Depends(get_current_org),
    db: AsyncSession = Depends(get_session),
) -> WorkspaceResponse:
    """Create a new workspace.

    Validates quota, inserts a row with status=Provisioning, and triggers
    the bootstrap-workspace.yml GitHub Actions workflow.

    For research workspaces:
    - An AWS account is mandatory (validated by schema)
    - Budget guardrails default to RESEARCH_DEFAULT_BUDGET if not provided
    - Agent IAM role is created during bootstrap with permissive policies
    """
    await enforce_workspace_creation_quota(org_id, db)

    # Apply default budget guardrails for research workspaces
    budget_max_daily_usd = body.budget_max_daily_usd
    budget_max_gpus = body.budget_max_gpus
    if body.isolation_mode == "research":
        if budget_max_daily_usd is None:
            budget_max_daily_usd = Decimal("100.00")
        if budget_max_gpus is None:
            budget_max_gpus = 8

    workspace = Workspace(
        org_id=org_id,
        name=body.name,
        isolation_mode=body.isolation_mode,
        quotas_json=body.quotas_json,
        budget_max_daily_usd=budget_max_daily_usd,
        budget_max_gpus=budget_max_gpus,
        status="Provisioning",
    )
    db.add(workspace)
    await db.commit()
    await db.refresh(workspace)

    # Fire-and-forget: trigger the bootstrap workflow
    triggered = await trigger_bootstrap(
        workspace_id=str(workspace.id),
        workspace_name=workspace.name,
        org_id=str(org_id),
        isolation_mode=workspace.isolation_mode,
        account=body.account or "",
    )
    if not triggered:
        logger.warning(
            "Bootstrap workflow not triggered for workspace %s — check GITHUB_TOKEN",
            workspace.id,
        )

    return _workspace_to_response(workspace)


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
        select(Workspace).where(
            Workspace.id == workspace_id, Workspace.org_id == org_id
        )
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

    if workspace.status in ("Teardown", "Deleted"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Workspace already in {workspace.status} state",
        )

    workspace.status = "Teardown"
    await db.commit()

    # Trigger teardown workflow
    triggered = await trigger_teardown(
        workspace_id=str(workspace.id),
        workspace_name=workspace.name,
        org_id=str(org_id),
    )
    if not triggered:
        logger.warning(
            "Teardown workflow not triggered for workspace %s — check GITHUB_TOKEN",
            workspace.id,
        )

    return WorkspaceDeleteResponse(id=workspace.id, status="Teardown")


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

    if workspace.status != "Active":
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
