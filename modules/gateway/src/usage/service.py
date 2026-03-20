"""Usage service implementing IUsageService interface."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import and_, distinct, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.interfaces.usage import IUsageService
from src.shared.models.usage import UsageLog
from src.shared.schemas.auth import TokenContext
from src.usage.config import AggregationInterval, get_usage_config
from src.usage.schemas import (
    UsageByModelResponse,
    UsageByOrganizationResponse,
    UsageTimelineEntry,
    UsageTimelineResponse,
)


class UsageService(IUsageService):
    """
    Service for recording and querying usage data.

    Implements the IUsageService interface and provides:
    - Request logging
    - Usage aggregation queries
    - Time-series data generation
    """

    def __init__(self, db: AsyncSession):
        """
        Initialize usage service.

        Args:
            db: Database session
        """
        self.db = db
        self.config = get_usage_config()

    async def log_request(
        self,
        context: TokenContext,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cost_usd: float,
        latency_ms: int,
        status_code: int,
        request_id: str | None = None,
        bedrock_account_id: str | None = None,
    ) -> None:
        """
        Log a Bedrock API request.

        Args:
            context: Token context with user/org information
            model: Model name/ID
            input_tokens: Number of input tokens
            output_tokens: Number of output tokens
            cost_usd: Cost in USD
            latency_ms: Request latency in milliseconds
            status_code: HTTP status code
            request_id: Optional request ID
            bedrock_account_id: Optional Bedrock account ID used
        """
        log_entry = UsageLog(
            org_id=context.org_id,
            department_id=context.department_id,
            team_id=context.team_id,
            user_id=context.user_id,
            account_type=context.account_type,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=Decimal(str(cost_usd)),
            latency_ms=latency_ms,
            status_code=status_code,
            request_id=request_id,
            bedrock_account_id=bedrock_account_id,
        )

        self.db.add(log_entry)
        await self.db.commit()

    async def query_logs(
        self,
        org_id: str,
        filters: dict[str, Any] | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """
        Query usage logs with filtering.

        Args:
            org_id: Organization ID to filter by
            filters: Additional filter conditions
            limit: Maximum number of results
            offset: Offset for pagination

        Returns:
            List of usage log entries as dictionaries
        """
        filters = filters or {}

        query = select(UsageLog).where(UsageLog.org_id == org_id)

        # Apply filters
        conditions = self._build_filter_conditions(org_id, filters)
        if conditions:
            query = query.where(and_(*conditions))

        # Apply pagination and ordering
        query = query.order_by(UsageLog.timestamp.desc()).offset(offset).limit(limit)

        result = await self.db.execute(query)
        logs = result.scalars().all()

        return [
            {
                "id": log.id,
                "timestamp": log.timestamp.isoformat(),
                "org_id": log.org_id,
                "department_id": log.department_id,
                "team_id": log.team_id,
                "user_id": log.user_id,
                "account_type": log.account_type,
                "model": log.model,
                "input_tokens": log.input_tokens,
                "output_tokens": log.output_tokens,
                "cost_usd": float(log.cost_usd),
                "latency_ms": log.latency_ms,
                "status_code": log.status_code,
                "request_id": log.request_id,
                "bedrock_account_id": log.bedrock_account_id,
            }
            for log in logs
        ]

    async def get_usage_summary(
        self,
        org_id: str,
        filters: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        Get usage summary for an organization.

        Args:
            org_id: Organization ID
            filters: Additional filter conditions

        Returns:
            Dictionary with usage summary metrics
        """
        filters = filters or {}

        # Set default time range to last 30 days if not specified
        start_date = filters.get("start_date", datetime.now(UTC) - timedelta(days=30))
        end_date = filters.get("end_date", datetime.now(UTC))

        conditions = [
            UsageLog.org_id == org_id,
            UsageLog.timestamp >= start_date,
            UsageLog.timestamp <= end_date,
        ]

        # Add additional filters
        additional_conditions = self._build_filter_conditions(org_id, filters)
        conditions.extend(additional_conditions)

        # Aggregate query
        query = select(
            func.count(UsageLog.id).label("total_requests"),
            func.sum(UsageLog.input_tokens).label("total_input_tokens"),
            func.sum(UsageLog.output_tokens).label("total_output_tokens"),
            func.sum(UsageLog.cost_usd).label("total_cost_usd"),
            func.avg(UsageLog.latency_ms).label("average_latency_ms"),
            func.count(distinct(UsageLog.user_id)).label("unique_users"),
            func.count(distinct(UsageLog.model)).label("unique_models"),
        ).where(and_(*conditions))

        result = await self.db.execute(query)
        row = result.one()

        # Count errors (status >= 400)
        error_conditions = conditions + [UsageLog.status_code >= 400]
        error_query = select(func.count(UsageLog.id)).where(and_(*error_conditions))
        error_result = await self.db.execute(error_query)
        error_count = error_result.scalar_one()

        # Count successful requests
        success_conditions = conditions + [UsageLog.status_code < 400]
        success_query = select(func.count(UsageLog.id)).where(and_(*success_conditions))
        success_result = await self.db.execute(success_query)
        success_count = success_result.scalar_one()

        total_requests = row.total_requests or 0
        error_rate = (error_count / total_requests * 100) if total_requests > 0 else 0

        return {
            "org_id": org_id,
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "total_requests": total_requests,
            "successful_requests": success_count,
            "failed_requests": error_count,
            "total_input_tokens": row.total_input_tokens or 0,
            "total_output_tokens": row.total_output_tokens or 0,
            "total_tokens": (row.total_input_tokens or 0) + (row.total_output_tokens or 0),
            "total_cost_usd": float(row.total_cost_usd or 0),
            "average_latency_ms": float(row.average_latency_ms or 0),
            "error_rate_percent": error_rate,
            "unique_users": row.unique_users or 0,
            "unique_models": row.unique_models or 0,
        }

    async def get_usage_by_organization(
        self,
        org_ids: list[str] | None = None,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
    ) -> list[UsageByOrganizationResponse]:
        """
        Get usage aggregated by organization.

        Args:
            org_ids: Optional list of org IDs to filter by
            start_date: Start of time range
            end_date: End of time range

        Returns:
            List of usage data per organization
        """
        if start_date is None:
            start_date = datetime.now(UTC) - timedelta(days=30)
        if end_date is None:
            end_date = datetime.now(UTC)

        conditions = [
            UsageLog.timestamp >= start_date,
            UsageLog.timestamp <= end_date,
        ]

        if org_ids:
            conditions.append(UsageLog.org_id.in_(org_ids))

        query = (
            select(
                UsageLog.org_id,
                func.count(UsageLog.id).label("total_requests"),
                func.sum(UsageLog.input_tokens + UsageLog.output_tokens).label("total_tokens"),
                func.sum(UsageLog.cost_usd).label("total_cost_usd"),
                func.avg(UsageLog.latency_ms).label("average_latency_ms"),
            )
            .where(and_(*conditions))
            .group_by(UsageLog.org_id)
        )

        result = await self.db.execute(query)
        rows = result.all()

        responses = []
        for row in rows:
            # Calculate error rate
            error_conditions = conditions + [
                UsageLog.org_id == row.org_id,
                UsageLog.status_code >= 400,
            ]
            error_query = select(func.count(UsageLog.id)).where(and_(*error_conditions))
            error_result = await self.db.execute(error_query)
            error_count = error_result.scalar_one()
            error_rate = (error_count / row.total_requests * 100) if row.total_requests > 0 else 0

            responses.append(
                UsageByOrganizationResponse(
                    org_id=row.org_id,
                    org_name=None,  # Would need to join with organizations table
                    total_requests=row.total_requests,
                    total_tokens=row.total_tokens or 0,
                    total_cost_usd=Decimal(str(row.total_cost_usd or 0)),
                    average_latency_ms=float(row.average_latency_ms or 0),
                    error_rate_percent=error_rate,
                    period_start=start_date,
                    period_end=end_date,
                )
            )

        return responses

    async def get_usage_by_model(
        self,
        org_id: str | None = None,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
    ) -> list[UsageByModelResponse]:
        """
        Get usage aggregated by model.

        Args:
            org_id: Optional organization ID to filter by
            start_date: Start of time range
            end_date: End of time range

        Returns:
            List of usage data per model
        """
        if start_date is None:
            start_date = datetime.now(UTC) - timedelta(days=30)
        if end_date is None:
            end_date = datetime.now(UTC)

        conditions = [
            UsageLog.timestamp >= start_date,
            UsageLog.timestamp <= end_date,
        ]

        if org_id:
            conditions.append(UsageLog.org_id == org_id)

        query = (
            select(
                UsageLog.model,
                func.count(UsageLog.id).label("total_requests"),
                func.sum(UsageLog.input_tokens).label("total_input_tokens"),
                func.sum(UsageLog.output_tokens).label("total_output_tokens"),
                func.sum(UsageLog.cost_usd).label("total_cost_usd"),
                func.avg(UsageLog.latency_ms).label("average_latency_ms"),
            )
            .where(and_(*conditions))
            .group_by(UsageLog.model)
            .order_by(func.count(UsageLog.id).desc())
        )

        result = await self.db.execute(query)
        rows = result.all()

        responses = []
        for row in rows:
            # Calculate error rate
            error_conditions = conditions + [
                UsageLog.model == row.model,
                UsageLog.status_code >= 400,
            ]
            error_query = select(func.count(UsageLog.id)).where(and_(*error_conditions))
            error_result = await self.db.execute(error_query)
            error_count = error_result.scalar_one()
            error_rate = (error_count / row.total_requests * 100) if row.total_requests > 0 else 0

            responses.append(
                UsageByModelResponse(
                    model=row.model,
                    total_requests=row.total_requests,
                    total_input_tokens=row.total_input_tokens or 0,
                    total_output_tokens=row.total_output_tokens or 0,
                    total_tokens=(row.total_input_tokens or 0) + (row.total_output_tokens or 0),
                    total_cost_usd=Decimal(str(row.total_cost_usd or 0)),
                    average_latency_ms=float(row.average_latency_ms or 0),
                    error_rate_percent=error_rate,
                )
            )

        return responses

    async def get_usage_timeline(
        self,
        org_id: str | None = None,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
        interval: AggregationInterval = AggregationInterval.DAILY,
    ) -> UsageTimelineResponse:
        """
        Get usage data as a time series.

        Args:
            org_id: Optional organization ID to filter by
            start_date: Start of time range
            end_date: End of time range
            interval: Aggregation interval

        Returns:
            Usage timeline data
        """
        if start_date is None:
            start_date = datetime.now(UTC) - timedelta(days=30)
        if end_date is None:
            end_date = datetime.now(UTC)

        conditions = [
            UsageLog.timestamp >= start_date,
            UsageLog.timestamp <= end_date,
        ]

        if org_id:
            conditions.append(UsageLog.org_id == org_id)

        # Determine truncation based on interval
        if interval == AggregationInterval.HOURLY:
            trunc_func = func.date_trunc("hour", UsageLog.timestamp)
        elif interval == AggregationInterval.DAILY:
            trunc_func = func.date_trunc("day", UsageLog.timestamp)
        elif interval == AggregationInterval.WEEKLY:
            trunc_func = func.date_trunc("week", UsageLog.timestamp)
        else:  # MONTHLY
            trunc_func = func.date_trunc("month", UsageLog.timestamp)

        query = (
            select(
                trunc_func.label("period"),
                func.count(UsageLog.id).label("total_requests"),
                func.sum(UsageLog.input_tokens + UsageLog.output_tokens).label("total_tokens"),
                func.sum(UsageLog.cost_usd).label("total_cost_usd"),
                func.avg(UsageLog.latency_ms).label("average_latency_ms"),
                func.count(UsageLog.id).filter(UsageLog.status_code >= 400).label("error_count"),
            )
            .where(and_(*conditions))
            .group_by(trunc_func)
            .order_by(trunc_func)
        )

        result = await self.db.execute(query)
        rows = result.all()

        data = [
            UsageTimelineEntry(
                timestamp=row.period,
                interval=interval,
                total_requests=row.total_requests,
                total_tokens=row.total_tokens or 0,
                total_cost_usd=Decimal(str(row.total_cost_usd or 0)),
                average_latency_ms=float(row.average_latency_ms or 0),
                error_count=row.error_count or 0,
            )
            for row in rows
        ]

        return UsageTimelineResponse(
            org_id=org_id,
            start_date=start_date,
            end_date=end_date,
            interval=interval,
            data=data,
        )

    async def get_usage_by_user(
        self,
        org_id: str,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """
        Get usage aggregated by user.

        Args:
            org_id: Organization ID
            start_date: Start of time range
            end_date: End of time range
            limit: Maximum number of users to return

        Returns:
            List of usage data per user
        """
        if start_date is None:
            start_date = datetime.now(UTC) - timedelta(days=30)
        if end_date is None:
            end_date = datetime.now(UTC)

        query = (
            select(
                UsageLog.user_id,
                UsageLog.account_type,
                func.count(UsageLog.id).label("total_requests"),
                func.sum(UsageLog.input_tokens + UsageLog.output_tokens).label("total_tokens"),
                func.sum(UsageLog.cost_usd).label("total_cost_usd"),
                func.max(UsageLog.timestamp).label("last_request_at"),
            )
            .where(
                and_(
                    UsageLog.org_id == org_id,
                    UsageLog.timestamp >= start_date,
                    UsageLog.timestamp <= end_date,
                )
            )
            .group_by(UsageLog.user_id, UsageLog.account_type)
            .order_by(func.sum(UsageLog.cost_usd).desc())
            .limit(limit)
        )

        result = await self.db.execute(query)
        rows = result.all()

        return [
            {
                "user_id": row.user_id,
                "account_type": row.account_type,
                "total_requests": row.total_requests,
                "total_tokens": row.total_tokens or 0,
                "total_cost_usd": float(row.total_cost_usd or 0),
                "last_request_at": row.last_request_at.isoformat() if row.last_request_at else None,
            }
            for row in rows
        ]

    async def get_usage_by_department(
        self,
        org_id: str,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """
        Get usage aggregated by department.

        Args:
            org_id: Organization ID
            start_date: Start of time range
            end_date: End of time range

        Returns:
            List of usage data per department
        """
        if start_date is None:
            start_date = datetime.now(UTC) - timedelta(days=30)
        if end_date is None:
            end_date = datetime.now(UTC)

        query = (
            select(
                UsageLog.department_id,
                func.count(UsageLog.id).label("total_requests"),
                func.sum(UsageLog.input_tokens + UsageLog.output_tokens).label("total_tokens"),
                func.sum(UsageLog.cost_usd).label("total_cost_usd"),
                func.count(distinct(UsageLog.user_id)).label("unique_users"),
            )
            .where(
                and_(
                    UsageLog.org_id == org_id,
                    UsageLog.timestamp >= start_date,
                    UsageLog.timestamp <= end_date,
                )
            )
            .group_by(UsageLog.department_id)
            .order_by(func.sum(UsageLog.cost_usd).desc())
        )

        result = await self.db.execute(query)
        rows = result.all()

        return [
            {
                "department_id": row.department_id,
                "total_requests": row.total_requests,
                "total_tokens": row.total_tokens or 0,
                "total_cost_usd": float(row.total_cost_usd or 0),
                "unique_users": row.unique_users,
            }
            for row in rows
        ]

    def _build_filter_conditions(self, org_id: str, filters: dict[str, Any]) -> list:
        """
        Build SQLAlchemy filter conditions.

        Args:
            org_id: Organization ID
            filters: Filter dictionary

        Returns:
            List of filter conditions
        """
        conditions = []

        if "department_id" in filters:
            conditions.append(UsageLog.department_id == filters["department_id"])

        if "team_id" in filters:
            conditions.append(UsageLog.team_id == filters["team_id"])

        if "user_id" in filters:
            conditions.append(UsageLog.user_id == filters["user_id"])

        if "model" in filters:
            conditions.append(UsageLog.model == filters["model"])

        if "status_code" in filters:
            conditions.append(UsageLog.status_code == filters["status_code"])

        if "min_latency_ms" in filters:
            conditions.append(UsageLog.latency_ms >= filters["min_latency_ms"])

        if "max_latency_ms" in filters:
            conditions.append(UsageLog.latency_ms <= filters["max_latency_ms"])

        if "account_type" in filters:
            conditions.append(UsageLog.account_type == filters["account_type"])

        if "bedrock_account_id" in filters:
            conditions.append(UsageLog.bedrock_account_id == filters["bedrock_account_id"])

        return conditions
