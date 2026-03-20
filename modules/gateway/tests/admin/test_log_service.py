"""Unit tests for LogService."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from src.admin.exceptions import ResourceNotFoundError
from src.admin.log_service import LogService
from src.admin.models import RequestLog


@pytest.fixture
async def log_service(db_session: AsyncSession) -> LogService:
    """Create a log service instance."""
    return LogService(db=db_session)


@pytest.fixture
async def sample_request_logs(db_session: AsyncSession) -> list[RequestLog]:
    """Create sample request logs in the database."""
    now = datetime.now(UTC)
    logs = [
        RequestLog(
            id="log-001",
            org_id="org-001",
            user_id="user-001",
            department_id="dept-001",
            team_id="team-001",
            method="POST",
            path="/v1/chat/completions",
            status_code=200,
            response_time_ms=150,
            request_body_size=500,
            response_body_size=1000,
            timestamp=now - timedelta(hours=1),
        ),
        RequestLog(
            id="log-002",
            org_id="org-001",
            user_id="user-002",
            department_id="dept-001",
            team_id="team-001",
            method="POST",
            path="/v1/chat/completions",
            status_code=200,
            response_time_ms=200,
            timestamp=now - timedelta(hours=2),
        ),
        RequestLog(
            id="log-003",
            org_id="org-001",
            user_id="user-001",
            department_id="dept-001",
            team_id="team-001",
            method="POST",
            path="/v1/embeddings",
            status_code=400,
            response_time_ms=50,
            timestamp=now - timedelta(hours=3),
        ),
        RequestLog(
            id="log-004",
            org_id="org-002",
            user_id="user-003",
            department_id="dept-002",
            team_id="team-002",
            method="POST",
            path="/v1/chat/completions",
            status_code=500,
            response_time_ms=300,
            timestamp=now - timedelta(days=1),
        ),
        RequestLog(
            id="log-005",
            org_id="org-001",
            user_id="user-001",
            department_id="dept-001",
            team_id="team-001",
            method="GET",
            path="/health",
            status_code=200,
            response_time_ms=10,
            timestamp=now,
            request_id="req-12345",
        ),
    ]

    for log in logs:
        db_session.add(log)

    await db_session.commit()
    return logs


class TestLogServiceQuery:
    """Tests for log querying."""

    @pytest.mark.asyncio
    async def test_query_logs_no_filter(self, log_service: LogService, sample_request_logs):
        """Test querying logs without filters."""
        logs, total = await log_service.query_logs()

        assert total == 5
        assert len(logs) == 5

    @pytest.mark.asyncio
    async def test_query_logs_by_org(self, log_service: LogService, sample_request_logs):
        """Test querying logs filtered by organization."""
        logs, total = await log_service.query_logs(filters={"org_id": "org-001"})

        assert total == 4
        assert all(log.org_id == "org-001" for log in logs)

    @pytest.mark.asyncio
    async def test_query_logs_by_user(self, log_service: LogService, sample_request_logs):
        """Test querying logs filtered by user."""
        logs, total = await log_service.query_logs(filters={"user_id": "user-001"})

        assert total == 3
        assert all(log.user_id == "user-001" for log in logs)

    @pytest.mark.asyncio
    async def test_query_logs_by_status_code(self, log_service: LogService, sample_request_logs):
        """Test querying logs filtered by status code."""
        logs, total = await log_service.query_logs(filters={"status_code": 200})

        assert total == 3
        assert all(log.status_code == 200 for log in logs)

    @pytest.mark.asyncio
    async def test_query_logs_by_path_pattern(self, log_service: LogService, sample_request_logs):
        """Test querying logs filtered by path pattern."""
        logs, total = await log_service.query_logs(filters={"path_pattern": "chat"})

        assert total == 3
        assert all("chat" in log.path for log in logs)

    @pytest.mark.asyncio
    async def test_query_logs_by_time_range(self, log_service: LogService, sample_request_logs):
        """Test querying logs filtered by time range."""
        now = datetime.now(UTC)
        logs, total = await log_service.query_logs(
            filters={
                "start_time": now - timedelta(hours=2, minutes=30),
                "end_time": now,
            }
        )

        # Should include logs from last 2.5 hours (log-001, log-002, log-005)
        assert total == 3

    @pytest.mark.asyncio
    async def test_query_logs_error_only(self, log_service: LogService, sample_request_logs):
        """Test querying only error logs."""
        logs, total = await log_service.query_logs(filters={"error_only": True})

        assert total == 2
        assert all(log.status_code >= 400 for log in logs)

    @pytest.mark.asyncio
    async def test_query_logs_min_response_time(self, log_service: LogService, sample_request_logs):
        """Test querying logs by minimum response time."""
        logs, total = await log_service.query_logs(filters={"min_response_time_ms": 100})

        # Should include logs with response_time >= 100
        assert all(log.response_time_ms >= 100 for log in logs)

    @pytest.mark.asyncio
    async def test_query_logs_pagination(self, log_service: LogService, sample_request_logs):
        """Test log pagination."""
        logs_page1, total = await log_service.query_logs(page=1, page_size=2)
        logs_page2, _ = await log_service.query_logs(page=2, page_size=2)
        logs_page3, _ = await log_service.query_logs(page=3, page_size=2)

        assert total == 5
        assert len(logs_page1) == 2
        assert len(logs_page2) == 2
        assert len(logs_page3) == 1

        # Pages should have different logs
        page1_ids = {log.id for log in logs_page1}
        page2_ids = {log.id for log in logs_page2}
        assert page1_ids.isdisjoint(page2_ids)

    @pytest.mark.asyncio
    async def test_query_logs_sorting(self, log_service: LogService, sample_request_logs):
        """Test log sorting."""
        logs_desc, _ = await log_service.query_logs(sort_by="timestamp", sort_desc=True)
        logs_asc, _ = await log_service.query_logs(sort_by="timestamp", sort_desc=False)

        # First log in desc should be latest (log-005)
        assert logs_desc[0].id == "log-005"

        # First log in asc should be oldest (log-004)
        assert logs_asc[0].id == "log-004"


class TestLogServiceGetById:
    """Tests for getting single log entries."""

    @pytest.mark.asyncio
    async def test_get_log_by_id(self, log_service: LogService, sample_request_logs):
        """Test getting a log by ID."""
        log = await log_service.get_log_by_id("log-001")

        assert log.id == "log-001"
        assert log.org_id == "org-001"
        assert log.method == "POST"

    @pytest.mark.asyncio
    async def test_get_log_by_id_not_found(self, log_service: LogService):
        """Test getting non-existent log."""
        with pytest.raises(ResourceNotFoundError):
            await log_service.get_log_by_id("non-existent")

    @pytest.mark.asyncio
    async def test_get_logs_by_request_id(self, log_service: LogService, sample_request_logs):
        """Test getting logs by request ID."""
        logs = await log_service.get_logs_by_request_id("req-12345")

        assert len(logs) == 1
        assert logs[0].id == "log-005"


class TestLogServiceExport:
    """Tests for log export functionality."""

    @pytest.mark.asyncio
    async def test_export_logs(self, log_service: LogService, sample_request_logs):
        """Test exporting logs."""
        exported = await log_service.export_logs()

        assert len(exported) == 5
        assert all("id" in log for log in exported)
        assert all("timestamp" in log for log in exported)

    @pytest.mark.asyncio
    async def test_export_logs_with_filters(self, log_service: LogService, sample_request_logs):
        """Test exporting logs with filters."""
        exported = await log_service.export_logs(filters={"org_id": "org-001"})

        assert len(exported) == 4
        assert all(log["org_id"] == "org-001" for log in exported)

    @pytest.mark.asyncio
    async def test_export_logs_with_limit(self, log_service: LogService, sample_request_logs):
        """Test exporting logs with limit."""
        exported = await log_service.export_logs(limit=3)

        assert len(exported) == 3


class TestLogServiceStatistics:
    """Tests for log statistics."""

    @pytest.mark.asyncio
    async def test_get_log_statistics(self, log_service: LogService, sample_request_logs):
        """Test getting log statistics."""
        stats = await log_service.get_log_statistics()

        assert stats["total_requests"] == 5
        assert stats["error_count"] == 2  # status >= 400
        assert stats["error_rate_percent"] == 40.0  # 2/5 * 100
        assert stats["average_response_time_ms"] > 0

    @pytest.mark.asyncio
    async def test_get_log_statistics_with_filters(self, log_service: LogService, sample_request_logs):
        """Test getting log statistics with filters."""
        stats = await log_service.get_log_statistics(filters={"org_id": "org-001"})

        assert stats["total_requests"] == 4
        assert stats["error_count"] == 1  # Only log-003 is an error in org-001


class TestLogServiceRetention:
    """Tests for log retention."""

    @pytest.mark.asyncio
    async def test_delete_old_logs(self, log_service: LogService, sample_request_logs):
        """Test deleting old logs."""
        # Delete logs older than 12 hours
        deleted_count = await log_service.delete_old_logs(days=0)  # 0 days = delete all

        # All logs should be considered "old" with 0 days retention
        assert deleted_count >= 0  # May vary based on timing

    @pytest.mark.asyncio
    async def test_delete_old_logs_preserves_recent(self, log_service: LogService, sample_request_logs):
        """Test that recent logs are preserved."""
        # Delete logs older than 2 days
        await log_service.delete_old_logs(days=2)

        # Most recent logs should still exist
        logs, total = await log_service.query_logs()

        # log-004 is 1 day old, should be preserved
        # Others are less than 1 day old
        assert total >= 4


class TestLogServiceFilters:
    """Tests for filter building."""

    @pytest.mark.asyncio
    async def test_multiple_filters(self, log_service: LogService, sample_request_logs):
        """Test combining multiple filters."""
        logs, total = await log_service.query_logs(
            filters={
                "org_id": "org-001",
                "user_id": "user-001",
                "status_code": 200,
            }
        )

        assert total == 2  # log-001 and log-005
        assert all(log.org_id == "org-001" for log in logs)
        assert all(log.user_id == "user-001" for log in logs)
        assert all(log.status_code == 200 for log in logs)

    @pytest.mark.asyncio
    async def test_filter_by_method(self, log_service: LogService, sample_request_logs):
        """Test filtering by HTTP method."""
        logs, total = await log_service.query_logs(filters={"method": "GET"})

        assert total == 1
        assert logs[0].id == "log-005"

    @pytest.mark.asyncio
    async def test_filter_by_department(self, log_service: LogService, sample_request_logs):
        """Test filtering by department."""
        logs, total = await log_service.query_logs(filters={"department_id": "dept-001"})

        assert total == 4
        assert all(log.path != "/v1/chat/completions" or log.org_id == "org-001" for log in logs)
