from abc import ABC, abstractmethod
from decimal import Decimal
from typing import Any

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
    EnforcementResult,
    EntityType,
    PeriodType,
)
from src.shared.schemas.common import BudgetCheckResult


class IBudgetService(ABC):
    """Interface for budget service with hierarchical enforcement and cost tracking."""

    # Budget Management Methods
    @abstractmethod
    async def create_budget(self, request: BudgetCreateRequest, org_id: str) -> BudgetResponse:
        """Create a new budget configuration."""
        ...

    @abstractmethod
    async def get_budget(self, budget_id: str, org_id: str) -> BudgetResponse | None:
        """Retrieve a specific budget configuration."""
        ...

    @abstractmethod
    async def get_budgets_for_entity(self, entity_type: EntityType, entity_id: str, org_id: str) -> list[BudgetResponse]:
        """Get all budget configurations for a specific entity."""
        ...

    @abstractmethod
    async def update_budget(self, budget_id: str, request: BudgetUpdateRequest, org_id: str) -> BudgetResponse | None:
        """Update an existing budget configuration."""
        ...

    @abstractmethod
    async def delete_budget(self, budget_id: str, org_id: str) -> bool:
        """Delete a budget configuration."""
        ...

    # Budget Status and Usage Methods
    @abstractmethod
    async def get_budget_status(self, entity_type: EntityType, entity_id: str, period_type: PeriodType, org_id: str) -> BudgetStatusResponse | None:
        """Get current budget status and usage for an entity."""
        ...

    @abstractmethod
    async def get_budget_usage(self, entity_type: EntityType, entity_id: str, period_type: PeriodType, org_id: str) -> BudgetUsageResponse | None:
        """Get usage statistics for an entity's budget."""
        ...

    # Budget Enforcement Methods
    @abstractmethod
    async def check_budget(self, context: TokenContext) -> BudgetCheckResult:
        """Check if a request is allowed under current budget constraints."""
        ...

    @abstractmethod
    async def check_budget_with_cost(self, context: TokenContext, estimated_cost_usd: Decimal) -> EnforcementResult:
        """Check if a request with specific cost is allowed under budget constraints."""
        ...

    @abstractmethod
    async def record_usage(self, context: TokenContext, tokens_in: int, tokens_out: int, model: str) -> None:
        """Record usage against budgets after a request completes."""
        ...

    @abstractmethod
    async def record_cost(self, request: CostRecordRequest, org_id: str) -> None:
        """Record a cost entry against entity budgets."""
        ...

    # Cost Calculation Methods
    @abstractmethod
    async def calculate_cost(self, request: CostCalculationRequest) -> CostCalculationResponse:
        """Calculate the cost for a given model and token usage."""
        ...

    # Hierarchical Enforcement Methods
    @abstractmethod
    async def check_hierarchical_budget(self, context: TokenContext, estimated_cost_usd: Decimal) -> EnforcementResult:
        """Check budget constraints across the entire hierarchy (user → team → dept → org)."""
        ...

    @abstractmethod
    async def validate_budget_hierarchy(self, entity_type: EntityType, entity_id: str, budget_amount_usd: Decimal, org_id: str) -> bool:
        """Validate that a budget amount doesn't exceed parent budget limits."""
        ...

    # Admin and Reporting Methods
    @abstractmethod
    async def get_budget_summary(self, entity_type: str, entity_id: str, org_id: str) -> dict[str, Any]:
        """Get comprehensive budget summary including hierarchy and usage."""
        ...

    @abstractmethod
    async def get_organization_budget_overview(self, org_id: str) -> dict[str, Any]:
        """Get organization-wide budget overview including all entities."""
        ...

    @abstractmethod
    async def get_budget_alerts(self, org_id: str, threshold_percent: float = 80.0) -> list[dict[str, Any]]:
        """Get budget alerts for entities approaching or exceeding their budgets."""
        ...
