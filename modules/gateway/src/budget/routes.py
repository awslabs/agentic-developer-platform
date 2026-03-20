"""Budget management REST API routes.

Issue #133: Security Fix - All budget routes now require valid Cognito JWT authentication.
The mock get_current_user() that returned hardcoded user context has been replaced with
real Cognito JWT validation via src.auth.dependencies.get_current_user.
"""

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth.dependencies import get_current_user  # Issue #133: Real Cognito JWT auth
from src.shared.database import get_db
from src.shared.exceptions import ValidationError
from src.shared.schemas.auth import TokenContext
from src.shared.schemas.budget import (
    BudgetCreateRequest,
    BudgetResponse,
    BudgetStatusResponse,
    BudgetUpdateRequest,
    BudgetUsageResponse,
    CostCalculationRequest,
    CostCalculationResponse,
    CostRecordRequest,
    EntityType,
    PeriodType,
)

from .service import BudgetService

router = APIRouter(prefix="/budgets", tags=["budgets"])


def get_budget_service(session: AsyncSession = Depends(get_db)) -> BudgetService:
    """Dependency to get budget service instance."""
    return BudgetService(session)


# Issue #133: CRITICAL SECURITY FIX
# The mock get_current_user() that returned hardcoded user context has been REMOVED.
# Authentication is now handled by src.auth.dependencies.get_current_user which
# validates Cognito JWT tokens and extracts real user context from claims.


# Budget Management Routes


@router.post("/", response_model=BudgetResponse, status_code=status.HTTP_201_CREATED)
async def create_budget(
    request: BudgetCreateRequest,
    current_user: TokenContext = Depends(get_current_user),
    budget_service: BudgetService = Depends(get_budget_service),
):
    """Create a new budget configuration."""
    try:
        return await budget_service.create_budget(request, current_user.org_id)
    except ValidationError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except Exception:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal server error")


@router.get("/{budget_id}", response_model=BudgetResponse)
async def get_budget(
    budget_id: str,
    current_user: TokenContext = Depends(get_current_user),
    budget_service: BudgetService = Depends(get_budget_service),
):
    """Retrieve a specific budget configuration."""
    budget = await budget_service.get_budget(budget_id, current_user.org_id)
    if not budget:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Budget not found")
    return budget


@router.get("/entity/{entity_type}/{entity_id}", response_model=list[BudgetResponse])
async def get_budgets_for_entity(
    entity_type: EntityType,
    entity_id: str,
    current_user: TokenContext = Depends(get_current_user),
    budget_service: BudgetService = Depends(get_budget_service),
):
    """Get all budget configurations for a specific entity."""
    return await budget_service.get_budgets_for_entity(entity_type, entity_id, current_user.org_id)


@router.put("/{budget_id}", response_model=BudgetResponse)
async def update_budget(
    budget_id: str,
    request: BudgetUpdateRequest,
    current_user: TokenContext = Depends(get_current_user),
    budget_service: BudgetService = Depends(get_budget_service),
):
    """Update an existing budget configuration."""
    try:
        budget = await budget_service.update_budget(budget_id, request, current_user.org_id)
        if not budget:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Budget not found")
        return budget
    except HTTPException:
        raise
    except ValidationError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except Exception:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal server error")


@router.delete("/{budget_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_budget(
    budget_id: str,
    current_user: TokenContext = Depends(get_current_user),
    budget_service: BudgetService = Depends(get_budget_service),
):
    """Delete a budget configuration."""
    success = await budget_service.delete_budget(budget_id, current_user.org_id)
    if not success:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Budget not found")


# Budget Status and Usage Routes


@router.get("/status/{entity_type}/{entity_id}", response_model=BudgetStatusResponse)
async def get_budget_status(
    entity_type: EntityType,
    entity_id: str,
    period_type: PeriodType = Query(..., description="Budget period type"),
    current_user: TokenContext = Depends(get_current_user),
    budget_service: BudgetService = Depends(get_budget_service),
):
    """Get current budget status and usage for an entity."""
    status_response = await budget_service.get_budget_status(entity_type, entity_id, period_type, current_user.org_id)
    if not status_response:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Budget not found for {entity_type.value} {entity_id} with period {period_type.value}"
        )
    return status_response


@router.get("/usage/{entity_type}/{entity_id}", response_model=BudgetUsageResponse)
async def get_budget_usage(
    entity_type: EntityType,
    entity_id: str,
    period_type: PeriodType = Query(..., description="Budget period type"),
    current_user: TokenContext = Depends(get_current_user),
    budget_service: BudgetService = Depends(get_budget_service),
):
    """Get usage statistics for an entity's budget."""
    usage = await budget_service.get_budget_usage(entity_type, entity_id, period_type, current_user.org_id)
    if not usage:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Usage not found for {entity_type.value} {entity_id} with period {period_type.value}"
        )
    return usage


# Cost Management Routes


@router.post("/calculate-cost", response_model=CostCalculationResponse)
async def calculate_cost(
    request: CostCalculationRequest,
    budget_service: BudgetService = Depends(get_budget_service),
):
    """Calculate the cost for a given model and token usage."""
    return await budget_service.calculate_cost(request)


@router.post("/record-cost", status_code=status.HTTP_204_NO_CONTENT)
async def record_cost(
    request: CostRecordRequest,
    current_user: TokenContext = Depends(get_current_user),
    budget_service: BudgetService = Depends(get_budget_service),
):
    """Record a cost entry against entity budgets."""
    try:
        await budget_service.record_cost(request, current_user.org_id)
    except Exception:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal server error")


# Admin and Reporting Routes


@router.get("/summary/{entity_type}/{entity_id}")
async def get_budget_summary(
    entity_type: str,
    entity_id: str,
    current_user: TokenContext = Depends(get_current_user),
    budget_service: BudgetService = Depends(get_budget_service),
):
    """Get comprehensive budget summary including hierarchy and usage."""
    return await budget_service.get_budget_summary(entity_type, entity_id, current_user.org_id)


@router.get("/organization/overview")
async def get_organization_budget_overview(
    current_user: TokenContext = Depends(get_current_user),
    budget_service: BudgetService = Depends(get_budget_service),
):
    """Get organization-wide budget overview including all entities."""
    return await budget_service.get_organization_budget_overview(current_user.org_id)


@router.get("/organization/alerts")
async def get_budget_alerts(
    threshold_percent: float = Query(80.0, ge=0, le=100, description="Alert threshold percentage"),
    current_user: TokenContext = Depends(get_current_user),
    budget_service: BudgetService = Depends(get_budget_service),
):
    """Get budget alerts for entities approaching or exceeding their budgets."""
    return await budget_service.get_budget_alerts(current_user.org_id, threshold_percent)
