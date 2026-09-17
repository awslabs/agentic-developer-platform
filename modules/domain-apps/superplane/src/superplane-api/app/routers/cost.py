"""Cost aggregation and budget enforcement endpoints.

Routes:
    GET  /workspaces/{id}/cost    — workspace cost aggregation
    GET  /workspaces/{id}/budget  — workspace budget status
    GET  /orgs/cost               — org-level cost aggregation
    POST /internal/cost-reconcile — trigger cost reconciliation (internal)
"""

import logging
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.middleware.auth import get_current_org
from app.schemas.proxy import (
    BudgetStatusResponse,
    CostResponse,
    OrgCostResponse,
    ReconcileResponse,
)
from app.services.cost import get_workspace_cost
from app.services.cost_reconciler import (
    CostReconciler,
    get_org_cost_summary,
    get_workspace_budget_status,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["cost"])


@router.get("/workspaces/{workspace_id}/cost", response_model=CostResponse)
async def get_cost(
    workspace_id: uuid.UUID,
    start_date: datetime | None = Query(
        default=None, description="Start of cost window (ISO 8601)"
    ),
    end_date: datetime | None = Query(
        default=None, description="End of cost window (ISO 8601)"
    ),
    org_id: uuid.UUID = Depends(get_current_org),
    db: AsyncSession = Depends(get_session),
) -> CostResponse:
    """Get aggregated cost data for a workspace.

    Queries node data from Postgres, computes running hours * hourly cost rate,
    and returns totals broken down by GPU type and cloud provider.
    """
    result = await get_workspace_cost(
        workspace_id=workspace_id,
        org_id=org_id,
        db=db,
        start_date=start_date,
        end_date=end_date,
    )

    if "error" in result:
        raise HTTPException(status_code=result["status_code"], detail=result["error"])

    return CostResponse(**result)


@router.get("/workspaces/{workspace_id}/budget", response_model=BudgetStatusResponse)
async def get_budget_status(
    workspace_id: uuid.UUID,
    org_id: uuid.UUID = Depends(get_current_org),
    db: AsyncSession = Depends(get_session),
) -> BudgetStatusResponse:
    """Get budget status for a workspace including current usage and active alerts."""
    result = await get_workspace_budget_status(
        workspace_id=workspace_id,
        org_id=org_id,
        db=db,
    )

    if "error" in result:
        raise HTTPException(status_code=result["status_code"], detail=result["error"])

    return BudgetStatusResponse(**result)


@router.get("/orgs/cost", response_model=OrgCostResponse)
async def get_org_cost(
    start_date: datetime | None = Query(
        default=None, description="Start of cost window (ISO 8601)"
    ),
    end_date: datetime | None = Query(
        default=None, description="End of cost window (ISO 8601)"
    ),
    org_id: uuid.UUID = Depends(get_current_org),
    db: AsyncSession = Depends(get_session),
) -> OrgCostResponse:
    """Get aggregated cost data across all workspaces for the current organization."""
    result = await get_org_cost_summary(
        org_id=org_id,
        db=db,
        start_date=start_date,
        end_date=end_date,
    )
    return OrgCostResponse(**result)


@router.post("/internal/cost-reconcile", response_model=ReconcileResponse)
async def trigger_cost_reconcile(
    db: AsyncSession = Depends(get_session),
) -> ReconcileResponse:
    """Trigger a cost reconciliation cycle (internal endpoint).

    This endpoint is called by a cron job or background task to enforce
    budget limits across all active workspaces.
    """
    reconciler = CostReconciler(db)
    result = await reconciler.reconcile()
    return ReconcileResponse(**result)
