"""Quota management endpoints — set and view per-org and per-workspace quotas.

Routes:
    PATCH /workspaces/{id}/quota  — set per-workspace quotas
    GET   /workspaces/{id}/quota  — get workspace quota + current usage
    PATCH /orgs/current/quota     — set org-wide quotas
    GET   /orgs/current/quota     — get org quota + current usage
"""

import json
import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.middleware.auth import get_current_org
from app.models.organization import Organization
from app.models.workspace import Workspace
from app.schemas.quota import (
    OrgQuotaResponse,
    QuotaLimits,
    QuotaUsage,
    SetOrgQuotaRequest,
    SetWorkspaceQuotaRequest,
    WorkspaceQuotaResponse,
)
from app.services.quota import (
    count_org_gpus,
    count_org_nodes,
    count_org_workspaces,
    count_workspace_gpus,
    count_workspace_nodes,
    get_org_quotas,
    get_workspace_quotas,
    merge_quotas,
    parse_quotas,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["quotas"])


@router.patch("/workspaces/{workspace_id}/quota", response_model=WorkspaceQuotaResponse)
async def set_workspace_quota(
    workspace_id: uuid.UUID,
    body: SetWorkspaceQuotaRequest,
    org_id: uuid.UUID = Depends(get_current_org),
    db: AsyncSession = Depends(get_session),
) -> WorkspaceQuotaResponse:
    """Set per-workspace quotas (max GPUs, max cost/day, max nodes, allowed clouds).

    Only provided fields are updated; omitted fields retain their current values.
    """
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

    # Parse existing quotas
    existing = parse_quotas(workspace.quotas_json)

    # Build update dict from non-None request fields
    updates: dict = {}
    if body.max_gpus is not None:
        updates["max_gpus"] = body.max_gpus
    if body.max_cost_per_day is not None:
        updates["max_cost_per_day"] = float(body.max_cost_per_day)
    if body.max_nodes is not None:
        updates["max_nodes"] = body.max_nodes
    if body.allowed_clouds is not None:
        updates["allowed_clouds"] = body.allowed_clouds

    merged = merge_quotas(existing, updates)
    workspace.quotas_json = json.dumps(merged)

    # Also sync budget fields on the workspace model for CostReconciler compatibility
    if body.max_cost_per_day is not None:
        workspace.budget_max_daily_usd = body.max_cost_per_day
    if body.max_gpus is not None:
        workspace.budget_max_gpus = body.max_gpus

    await db.commit()
    await db.refresh(workspace)

    # Compute current usage
    current_gpus = await count_workspace_gpus(workspace, db)
    current_nodes = await count_workspace_nodes(workspace, db)

    violations = _check_violations(merged, current_gpus, 0, current_nodes)

    return WorkspaceQuotaResponse(
        workspace_id=workspace.id,
        workspace_name=workspace.name,
        quotas=QuotaLimits(
            **{k: v for k, v in merged.items() if k in QuotaLimits.model_fields}
        ),
        usage=QuotaUsage(
            current_gpus=current_gpus,
            current_nodes=current_nodes,
        ),
        within_limits=len(violations) == 0,
        violations=violations,
        updated_at=workspace.updated_at,
    )


@router.get("/workspaces/{workspace_id}/quota", response_model=WorkspaceQuotaResponse)
async def get_workspace_quota(
    workspace_id: uuid.UUID,
    org_id: uuid.UUID = Depends(get_current_org),
    db: AsyncSession = Depends(get_session),
) -> WorkspaceQuotaResponse:
    """Get workspace quota limits and current usage."""
    workspace, quotas = await get_workspace_quotas(workspace_id, org_id, db)

    current_gpus = await count_workspace_gpus(workspace, db)
    current_nodes = await count_workspace_nodes(workspace, db)

    violations = _check_violations(quotas, current_gpus, 0, current_nodes)

    return WorkspaceQuotaResponse(
        workspace_id=workspace.id,
        workspace_name=workspace.name,
        quotas=QuotaLimits(
            **{k: v for k, v in quotas.items() if k in QuotaLimits.model_fields}
        ),
        usage=QuotaUsage(
            current_gpus=current_gpus,
            current_nodes=current_nodes,
        ),
        within_limits=len(violations) == 0,
        violations=violations,
        updated_at=workspace.updated_at,
    )


@router.patch("/orgs/current/quota", response_model=OrgQuotaResponse)
async def set_org_quota(
    body: SetOrgQuotaRequest,
    org_id: uuid.UUID = Depends(get_current_org),
    db: AsyncSession = Depends(get_session),
) -> OrgQuotaResponse:
    """Set org-wide quotas (max GPUs, max cost/day, max workspaces, max nodes, allowed clouds).

    Only provided fields are updated; omitted fields retain their current values.
    """
    result = await db.execute(select(Organization).where(Organization.id == org_id))
    org = result.scalar_one_or_none()
    if org is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Organization not found"
        )

    existing = parse_quotas(org.quotas_json, org.billing_plan)

    updates: dict = {}
    if body.max_gpus is not None:
        updates["max_gpus"] = body.max_gpus
    if body.max_cost_per_day is not None:
        updates["max_cost_per_day"] = float(body.max_cost_per_day)
    if body.max_workspaces is not None:
        updates["max_workspaces"] = body.max_workspaces
    if body.max_nodes is not None:
        updates["max_nodes"] = body.max_nodes
    if body.allowed_clouds is not None:
        updates["allowed_clouds"] = body.allowed_clouds

    merged = merge_quotas(existing, updates)
    org.quotas_json = json.dumps(merged)

    await db.commit()
    await db.refresh(org)

    # Compute current usage
    current_workspaces = await count_org_workspaces(org_id, db)
    current_gpus = await count_org_gpus(org_id, db)
    current_nodes = await count_org_nodes(org_id, db)

    violations = _check_violations(
        merged, current_gpus, current_workspaces, current_nodes
    )

    return OrgQuotaResponse(
        org_id=org.id,
        org_name=org.name,
        billing_plan=org.billing_plan,
        quotas=QuotaLimits(
            **{k: v for k, v in merged.items() if k in QuotaLimits.model_fields}
        ),
        usage=QuotaUsage(
            current_gpus=current_gpus,
            current_workspaces=current_workspaces,
            current_nodes=current_nodes,
        ),
        within_limits=len(violations) == 0,
        violations=violations,
        updated_at=org.created_at,
    )


@router.get("/orgs/current/quota", response_model=OrgQuotaResponse)
async def get_org_quota(
    org_id: uuid.UUID = Depends(get_current_org),
    db: AsyncSession = Depends(get_session),
) -> OrgQuotaResponse:
    """Get org-level quota limits and current usage."""
    org, quotas = await get_org_quotas(org_id, db)

    current_workspaces = await count_org_workspaces(org_id, db)
    current_gpus = await count_org_gpus(org_id, db)
    current_nodes = await count_org_nodes(org_id, db)

    violations = _check_violations(
        quotas, current_gpus, current_workspaces, current_nodes
    )

    return OrgQuotaResponse(
        org_id=org.id,
        org_name=org.name,
        billing_plan=org.billing_plan,
        quotas=QuotaLimits(
            **{k: v for k, v in quotas.items() if k in QuotaLimits.model_fields}
        ),
        usage=QuotaUsage(
            current_gpus=current_gpus,
            current_workspaces=current_workspaces,
            current_nodes=current_nodes,
        ),
        within_limits=len(violations) == 0,
        violations=violations,
        updated_at=org.created_at,
    )


def _check_violations(
    quotas: dict,
    current_gpus: int,
    current_workspaces: int,
    current_nodes: int,
) -> list[str]:
    """Check current usage against quota limits and return a list of violation descriptions."""
    violations: list[str] = []

    max_gpus = quotas.get("max_gpus")
    if max_gpus is not None and current_gpus > max_gpus:
        violations.append(f"GPU usage ({current_gpus}) exceeds limit ({max_gpus})")

    max_workspaces = quotas.get("max_workspaces")
    if max_workspaces is not None and current_workspaces > max_workspaces:
        violations.append(
            f"Workspace count ({current_workspaces}) exceeds limit ({max_workspaces})"
        )

    max_nodes = quotas.get("max_nodes")
    if max_nodes is not None and current_nodes > max_nodes:
        violations.append(f"Node count ({current_nodes}) exceeds limit ({max_nodes})")

    return violations
