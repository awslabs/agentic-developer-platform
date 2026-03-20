"""
Budget Helper Service for Agent Budget Management.

Issue #249: Per-Agent Budget Assignment and Usage Dashboard

Provides helper functions for creating, deleting, and validating budget configs
for agents. This module handles the Postgres side of the cross-database
consistency pattern (Postgres first, DynamoDB second, with compensating rollback).
"""

import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date
from decimal import Decimal

from sqlalchemy import and_, delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.database import get_session_factory, reset_engine
from src.shared.models.budget import BudgetConfig, BudgetUsage
from src.shared.schemas.budget import EntityType, PeriodType

logger = logging.getLogger(__name__)


class BudgetHelperService:
    """
    Helper service for agent budget operations in Postgres.

    Provides methods to create, delete, and validate budget configs,
    as well as query usage data for agents.
    """

    def __init__(self, db_session: AsyncSession | None = None):
        """
        Initialize the budget helper service.

        Args:
            db_session: Optional injected database session
        """
        self.db_session = db_session

    @asynccontextmanager
    async def _get_session(self) -> AsyncIterator[AsyncSession]:
        """Get database session as async context manager.

        For IAM auth, resets the engine before each session to ensure
        a fresh IAM token.
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

    async def create_agent_budget_config(
        self,
        agent_id: str,
        org_id: str,
        monthly_limit_usd: Decimal,
        enforcement_mode: str = "hard",
    ) -> str:
        """
        Create a budget config for an agent in Postgres.

        Args:
            agent_id: Agent UUID (will be used as entity_id)
            org_id: Organization ID for tenant isolation
            monthly_limit_usd: Monthly budget limit in USD
            enforcement_mode: 'hard' or 'soft' enforcement

        Returns:
            str: The created budget_config_id

        Raises:
            Exception: If database operation fails
        """
        budget_config_id = str(uuid.uuid4())

        async with self._get_session() as session:
            budget_config = BudgetConfig(
                id=budget_config_id,
                org_id=org_id,
                entity_type=EntityType.AGENT.value,
                entity_id=agent_id,
                period_type=PeriodType.MONTHLY.value,
                budget_amount_usd=monthly_limit_usd,
                enforcement_mode=enforcement_mode,
            )
            session.add(budget_config)
            await session.commit()

            logger.info(f"Created budget config {budget_config_id} for agent {agent_id}")
            return budget_config_id

    async def delete_budget_config(self, budget_config_id: str, org_id: str) -> bool:
        """
        Delete a budget config by ID (compensating action for rollback).

        Defense-in-depth: Also filters by org_id to prevent cross-tenant deletion.

        Args:
            budget_config_id: Budget config ID to delete
            org_id: Organization ID for tenant isolation

        Returns:
            bool: True if deleted, False if not found
        """
        async with self._get_session() as session:
            result = await session.execute(delete(BudgetConfig).where(BudgetConfig.id == budget_config_id, BudgetConfig.org_id == org_id))
            await session.commit()

            deleted = result.rowcount > 0
            if deleted:
                logger.info(f"Deleted budget config {budget_config_id}")
            else:
                logger.warning(f"Budget config {budget_config_id} not found for deletion")

            return deleted

    async def validate_budget_config_exists(self, budget_config_id: str, org_id: str) -> bool:
        """
        Validate that a budget config exists and belongs to the specified organization.

        Args:
            budget_config_id: Budget config ID to validate
            org_id: Organization ID for tenant isolation

        Returns:
            bool: True if exists and belongs to org, False otherwise
        """
        async with self._get_session() as session:
            result = await session.execute(select(BudgetConfig.id).where(BudgetConfig.id == budget_config_id, BudgetConfig.org_id == org_id))
            return result.scalar_one_or_none() is not None

    async def get_budget_config_by_agent(
        self,
        agent_id: str,
        org_id: str,
    ) -> BudgetConfig | None:
        """
        Get budget config for an agent.

        Args:
            agent_id: Agent UUID
            org_id: Organization ID

        Returns:
            BudgetConfig or None if not found
        """
        async with self._get_session() as session:
            result = await session.execute(
                select(BudgetConfig).where(
                    and_(
                        BudgetConfig.org_id == org_id,
                        BudgetConfig.entity_type == EntityType.AGENT.value,
                        BudgetConfig.entity_id == agent_id,
                    )
                )
            )
            return result.scalar_one_or_none()

    async def get_agent_usage(
        self,
        budget_config_id: str,
        period_type: str = "monthly",
        period_start: date | None = None,
    ) -> BudgetUsage | None:
        """
        Get usage for an agent's budget config.

        Args:
            budget_config_id: Budget config ID
            period_type: Period type (daily/weekly/monthly)
            period_start: Start of period (defaults to current period)

        Returns:
            BudgetUsage or None if no usage recorded
        """
        from src.budget.utils import get_period_start_end

        if period_start is None:
            period_start, _ = get_period_start_end(PeriodType(period_type))

        async with self._get_session() as session:
            # First get the budget config to get entity details
            config_result = await session.execute(select(BudgetConfig).where(BudgetConfig.id == budget_config_id))
            config = config_result.scalar_one_or_none()

            if not config:
                return None

            # Then get the usage
            result = await session.execute(
                select(BudgetUsage).where(
                    and_(
                        BudgetUsage.org_id == config.org_id,
                        BudgetUsage.entity_type == config.entity_type,
                        BudgetUsage.entity_id == config.entity_id,
                        BudgetUsage.period_type == period_type,
                        BudgetUsage.period_start == period_start,
                    )
                )
            )
            return result.scalar_one_or_none()

    async def get_budget_and_usage_by_config_id(
        self,
        budget_config_id: str,
        period_type: str = "monthly",
    ) -> tuple[BudgetConfig | None, Decimal]:
        """
        Get budget config and current usage by budget_config_id.

        Args:
            budget_config_id: Budget config ID
            period_type: Period type (daily/weekly/monthly)

        Returns:
            Tuple of (BudgetConfig, current_spend_usd)
        """
        from src.budget.utils import get_period_start_end

        period_start, _ = get_period_start_end(PeriodType(period_type))

        async with self._get_session() as session:
            # Get the budget config
            config_result = await session.execute(select(BudgetConfig).where(BudgetConfig.id == budget_config_id))
            config = config_result.scalar_one_or_none()

            if not config:
                return None, Decimal("0")

            # Get the usage
            usage_result = await session.execute(
                select(BudgetUsage).where(
                    and_(
                        BudgetUsage.org_id == config.org_id,
                        BudgetUsage.entity_type == config.entity_type,
                        BudgetUsage.entity_id == config.entity_id,
                        BudgetUsage.period_type == period_type,
                        BudgetUsage.period_start == period_start,
                    )
                )
            )
            usage = usage_result.scalar_one_or_none()

            current_spend = usage.total_cost_usd if usage else Decimal("0")
            return config, current_spend


# Module-level instance
budget_helper_service = BudgetHelperService()
