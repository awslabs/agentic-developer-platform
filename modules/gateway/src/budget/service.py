from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.database import get_session_factory
from src.shared.exceptions import ValidationError
from src.shared.interfaces.budget import IBudgetService
from src.shared.logging import get_logger
from src.shared.metrics import emit_budget_utilization
from src.shared.models.budget import BudgetConfig, BudgetUsage
from src.shared.models.organization import Team, User
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
    EnforcementMode,
    EnforcementResult,
    EntityType,
    PeriodType,
)
from src.shared.schemas.common import BudgetCheckResult

from .config import budget_config
from .utils import (
    calculate_budget_utilization,
    calculate_model_cost,
    generate_budget_warnings,
    get_parent_entity_info,
    get_period_start_end,
    is_budget_exceeded,
    validate_budget_amount,
)

logger = get_logger(__name__)


class BudgetService(IBudgetService):
    """Service for managing budgets with hierarchical enforcement and cost tracking."""

    def __init__(self, db_session: AsyncSession = None):
        self.db_session = db_session

    @asynccontextmanager
    async def _get_session(self) -> AsyncIterator[AsyncSession]:
        """Get database session as async context manager."""
        if self.db_session:
            # Use injected session directly (don't close it)
            yield self.db_session
        else:
            # Create a new session from factory
            factory = get_session_factory()
            async with factory() as session:
                yield session

    # Budget Management Methods

    async def create_budget(self, request: BudgetCreateRequest, org_id: str) -> BudgetResponse:
        """Create a new budget configuration."""
        async with self._get_session() as session:
            # Validate budget amount
            if not validate_budget_amount(request.budget_amount_usd):
                raise ValidationError("Budget amount must be positive and reasonable")

            # Check if budget already exists for this entity/period combination
            existing = await session.execute(
                select(BudgetConfig).where(
                    and_(
                        BudgetConfig.org_id == org_id,
                        BudgetConfig.entity_type == request.entity_type.value,
                        BudgetConfig.entity_id == request.entity_id,
                        BudgetConfig.period_type == request.period_type.value,
                    )
                )
            )
            if existing.scalar_one_or_none():
                raise ValidationError(
                    f"Budget already exists for {request.entity_type.value} {request.entity_id} with period {request.period_type.value}"
                )

            # Validate hierarchical constraints
            if not await self.validate_budget_hierarchy(request.entity_type, request.entity_id, request.budget_amount_usd, org_id):
                raise ValidationError("Budget amount exceeds parent budget constraints")

            # Create budget
            budget = BudgetConfig(
                org_id=org_id,
                entity_type=request.entity_type.value,
                entity_id=request.entity_id,
                period_type=request.period_type.value,
                budget_amount_usd=request.budget_amount_usd,
                enforcement_mode=request.enforcement_mode.value,
            )

            session.add(budget)
            await session.commit()
            await session.refresh(budget)

            return BudgetResponse.from_orm(budget)

    async def get_budget(self, budget_id: str, org_id: str) -> BudgetResponse | None:
        """Retrieve a specific budget configuration."""
        async with self._get_session() as session:
            result = await session.execute(select(BudgetConfig).where(and_(BudgetConfig.id == budget_id, BudgetConfig.org_id == org_id)))
            budget = result.scalar_one_or_none()
            return BudgetResponse.from_orm(budget) if budget else None

    async def get_budgets_for_entity(self, entity_type: EntityType, entity_id: str, org_id: str) -> list[BudgetResponse]:
        """Get all budget configurations for a specific entity."""
        async with self._get_session() as session:
            result = await session.execute(
                select(BudgetConfig).where(
                    and_(
                        BudgetConfig.org_id == org_id,
                        BudgetConfig.entity_type == entity_type.value,
                        BudgetConfig.entity_id == entity_id,
                    )
                )
            )
            budgets = result.scalars().all()
            return [BudgetResponse.from_orm(budget) for budget in budgets]

    async def update_budget(self, budget_id: str, request: BudgetUpdateRequest, org_id: str) -> BudgetResponse | None:
        """Update an existing budget configuration."""
        async with self._get_session() as session:
            result = await session.execute(select(BudgetConfig).where(and_(BudgetConfig.id == budget_id, BudgetConfig.org_id == org_id)))
            budget = result.scalar_one_or_none()
            if not budget:
                return None

            # Validate new budget amount if provided
            if request.budget_amount_usd is not None:
                if not validate_budget_amount(request.budget_amount_usd):
                    raise ValidationError("Budget amount must be positive and reasonable")

                # Validate hierarchical constraints
                if not await self.validate_budget_hierarchy(EntityType(budget.entity_type), budget.entity_id, request.budget_amount_usd, org_id):
                    raise ValidationError("Budget amount exceeds parent budget constraints")

                budget.budget_amount_usd = request.budget_amount_usd

            if request.enforcement_mode is not None:
                budget.enforcement_mode = request.enforcement_mode.value

            budget.updated_at = datetime.utcnow()
            await session.commit()
            await session.refresh(budget)

            return BudgetResponse.from_orm(budget)

    async def delete_budget(self, budget_id: str, org_id: str) -> bool:
        """Delete a budget configuration."""
        async with self._get_session() as session:
            result = await session.execute(select(BudgetConfig).where(and_(BudgetConfig.id == budget_id, BudgetConfig.org_id == org_id)))
            budget = result.scalar_one_or_none()
            if not budget:
                return False

            await session.delete(budget)
            await session.commit()
            return True

    # Budget Status and Usage Methods

    async def get_budget_status(self, entity_type: EntityType, entity_id: str, period_type: PeriodType, org_id: str) -> BudgetStatusResponse | None:
        """Get current budget status and usage for an entity."""
        async with self._get_session() as session:
            # Get budget configuration
            budget_result = await session.execute(
                select(BudgetConfig).where(
                    and_(
                        BudgetConfig.org_id == org_id,
                        BudgetConfig.entity_type == entity_type.value,
                        BudgetConfig.entity_id == entity_id,
                        BudgetConfig.period_type == period_type.value,
                    )
                )
            )
            budget = budget_result.scalar_one_or_none()
            if not budget:
                return None

            # Get current usage
            period_start, period_end = get_period_start_end(period_type)
            usage_result = await session.execute(
                select(BudgetUsage).where(
                    and_(
                        BudgetUsage.org_id == org_id,
                        BudgetUsage.entity_type == entity_type.value,
                        BudgetUsage.entity_id == entity_id,
                        BudgetUsage.period_type == period_type.value,
                        BudgetUsage.period_start == period_start,
                    )
                )
            )
            usage = usage_result.scalar_one_or_none()

            current_spend = usage.total_cost_usd if usage else Decimal("0")
            remaining_budget = budget.budget_amount_usd - current_spend
            utilization = calculate_budget_utilization(budget.budget_amount_usd, current_spend)
            exceeded = is_budget_exceeded(budget.budget_amount_usd, current_spend)

            warnings = generate_budget_warnings(budget.budget_amount_usd, current_spend, period_type, entity_type)

            # Emit budget utilization metric
            emit_budget_utilization(
                org_id=org_id,
                entity_type=entity_type.value,
                entity_id=entity_id,
                utilization_percent=utilization,
            )

            logger.debug(
                "Budget status retrieved",
                extra={
                    "entity_type": entity_type.value,
                    "entity_id": entity_id,
                    "utilization_percent": round(utilization, 2),
                    "budget_exceeded": exceeded,
                },
            )

            return BudgetStatusResponse(
                budget_amount_usd=budget.budget_amount_usd,
                current_spend_usd=current_spend,
                remaining_budget_usd=remaining_budget,
                budget_utilization_percent=utilization,
                period_start=period_start,
                period_end=period_end,
                period_type=period_type,
                enforcement_mode=EnforcementMode(budget.enforcement_mode),
                budget_exceeded=exceeded,
                warnings=warnings,
            )

    async def get_budget_usage(self, entity_type: EntityType, entity_id: str, period_type: PeriodType, org_id: str) -> BudgetUsageResponse | None:
        """Get usage statistics for an entity's budget."""
        async with self._get_session() as session:
            period_start, _ = get_period_start_end(period_type)

            result = await session.execute(
                select(BudgetUsage).where(
                    and_(
                        BudgetUsage.org_id == org_id,
                        BudgetUsage.entity_type == entity_type.value,
                        BudgetUsage.entity_id == entity_id,
                        BudgetUsage.period_type == period_type.value,
                        BudgetUsage.period_start == period_start,
                    )
                )
            )
            usage = result.scalar_one_or_none()
            return BudgetUsageResponse.from_orm(usage) if usage else None

    # Budget Enforcement Methods

    async def check_budget(self, context: TokenContext) -> BudgetCheckResult:
        """Check if a request is allowed under current budget constraints."""
        # Use a small default cost for budget checking
        estimated_cost = Decimal("0.01")
        result = await self.check_hierarchical_budget(context, estimated_cost)

        return BudgetCheckResult(
            allowed=result.allowed,
            exceeded_level=result.exceeded_entity_type.value if result.exceeded_entity_type else None,
            exceeded_entity=result.exceeded_entity_id,
            budget_usd=float(result.budget_amount_usd) if result.budget_amount_usd else None,
            spent_usd=float(result.current_spend_usd) if result.current_spend_usd else None,
            enforcement_mode=result.enforcement_mode.value if result.enforcement_mode else None,
            warnings=result.warnings,
        )

    async def check_budget_with_cost(self, context: TokenContext, estimated_cost_usd: Decimal) -> EnforcementResult:
        """Check if a request with specific cost is allowed under budget constraints."""
        return await self.check_hierarchical_budget(context, estimated_cost_usd)

    async def record_usage(self, context: TokenContext, tokens_in: int, tokens_out: int, model: str) -> None:
        """Record usage against budgets after a request completes."""
        # Calculate cost
        cost, _, _ = calculate_model_cost(model, tokens_in, tokens_out)

        # Record for user
        await self.record_cost(
            CostRecordRequest(
                entity_type=EntityType.USER,
                entity_id=context.user_id,
                model_name=model,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                request_cost_usd=cost,
            ),
            context.org_id,
        )

        # Record for team
        await self.record_cost(
            CostRecordRequest(
                entity_type=EntityType.TEAM,
                entity_id=context.team_id,
                model_name=model,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                request_cost_usd=cost,
            ),
            context.org_id,
        )

        # Record for department
        await self.record_cost(
            CostRecordRequest(
                entity_type=EntityType.DEPARTMENT,
                entity_id=context.department_id,
                model_name=model,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                request_cost_usd=cost,
            ),
            context.org_id,
        )

        # Record for organization
        await self.record_cost(
            CostRecordRequest(
                entity_type=EntityType.ORGANIZATION,
                entity_id=context.org_id,
                model_name=model,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                request_cost_usd=cost,
            ),
            context.org_id,
        )

    async def record_cost(self, request: CostRecordRequest, org_id: str) -> None:
        """Record a cost entry against entity budgets."""
        async with self._get_session() as session:
            # Calculate cost if not provided
            if request.request_cost_usd is None:
                cost, _, _ = calculate_model_cost(request.model_name, request.tokens_in, request.tokens_out)
            else:
                cost = request.request_cost_usd

            # Get or create usage record for each period type
            for period_type in [PeriodType.DAILY, PeriodType.WEEKLY, PeriodType.MONTHLY]:
                period_start, _ = get_period_start_end(period_type)

                # Get or create usage record
                result = await session.execute(
                    select(BudgetUsage).where(
                        and_(
                            BudgetUsage.org_id == org_id,
                            BudgetUsage.entity_type == request.entity_type.value,
                            BudgetUsage.entity_id == request.entity_id,
                            BudgetUsage.period_type == period_type.value,
                            BudgetUsage.period_start == period_start,
                        )
                    )
                )
                usage = result.scalar_one_or_none()

                if not usage:
                    usage = BudgetUsage(
                        org_id=org_id,
                        entity_type=request.entity_type.value,
                        entity_id=request.entity_id,
                        period_start=period_start,
                        period_type=period_type.value,
                        total_cost_usd=Decimal("0"),
                        total_tokens=0,
                        request_count=0,
                    )
                    session.add(usage)

                # Update usage
                usage.total_cost_usd += cost
                usage.total_tokens += request.tokens_in + request.tokens_out
                usage.request_count += 1

            await session.commit()

    # Cost Calculation Methods

    async def calculate_cost(self, request: CostCalculationRequest) -> CostCalculationResponse:
        """Calculate the cost for a given model and token usage."""
        cost, input_cost_per_1k, output_cost_per_1k = calculate_model_cost(request.model_name, request.tokens_in, request.tokens_out)

        return CostCalculationResponse(
            model_name=request.model_name,
            tokens_in=request.tokens_in,
            tokens_out=request.tokens_out,
            cost_usd=cost,
            input_cost_per_1k_tokens=input_cost_per_1k,
            output_cost_per_1k_tokens=output_cost_per_1k,
        )

    # Hierarchical Enforcement Methods

    async def check_hierarchical_budget(self, context: TokenContext, estimated_cost_usd: Decimal) -> EnforcementResult:
        """Check budget constraints across the entire hierarchy (user → team → dept → org)."""
        async with self._get_session() as session:
            # Get entity hierarchy
            entities = get_parent_entity_info(
                EntityType.USER,
                user_id=context.user_id,
                team_id=context.team_id,
                department_id=context.department_id,
                org_id=context.org_id,
            )

            # Accumulate warnings across the hierarchy
            all_warnings = []

            # Check each entity in the hierarchy
            for entity_type, entity_id in entities:
                for period_type in [PeriodType.DAILY, PeriodType.WEEKLY, PeriodType.MONTHLY]:
                    result = await self._check_entity_budget(session, entity_type, entity_id, period_type, estimated_cost_usd, context.org_id)

                    if not result.allowed:
                        return result

                    # Accumulate warnings from allowed results
                    if result.warnings:
                        all_warnings.extend(result.warnings)

            # If we get here, all budget checks passed
            return EnforcementResult(allowed=True, warnings=all_warnings)

    async def _check_entity_budget(
        self,
        session: AsyncSession,
        entity_type: EntityType,
        entity_id: str,
        period_type: PeriodType,
        estimated_cost_usd: Decimal,
        org_id: str,
    ) -> EnforcementResult:
        """Check budget for a specific entity."""
        # Get budget configuration
        budget_result = await session.execute(
            select(BudgetConfig).where(
                and_(
                    BudgetConfig.org_id == org_id,
                    BudgetConfig.entity_type == entity_type.value,
                    BudgetConfig.entity_id == entity_id,
                    BudgetConfig.period_type == period_type.value,
                )
            )
        )
        budget = budget_result.scalar_one_or_none()

        if not budget:
            # No budget configured, allow request
            return EnforcementResult(allowed=True)

        # Get current usage
        period_start, _ = get_period_start_end(period_type)
        usage_result = await session.execute(
            select(BudgetUsage).where(
                and_(
                    BudgetUsage.org_id == org_id,
                    BudgetUsage.entity_type == entity_type.value,
                    BudgetUsage.entity_id == entity_id,
                    BudgetUsage.period_type == period_type.value,
                    BudgetUsage.period_start == period_start,
                )
            )
        )
        usage = usage_result.scalar_one_or_none()

        current_spend = usage.total_cost_usd if usage else Decimal("0")
        projected_spend = current_spend + estimated_cost_usd

        # Check if budget would be exceeded
        if projected_spend > budget.budget_amount_usd:
            enforcement_mode = EnforcementMode(budget.enforcement_mode)

            if enforcement_mode == EnforcementMode.HARD:
                return EnforcementResult(
                    allowed=False,
                    blocked_reason=f"Budget exceeded for {entity_type.value} {entity_id}",
                    exceeded_entity_type=entity_type,
                    exceeded_entity_id=entity_id,
                    budget_amount_usd=budget.budget_amount_usd,
                    current_spend_usd=current_spend,
                    enforcement_mode=enforcement_mode,
                )
            else:  # soft enforcement
                warnings = [f"Budget exceeded for {entity_type.value} {entity_id}"]
                return EnforcementResult(allowed=True, warnings=warnings)

        # Generate warnings for budget utilization
        utilization = calculate_budget_utilization(budget.budget_amount_usd, projected_spend)
        warnings = []

        if utilization >= budget_config.budget_warning_threshold_percent:
            warnings.append(f"{entity_type.value} {entity_id} budget at {utilization:.1f}% utilization")

        return EnforcementResult(allowed=True, warnings=warnings)

    async def validate_budget_hierarchy(self, entity_type: EntityType, entity_id: str, budget_amount_usd: Decimal, org_id: str) -> bool:
        """Validate that a budget amount doesn't exceed parent budget limits."""
        async with self._get_session() as session:
            # Get parent entities
            if entity_type == EntityType.USER:
                # For users, check team budget
                user_result = await session.execute(select(User).where(and_(User.id == entity_id, User.org_id == org_id)))
                user = user_result.scalar_one_or_none()
                if user:
                    parent_entities = [
                        (EntityType.TEAM, user.team_id),
                    ]
                else:
                    return True

            elif entity_type == EntityType.TEAM:
                # For teams, check department budget
                team_result = await session.execute(select(Team).where(and_(Team.id == entity_id, Team.org_id == org_id)))
                team = team_result.scalar_one_or_none()
                if team:
                    parent_entities = [
                        (EntityType.DEPARTMENT, team.department_id),
                    ]
                else:
                    return True

            elif entity_type == EntityType.DEPARTMENT:
                # For departments, check organization budget
                parent_entities = [
                    (EntityType.ORGANIZATION, org_id),
                ]

            else:
                # No parent validation needed for organization or service account
                return True

            # Check parent budgets
            for parent_type, parent_id in parent_entities:
                for period_type in [PeriodType.MONTHLY]:  # Only check monthly for hierarchy
                    parent_budget_result = await session.execute(
                        select(BudgetConfig).where(
                            and_(
                                BudgetConfig.org_id == org_id,
                                BudgetConfig.entity_type == parent_type.value,
                                BudgetConfig.entity_id == parent_id,
                                BudgetConfig.period_type == period_type.value,
                            )
                        )
                    )
                    parent_budget = parent_budget_result.scalar_one_or_none()

                    if parent_budget and budget_amount_usd > parent_budget.budget_amount_usd:
                        return False

            return True

    # Admin and Reporting Methods

    async def get_budget_summary(self, entity_type: str, entity_id: str, org_id: str) -> dict[str, Any]:
        """Get comprehensive budget summary including hierarchy and usage."""
        async with self._get_session() as session:
            summary = {
                "entity_type": entity_type,
                "entity_id": entity_id,
                "budgets": [],
                "current_usage": [],
                "hierarchy": [],
            }

            # Get all budgets for this entity
            budgets_result = await session.execute(
                select(BudgetConfig).where(
                    and_(
                        BudgetConfig.org_id == org_id,
                        BudgetConfig.entity_type == entity_type,
                        BudgetConfig.entity_id == entity_id,
                    )
                )
            )
            budgets = budgets_result.scalars().all()

            for budget in budgets:
                budget_data = {
                    "id": budget.id,
                    "period_type": budget.period_type,
                    "budget_amount_usd": float(budget.budget_amount_usd),
                    "enforcement_mode": budget.enforcement_mode,
                }

                # Get current usage
                period_start, period_end = get_period_start_end(PeriodType(budget.period_type))
                usage_result = await session.execute(
                    select(BudgetUsage).where(
                        and_(
                            BudgetUsage.org_id == org_id,
                            BudgetUsage.entity_type == entity_type,
                            BudgetUsage.entity_id == entity_id,
                            BudgetUsage.period_type == budget.period_type,
                            BudgetUsage.period_start == period_start,
                        )
                    )
                )
                usage = usage_result.scalar_one_or_none()

                budget_data["current_usage"] = {
                    "total_cost_usd": float(usage.total_cost_usd) if usage else 0,
                    "total_tokens": usage.total_tokens if usage else 0,
                    "request_count": usage.request_count if usage else 0,
                    "period_start": period_start.isoformat(),
                    "period_end": period_end.isoformat(),
                }

                utilization = calculate_budget_utilization(budget.budget_amount_usd, usage.total_cost_usd if usage else Decimal("0"))
                budget_data["utilization_percent"] = round(utilization, 2)

                summary["budgets"].append(budget_data)

            return summary

    async def get_organization_budget_overview(self, org_id: str) -> dict[str, Any]:
        """Get organization-wide budget overview including all entities."""
        async with self._get_session() as session:
            overview = {
                "org_id": org_id,
                "total_budgets": 0,
                "total_spend_current_month": Decimal("0"),
                "entities": {
                    EntityType.ORGANIZATION.value: [],
                    EntityType.DEPARTMENT.value: [],
                    EntityType.TEAM.value: [],
                    EntityType.USER.value: [],
                    EntityType.SERVICE_ACCOUNT.value: [],
                },
                "alerts": [],
            }

            # Get all budgets for the organization
            budgets_result = await session.execute(select(BudgetConfig).where(BudgetConfig.org_id == org_id))
            budgets = budgets_result.scalars().all()
            overview["total_budgets"] = len(budgets)

            # Get current month usage
            period_start, _ = get_period_start_end(PeriodType.MONTHLY)
            usage_result = await session.execute(
                select(func.sum(BudgetUsage.total_cost_usd)).where(
                    and_(
                        BudgetUsage.org_id == org_id,
                        BudgetUsage.period_type == PeriodType.MONTHLY.value,
                        BudgetUsage.period_start == period_start,
                    )
                )
            )
            total_spend = usage_result.scalar_one_or_none() or Decimal("0")
            overview["total_spend_current_month"] = float(total_spend)

            # Group budgets by entity type
            for budget in budgets:
                entity_data = {
                    "entity_id": budget.entity_id,
                    "budget_amount_usd": float(budget.budget_amount_usd),
                    "period_type": budget.period_type,
                    "enforcement_mode": budget.enforcement_mode,
                }
                overview["entities"][budget.entity_type].append(entity_data)

            return overview

    async def get_budget_alerts(self, org_id: str, threshold_percent: float = 80.0) -> list[dict[str, Any]]:
        """Get budget alerts for entities approaching or exceeding their budgets."""
        async with self._get_session() as session:
            alerts = []

            # Get all budgets for the organization
            budgets_result = await session.execute(select(BudgetConfig).where(BudgetConfig.org_id == org_id))
            budgets = budgets_result.scalars().all()

            for budget in budgets:
                # Get current usage
                period_start, _ = get_period_start_end(PeriodType(budget.period_type))
                usage_result = await session.execute(
                    select(BudgetUsage).where(
                        and_(
                            BudgetUsage.org_id == org_id,
                            BudgetUsage.entity_type == budget.entity_type,
                            BudgetUsage.entity_id == budget.entity_id,
                            BudgetUsage.period_type == budget.period_type,
                            BudgetUsage.period_start == period_start,
                        )
                    )
                )
                usage = usage_result.scalar_one_or_none()

                if usage:
                    utilization = calculate_budget_utilization(budget.budget_amount_usd, usage.total_cost_usd)

                    if utilization >= threshold_percent:
                        alert_level = "critical" if utilization >= 100 else "warning"
                        alerts.append(
                            {
                                "entity_type": budget.entity_type,
                                "entity_id": budget.entity_id,
                                "period_type": budget.period_type,
                                "budget_amount_usd": float(budget.budget_amount_usd),
                                "current_spend_usd": float(usage.total_cost_usd),
                                "utilization_percent": round(utilization, 2),
                                "enforcement_mode": budget.enforcement_mode,
                                "alert_level": alert_level,
                                "period_start": period_start.isoformat(),
                            }
                        )

            return alerts
