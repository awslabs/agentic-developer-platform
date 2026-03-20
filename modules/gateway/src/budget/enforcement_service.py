"""
Budget Enforcement Service.

This module provides cascading budget enforcement logic for the proxy path.
It traverses the entity hierarchy (user → team → department → org) and
checks budget constraints at each level.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from decimal import Decimal
from typing import Any

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.database import get_session_factory, reset_engine
from src.shared.logging import get_logger
from src.shared.models.budget import BudgetConfig, BudgetUsage
from src.shared.schemas.auth import TokenContext
from src.shared.schemas.budget import (
    EnforcementMode,
    EnforcementResult,
    EntityType,
    PeriodType,
)

from .config import budget_config
from .pricing import PricingService, pricing_service
from .utils import (
    calculate_budget_utilization,
    get_period_start_end,
)

logger = get_logger(__name__)


class BudgetEnforcementService:
    """
    Service for cascading budget enforcement.

    Provides:
    - Hierarchical budget checking (user → team → dept → org)
    - Pre-request cost estimation
    - Post-request usage recording
    - Soft/hard enforcement mode support
    """

    def __init__(
        self,
        db_session: AsyncSession | None = None,
        pricing: PricingService | None = None,
    ):
        """
        Initialize the budget enforcement service.

        Args:
            db_session: Optional injected database session
            pricing: Optional custom pricing service (for testing)
        """
        self.db_session = db_session
        self._pricing = pricing or pricing_service

    @asynccontextmanager
    async def _get_session(self) -> AsyncIterator[AsyncSession]:
        """Get database session as async context manager.

        For IAM auth, resets the engine before each session to ensure
        a fresh IAM token — same pattern as get_db() in the main app.
        Without this, pooled connections use stale tokens and fail with
        'PAM authentication failed'.
        """
        if self.db_session:
            yield self.db_session
        else:
            from src.shared.config import get_settings

            settings = get_settings()
            if settings.rds_iam_auth and settings.rds_host:
                reset_engine()
            factory = get_session_factory()
            async with factory() as session:
                yield session

    def _get_entity_hierarchy(self, context: TokenContext) -> list[tuple[EntityType, str]]:
        """
        Get entity hierarchy for budget checking.

        Returns entities in order from most specific to most general:
        user/service_account → team → department → organization

        Args:
            context: Token context with user hierarchy info

        Returns:
            List of (EntityType, entity_id) tuples
        """
        entities = []

        # User or service account level
        if context.account_type == "service":
            entities.append((EntityType.SERVICE_ACCOUNT, context.user_id))
        else:
            entities.append((EntityType.USER, context.user_id))

        # Team level
        if context.team_id:
            entities.append((EntityType.TEAM, context.team_id))

        # Department level
        if context.department_id:
            entities.append((EntityType.DEPARTMENT, context.department_id))

        # Organization level
        if context.org_id:
            entities.append((EntityType.ORGANIZATION, context.org_id))

        return entities

    async def check_budget_hierarchy(
        self,
        context: TokenContext,
        estimated_cost: Decimal,
    ) -> EnforcementResult:
        """
        Check budget constraints across the entire hierarchy.

        Traverses user → team → department → org, checking each level.
        Returns immediately if a hard limit is exceeded.
        Accumulates warnings for soft limit breaches.

        Args:
            context: Token context with user hierarchy info
            estimated_cost: Estimated cost for this request

        Returns:
            EnforcementResult indicating if request is allowed
        """
        if not budget_config.budget_check_enabled:
            return EnforcementResult(allowed=True)

        try:
            async with self._get_session() as session:
                entities = self._get_entity_hierarchy(context)
                all_warnings = []

                # Check each entity in the hierarchy
                for entity_type, entity_id in entities:
                    # Check all period types (daily, weekly, monthly)
                    for period_type in [
                        PeriodType.DAILY,
                        PeriodType.WEEKLY,
                        PeriodType.MONTHLY,
                    ]:
                        result = await self._check_entity_budget(
                            session,
                            entity_type,
                            entity_id,
                            period_type,
                            estimated_cost,
                            context.org_id,
                        )

                        if not result.allowed:
                            # Hard limit exceeded - block immediately
                            return result

                        # Accumulate warnings from soft limits
                        if result.warnings:
                            all_warnings.extend(result.warnings)

                # All checks passed
                return EnforcementResult(allowed=True, warnings=all_warnings)

        except Exception as e:
            # Check fail mode from config (default: closed)
            fail_mode = budget_config.budget_fail_mode.lower()
            if fail_mode == "open":
                # Fail open - log and allow request
                logger.error(f"Budget check failed with error (failing open): {e}", exc_info=True)
                return EnforcementResult(
                    allowed=True,
                    warnings=[f"Budget check failed: {str(e)}"],
                )
            else:
                # Fail closed (default) - log and block request
                logger.error(f"Budget check failed with error (failing closed): {e}", exc_info=True)
                return EnforcementResult(
                    allowed=False,
                    blocked_reason=f"Budget check failed: {str(e)}",
                )

    async def _check_entity_budget(
        self,
        session: AsyncSession,
        entity_type: EntityType,
        entity_id: str,
        period_type: PeriodType,
        estimated_cost: Decimal,
        org_id: str,
    ) -> EnforcementResult:
        """
        Check budget for a specific entity and period.

        Args:
            session: Database session
            entity_type: Type of entity (user, team, dept, org)
            entity_id: Entity identifier
            period_type: Budget period type
            estimated_cost: Estimated cost for request
            org_id: Organization ID for tenant isolation

        Returns:
            EnforcementResult for this entity/period
        """
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
            # No budget configured for this entity/period - allow
            return EnforcementResult(allowed=True)

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
        projected_spend = current_spend + estimated_cost

        # Check if budget would be exceeded
        if projected_spend > budget.budget_amount_usd:
            enforcement_mode = EnforcementMode(budget.enforcement_mode)

            if enforcement_mode == EnforcementMode.HARD:
                # Hard limit - block request
                logger.warning(
                    f"Budget exceeded (hard limit): {entity_type.value} {entity_id} "
                    f"- {period_type.value} budget ${budget.budget_amount_usd}, "
                    f"current ${current_spend}, projected ${projected_spend}"
                )
                return EnforcementResult(
                    allowed=False,
                    blocked_reason=f"Budget exceeded for {entity_type.value} {entity_id}",
                    exceeded_entity_type=entity_type,
                    exceeded_entity_id=entity_id,
                    budget_amount_usd=budget.budget_amount_usd,
                    current_spend_usd=current_spend,
                    enforcement_mode=enforcement_mode,
                )
            else:
                # Soft limit - warn and continue
                logger.info(
                    f"Budget exceeded (soft limit): {entity_type.value} {entity_id} "
                    f"- {period_type.value} budget ${budget.budget_amount_usd}, "
                    f"current ${current_spend}, projected ${projected_spend}"
                )
                return EnforcementResult(
                    allowed=True,
                    warnings=[
                        f"Budget exceeded for {entity_type.value} {entity_id} "
                        f"({period_type.value}): ${projected_spend:.2f} / ${budget.budget_amount_usd:.2f}"
                    ],
                )

        # Check for warning threshold
        utilization = calculate_budget_utilization(budget.budget_amount_usd, projected_spend)
        warnings = []

        if utilization >= budget_config.budget_critical_threshold_percent:
            warnings.append(f"{entity_type.value} {entity_id} {period_type.value} budget at {utilization:.1f}% (critical)")
        elif utilization >= budget_config.budget_warning_threshold_percent:
            warnings.append(f"{entity_type.value} {entity_id} {period_type.value} budget at {utilization:.1f}%")

        return EnforcementResult(allowed=True, warnings=warnings)

    async def record_usage(
        self,
        context: TokenContext,
        input_tokens: int,
        output_tokens: int,
        model_id: str,
    ) -> None:
        """
        Record usage to all hierarchy levels after request completion.

        Args:
            context: Token context with user hierarchy info
            input_tokens: Number of input tokens used
            output_tokens: Number of output tokens generated
            model_id: Model ID used for the request
        """
        if not budget_config.cost_calculation_enabled:
            return

        try:
            # Calculate actual cost
            cost = self._pricing.calculate_cost(model_id, input_tokens, output_tokens)

            logger.debug(f"Recording usage: user={context.user_id}, tokens_in={input_tokens}, tokens_out={output_tokens}, cost=${cost:.6f}")

            async with self._get_session() as session:
                entities = self._get_entity_hierarchy(context)

                # Record to each entity in the hierarchy
                for entity_type, entity_id in entities:
                    await self._record_entity_usage(
                        session,
                        entity_type,
                        entity_id,
                        context.org_id,
                        input_tokens,
                        output_tokens,
                        cost,
                    )

                await session.commit()

        except Exception as e:
            logger.error(f"Failed to record usage: {e}", exc_info=True)
            # Don't raise - usage recording failure shouldn't block the response

    async def _record_entity_usage(
        self,
        session: AsyncSession,
        entity_type: EntityType,
        entity_id: str,
        org_id: str,
        input_tokens: int,
        output_tokens: int,
        cost: Decimal,
    ) -> None:
        """
        Record usage for a single entity across all period types.

        Args:
            session: Database session
            entity_type: Type of entity
            entity_id: Entity identifier
            org_id: Organization ID
            input_tokens: Input tokens used
            output_tokens: Output tokens generated
            cost: Total cost for this request
        """
        total_tokens = input_tokens + output_tokens

        for period_type in [PeriodType.DAILY, PeriodType.WEEKLY, PeriodType.MONTHLY]:
            period_start, _ = get_period_start_end(period_type)

            # Get or create usage record
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

            if not usage:
                usage = BudgetUsage(
                    org_id=org_id,
                    entity_type=entity_type.value,
                    entity_id=entity_id,
                    period_start=period_start,
                    period_type=period_type.value,
                    total_cost_usd=Decimal("0"),
                    total_tokens=0,
                    request_count=0,
                )
                session.add(usage)

            # Update usage
            usage.total_cost_usd += cost
            usage.total_tokens += total_tokens
            usage.request_count += 1

    async def get_budget_status_for_headers(self, context: TokenContext) -> dict[str, Any]:
        """
        Get budget status info for response headers.

        Returns the most restrictive (lowest remaining) budget across the hierarchy.

        Args:
            context: Token context with user hierarchy info

        Returns:
            Dict with budget_limit, budget_remaining, budget_reset
        """
        try:
            async with self._get_session() as session:
                entities = self._get_entity_hierarchy(context)

                lowest_remaining = None
                corresponding_limit = None
                corresponding_reset = None

                for entity_type, entity_id in entities:
                    # Check monthly budget (most common)
                    period_type = PeriodType.MONTHLY
                    period_start, period_end = get_period_start_end(period_type)

                    # Get budget config
                    budget_result = await session.execute(
                        select(BudgetConfig).where(
                            and_(
                                BudgetConfig.org_id == context.org_id,
                                BudgetConfig.entity_type == entity_type.value,
                                BudgetConfig.entity_id == entity_id,
                                BudgetConfig.period_type == period_type.value,
                            )
                        )
                    )
                    budget = budget_result.scalar_one_or_none()

                    if not budget:
                        continue

                    # Get current usage
                    usage_result = await session.execute(
                        select(BudgetUsage).where(
                            and_(
                                BudgetUsage.org_id == context.org_id,
                                BudgetUsage.entity_type == entity_type.value,
                                BudgetUsage.entity_id == entity_id,
                                BudgetUsage.period_type == period_type.value,
                                BudgetUsage.period_start == period_start,
                            )
                        )
                    )
                    usage = usage_result.scalar_one_or_none()

                    current_spend = usage.total_cost_usd if usage else Decimal("0")
                    remaining = budget.budget_amount_usd - current_spend

                    # Track the most restrictive (lowest remaining)
                    if lowest_remaining is None or remaining < lowest_remaining:
                        lowest_remaining = remaining
                        corresponding_limit = budget.budget_amount_usd
                        corresponding_reset = period_end

                if lowest_remaining is not None:
                    return {
                        "budget_limit": float(corresponding_limit),
                        "budget_remaining": float(max(Decimal("0"), lowest_remaining)),
                        "budget_reset": corresponding_reset.isoformat(),
                    }

                return {}

        except Exception as e:
            logger.error(f"Failed to get budget status for headers: {e}")
            return {}

    def estimate_request_cost(self, model_id: str, request_body: dict[str, Any]) -> Decimal:
        """
        Estimate the cost of a request before execution.

        Args:
            model_id: Model ID for the request
            request_body: Request body dictionary

        Returns:
            Estimated cost in USD
        """
        return self._pricing.estimate_request_cost(model_id, request_body)

    async def check_agent_budget(
        self,
        budget_config_id: str,
        estimated_cost: Decimal,
    ) -> EnforcementResult:
        """
        Check agent-level budget by budget_config_id.

        Issue #249: Agent budgets are checked directly by budget_config_id
        (passed from Lambda authorizer via X-Agent-BudgetConfigId header).
        This allows agent-level enforcement BEFORE the team/org hierarchy.

        Args:
            budget_config_id: Budget config ID from agent registry
            estimated_cost: Estimated cost for this request

        Returns:
            EnforcementResult indicating if request is allowed
        """
        if not budget_config.budget_check_enabled:
            return EnforcementResult(allowed=True)

        try:
            async with self._get_session() as session:
                # Get budget config directly by ID
                budget_result = await session.execute(select(BudgetConfig).where(BudgetConfig.id == budget_config_id))
                budget = budget_result.scalar_one_or_none()

                if not budget:
                    # No budget config found - allow request
                    logger.warning(f"Budget config not found: {budget_config_id}")
                    return EnforcementResult(allowed=True)

                # Get current usage for the budget period
                period_type = PeriodType(budget.period_type)
                period_start, _ = get_period_start_end(period_type)

                usage_result = await session.execute(
                    select(BudgetUsage).where(
                        and_(
                            BudgetUsage.org_id == budget.org_id,
                            BudgetUsage.entity_type == budget.entity_type,
                            BudgetUsage.entity_id == budget.entity_id,
                            BudgetUsage.period_type == budget.period_type,
                            BudgetUsage.period_start == period_start,
                        )
                    )
                )
                usage = usage_result.scalar_one_or_none()

                current_spend = usage.total_cost_usd if usage else Decimal("0")
                projected_spend = current_spend + estimated_cost

                # Check if budget would be exceeded
                if projected_spend > budget.budget_amount_usd:
                    enforcement_mode = EnforcementMode(budget.enforcement_mode)

                    if enforcement_mode == EnforcementMode.HARD:
                        logger.warning(
                            f"Agent budget exceeded (hard limit): config_id={budget_config_id}, "
                            f"budget=${budget.budget_amount_usd}, current=${current_spend}, projected=${projected_spend}"
                        )
                        return EnforcementResult(
                            allowed=False,
                            blocked_reason=f"Agent budget exceeded (config_id={budget_config_id})",
                            exceeded_entity_type=EntityType(budget.entity_type),
                            exceeded_entity_id=budget.entity_id,
                            budget_amount_usd=budget.budget_amount_usd,
                            current_spend_usd=current_spend,
                            enforcement_mode=enforcement_mode,
                        )
                    else:
                        logger.info(
                            f"Agent budget exceeded (soft limit): config_id={budget_config_id}, "
                            f"budget=${budget.budget_amount_usd}, current=${current_spend}, projected=${projected_spend}"
                        )
                        return EnforcementResult(
                            allowed=True,
                            warnings=[
                                f"Agent budget exceeded (config_id={budget_config_id}): ${projected_spend:.2f} / ${budget.budget_amount_usd:.2f}"
                            ],
                        )

                # Check warning threshold
                utilization = calculate_budget_utilization(budget.budget_amount_usd, projected_spend)
                warnings = []

                if utilization >= budget_config.budget_critical_threshold_percent:
                    warnings.append(f"Agent budget at {utilization:.1f}% (critical)")
                elif utilization >= budget_config.budget_warning_threshold_percent:
                    warnings.append(f"Agent budget at {utilization:.1f}%")

                return EnforcementResult(allowed=True, warnings=warnings)

        except Exception as e:
            fail_mode = budget_config.budget_fail_mode.lower()
            if fail_mode == "open":
                logger.error(f"Agent budget check failed (failing open): {e}", exc_info=True)
                return EnforcementResult(
                    allowed=True,
                    warnings=[f"Agent budget check failed: {str(e)}"],
                )
            else:
                logger.error(f"Agent budget check failed (failing closed): {e}", exc_info=True)
                return EnforcementResult(
                    allowed=False,
                    blocked_reason=f"Agent budget check failed: {str(e)}",
                )


# Global enforcement service instance
budget_enforcement_service = BudgetEnforcementService()
