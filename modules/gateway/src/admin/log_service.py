"""Log service for querying and managing request logs."""

from typing import Any

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.admin.config import get_admin_config
from src.admin.exceptions import ResourceNotFoundError
from src.admin.models import RequestLog
from src.admin.schemas import LogEntryResponse


class LogService:
    """
    Service for querying and managing request logs.

    Provides:
    - Log querying with filtering
    - Pagination support
    - Log export functionality
    """

    def __init__(self, db: AsyncSession):
        """
        Initialize log service.

        Args:
            db: Database session
        """
        self.db = db
        self.config = get_admin_config()

    async def query_logs(
        self,
        filters: dict[str, Any] | None = None,
        page: int = 1,
        page_size: int | None = None,
        sort_by: str = "timestamp",
        sort_desc: bool = True,
    ) -> tuple[list[LogEntryResponse], int]:
        """
        Query logs with filtering and pagination.

        Args:
            filters: Dictionary of filter conditions
            page: Page number (1-indexed)
            page_size: Items per page
            sort_by: Field to sort by
            sort_desc: Sort in descending order

        Returns:
            Tuple of (list of log entries, total count)
        """
        if page_size is None:
            page_size = self.config.default_page_size

        page_size = min(page_size, self.config.max_page_size)
        offset = (page - 1) * page_size

        # Build query
        query = select(RequestLog)
        count_query = select(func.count()).select_from(RequestLog)

        # Apply filters
        conditions = self._build_filter_conditions(filters or {})
        if conditions:
            query = query.where(and_(*conditions))
            count_query = count_query.where(and_(*conditions))

        # Get total count
        total_result = await self.db.execute(count_query)
        total = total_result.scalar_one()

        # Apply sorting
        sort_column = getattr(RequestLog, sort_by, RequestLog.timestamp)
        if sort_desc:
            query = query.order_by(sort_column.desc())
        else:
            query = query.order_by(sort_column.asc())

        # Apply pagination
        query = query.offset(offset).limit(page_size)

        # Execute query
        result = await self.db.execute(query)
        logs = result.scalars().all()

        return (
            [
                LogEntryResponse(
                    id=log.id,
                    timestamp=log.timestamp,
                    org_id=log.org_id,
                    user_id=log.user_id,
                    method=log.method,
                    path=log.path,
                    status_code=log.status_code,
                    response_time_ms=log.response_time_ms,
                    request_body_size=log.request_body_size,
                    response_body_size=log.response_body_size,
                )
                for log in logs
            ],
            total,
        )

    async def get_log_by_id(self, log_id: str) -> LogEntryResponse:
        """
        Get a specific log entry by ID.

        Args:
            log_id: Log entry ID

        Returns:
            Log entry data

        Raises:
            ResourceNotFoundError: If log not found
        """
        result = await self.db.execute(select(RequestLog).where(RequestLog.id == log_id))
        log = result.scalar_one_or_none()

        if not log:
            raise ResourceNotFoundError("RequestLog", log_id)

        return LogEntryResponse(
            id=log.id,
            timestamp=log.timestamp,
            org_id=log.org_id,
            user_id=log.user_id,
            method=log.method,
            path=log.path,
            status_code=log.status_code,
            response_time_ms=log.response_time_ms,
            request_body_size=log.request_body_size,
            response_body_size=log.response_body_size,
        )

    async def get_logs_by_request_id(self, request_id: str) -> list[LogEntryResponse]:
        """
        Get log entries by request ID.

        Args:
            request_id: The request ID to search for

        Returns:
            List of matching log entries
        """
        result = await self.db.execute(select(RequestLog).where(RequestLog.request_id == request_id).order_by(RequestLog.timestamp.desc()))
        logs = result.scalars().all()

        return [
            LogEntryResponse(
                id=log.id,
                timestamp=log.timestamp,
                org_id=log.org_id,
                user_id=log.user_id,
                method=log.method,
                path=log.path,
                status_code=log.status_code,
                response_time_ms=log.response_time_ms,
                request_body_size=log.request_body_size,
                response_body_size=log.response_body_size,
            )
            for log in logs
        ]

    async def export_logs(
        self,
        filters: dict[str, Any] | None = None,
        limit: int = 10000,
    ) -> list[dict[str, Any]]:
        """
        Export logs for download.

        Args:
            filters: Filter conditions
            limit: Maximum number of logs to export

        Returns:
            List of log entries as dictionaries
        """
        logs, _ = await self.query_logs(filters, page=1, page_size=limit)

        return [
            {
                "id": log.id,
                "timestamp": log.timestamp.isoformat(),
                "org_id": log.org_id,
                "user_id": log.user_id,
                "method": log.method,
                "path": log.path,
                "status_code": log.status_code,
                "response_time_ms": log.response_time_ms,
                "request_body_size": log.request_body_size,
                "response_body_size": log.response_body_size,
            }
            for log in logs
        ]

    async def get_log_statistics(
        self,
        filters: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        Get statistics about logs.

        Args:
            filters: Filter conditions

        Returns:
            Dictionary with statistics
        """
        conditions = self._build_filter_conditions(filters or {})

        # Build base query
        base_query = select(RequestLog)
        if conditions:
            base_query = base_query.where(and_(*conditions))

        # Total count
        count_query = select(func.count()).select_from(base_query.subquery())
        count_result = await self.db.execute(count_query)
        total_count = count_result.scalar_one()

        # Average response time
        avg_result = await self.db.execute(
            select(func.avg(RequestLog.response_time_ms)).where(and_(*conditions)) if conditions else select(func.avg(RequestLog.response_time_ms))
        )
        avg_response_time = avg_result.scalar_one() or 0

        # Error rate (status >= 400)
        error_conditions = conditions + [RequestLog.status_code >= 400] if conditions else [RequestLog.status_code >= 400]
        error_query = select(func.count()).select_from(RequestLog).where(and_(*error_conditions))
        error_result = await self.db.execute(error_query)
        error_count = error_result.scalar_one()

        error_rate = (error_count / total_count * 100) if total_count > 0 else 0

        return {
            "total_requests": total_count,
            "average_response_time_ms": float(avg_response_time),
            "error_count": error_count,
            "error_rate_percent": error_rate,
        }

    async def delete_old_logs(self, days: int | None = None) -> int:
        """
        Delete logs older than specified days.

        Args:
            days: Number of days to retain (uses config default if not specified)

        Returns:
            Number of deleted logs
        """
        if days is None:
            days = self.config.log_retention_days

        from datetime import timedelta

        from src.shared.models.base import utcnow

        cutoff_date = utcnow() - timedelta(days=days)

        # Delete old logs
        result = await self.db.execute(select(func.count()).select_from(RequestLog).where(RequestLog.timestamp < cutoff_date))
        count = result.scalar_one()

        if count > 0:
            from sqlalchemy import delete

            await self.db.execute(delete(RequestLog).where(RequestLog.timestamp < cutoff_date))
            await self.db.commit()

        return count

    def _build_filter_conditions(self, filters: dict[str, Any]) -> list:
        """
        Build SQLAlchemy filter conditions from a filter dictionary.

        Args:
            filters: Dictionary of filter key-values

        Returns:
            List of SQLAlchemy filter conditions
        """
        conditions = []

        if "start_time" in filters:
            conditions.append(RequestLog.timestamp >= filters["start_time"])

        if "end_time" in filters:
            conditions.append(RequestLog.timestamp <= filters["end_time"])

        if "org_id" in filters:
            conditions.append(RequestLog.org_id == filters["org_id"])

        if "user_id" in filters:
            conditions.append(RequestLog.user_id == filters["user_id"])

        if "status_code" in filters:
            conditions.append(RequestLog.status_code == filters["status_code"])

        if "path_pattern" in filters:
            # Use LIKE for path pattern matching
            pattern = filters["path_pattern"]
            if "%" not in pattern:
                pattern = f"%{pattern}%"
            conditions.append(RequestLog.path.like(pattern))

        if "min_response_time_ms" in filters:
            conditions.append(RequestLog.response_time_ms >= filters["min_response_time_ms"])

        if "method" in filters:
            conditions.append(RequestLog.method == filters["method"])

        if "department_id" in filters:
            conditions.append(RequestLog.department_id == filters["department_id"])

        if "request_id" in filters:
            conditions.append(RequestLog.request_id == filters["request_id"])

        if "status_codes" in filters:
            # Multiple status codes (OR)
            conditions.append(RequestLog.status_code.in_(filters["status_codes"]))

        if "error_only" in filters and filters["error_only"]:
            conditions.append(RequestLog.status_code >= 400)

        return conditions
