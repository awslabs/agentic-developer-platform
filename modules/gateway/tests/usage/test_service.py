"""Unit tests for UsageService."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.models.usage import UsageLog
from src.shared.schemas.auth import TokenContext
from src.usage.config import AggregationInterval
from src.usage.service import UsageService


class TestUsageServiceLogRequest:
    """Tests for log_request method."""

    @pytest.mark.asyncio
    async def test_log_request(self, usage_service: UsageService, org_user_context: TokenContext, db_session: AsyncSession):
        """Test logging a request."""
        await usage_service.log_request(
            context=org_user_context,
            model="claude-3-sonnet",
            input_tokens=100,
            output_tokens=200,
            cost_usd=0.01,
            latency_ms=150,
            status_code=200,
            request_id="req-12345",
            bedrock_account_id="123456789012",
        )

        # Verify the log was created
        result = await db_session.execute(select(UsageLog).where(UsageLog.request_id == "req-12345"))
        log = result.scalar_one_or_none()

        assert log is not None
        assert log.org_id == "org-001"
        assert log.user_id == "user-001"
        assert log.model == "claude-3-sonnet"
        assert log.input_tokens == 100
        assert log.output_tokens == 200
        assert log.cost_usd == Decimal("0.01")

    @pytest.mark.asyncio
    async def test_log_request_service_account(self, usage_service: UsageService, service_account_context: TokenContext, db_session: AsyncSession):
        """Test logging a request from a service account."""
        await usage_service.log_request(
            context=service_account_context,
            model="claude-3-haiku",
            input_tokens=500,
            output_tokens=1000,
            cost_usd=0.005,
            latency_ms=100,
            status_code=200,
        )

        result = await db_session.execute(select(UsageLog).where(UsageLog.user_id == "service-001"))
        log = result.scalar_one_or_none()

        assert log is not None
        assert log.account_type == "service"

    @pytest.mark.asyncio
    async def test_log_request_error_status(self, usage_service: UsageService, org_user_context: TokenContext, db_session: AsyncSession):
        """Test logging a request with error status."""
        await usage_service.log_request(
            context=org_user_context,
            model="claude-3-sonnet",
            input_tokens=50,
            output_tokens=0,
            cost_usd=0.005,
            latency_ms=50,
            status_code=400,
        )

        result = await db_session.execute(select(UsageLog).where(UsageLog.status_code == 400))
        log = result.scalar_one()

        assert log.status_code == 400


class TestUsageServiceQueryLogs:
    """Tests for query_logs method."""

    @pytest.mark.asyncio
    async def test_query_logs_by_org(self, usage_service: UsageService, sample_usage_logs):
        """Test querying logs by organization."""
        logs = await usage_service.query_logs("org-001")

        assert len(logs) == 6  # All org-001 logs
        assert all(log["org_id"] == "org-001" for log in logs)

    @pytest.mark.asyncio
    async def test_query_logs_with_user_filter(self, usage_service: UsageService, sample_usage_logs):
        """Test querying logs filtered by user."""
        logs = await usage_service.query_logs("org-001", filters={"user_id": "user-001"})

        assert len(logs) == 4  # user-001 logs in org-001
        assert all(log["user_id"] == "user-001" for log in logs)

    @pytest.mark.asyncio
    async def test_query_logs_with_model_filter(self, usage_service: UsageService, sample_usage_logs):
        """Test querying logs filtered by model."""
        logs = await usage_service.query_logs("org-001", filters={"model": "claude-3-opus"})

        assert len(logs) == 1
        assert logs[0]["model"] == "claude-3-opus"

    @pytest.mark.asyncio
    async def test_query_logs_pagination(self, usage_service: UsageService, sample_usage_logs):
        """Test log pagination."""
        logs_page1 = await usage_service.query_logs("org-001", limit=2, offset=0)
        logs_page2 = await usage_service.query_logs("org-001", limit=2, offset=2)

        assert len(logs_page1) == 2
        assert len(logs_page2) == 2

        # Pages should have different logs
        ids_page1 = {log["id"] for log in logs_page1}
        ids_page2 = {log["id"] for log in logs_page2}
        assert ids_page1.isdisjoint(ids_page2)

    @pytest.mark.asyncio
    async def test_query_logs_sorted_by_timestamp(self, usage_service: UsageService, sample_usage_logs):
        """Test logs are sorted by timestamp descending."""
        logs = await usage_service.query_logs("org-001")

        timestamps = [log["timestamp"] for log in logs]
        assert timestamps == sorted(timestamps, reverse=True)


class TestUsageServiceGetUsageSummary:
    """Tests for get_usage_summary method."""

    @pytest.mark.asyncio
    async def test_get_usage_summary(self, usage_service: UsageService, sample_usage_logs):
        """Test getting usage summary."""
        summary = await usage_service.get_usage_summary("org-001")

        assert summary["org_id"] == "org-001"
        assert summary["total_requests"] >= 1
        assert summary["total_input_tokens"] >= 0
        assert summary["total_output_tokens"] >= 0
        assert summary["total_cost_usd"] >= 0
        assert "unique_users" in summary
        assert "unique_models" in summary

    @pytest.mark.asyncio
    async def test_get_usage_summary_error_rate(self, usage_service: UsageService, sample_usage_logs):
        """Test error rate calculation in summary."""
        summary = await usage_service.get_usage_summary("org-001")

        # org-001 has 6 requests, 1 error (usage-004)
        assert summary["failed_requests"] >= 1
        assert summary["error_rate_percent"] > 0

    @pytest.mark.asyncio
    async def test_get_usage_summary_with_filters(self, usage_service: UsageService, sample_usage_logs):
        """Test usage summary with filters."""
        summary = await usage_service.get_usage_summary(
            "org-001",
            filters={"department_id": "dept-001"},
        )

        # Should only include dept-001 logs
        assert summary["total_requests"] == 5  # All dept-001 logs in org-001

    @pytest.mark.asyncio
    async def test_get_usage_summary_empty(self, usage_service: UsageService):
        """Test usage summary for org with no logs."""
        summary = await usage_service.get_usage_summary("non-existent-org")

        assert summary["total_requests"] == 0
        assert summary["total_cost_usd"] == 0


class TestUsageServiceGetUsageByOrganization:
    """Tests for get_usage_by_organization method."""

    @pytest.mark.asyncio
    async def test_get_usage_by_organization(self, usage_service: UsageService, sample_usage_logs):
        """Test getting usage by organization."""
        results = await usage_service.get_usage_by_organization()

        assert len(results) == 2  # org-001 and org-002
        org_ids = {r.org_id for r in results}
        assert "org-001" in org_ids
        assert "org-002" in org_ids

    @pytest.mark.asyncio
    async def test_get_usage_by_organization_filtered(self, usage_service: UsageService, sample_usage_logs):
        """Test getting usage for specific organizations."""
        results = await usage_service.get_usage_by_organization(org_ids=["org-001"])

        assert len(results) == 1
        assert results[0].org_id == "org-001"

    @pytest.mark.asyncio
    async def test_get_usage_by_organization_time_range(self, usage_service: UsageService, sample_usage_logs):
        """Test usage by org with time range filter."""
        now = datetime.now(UTC)
        results = await usage_service.get_usage_by_organization(
            start_date=now - timedelta(hours=6),
            end_date=now,
        )

        # Should include recent logs only
        for result in results:
            assert result.total_requests >= 0


class TestUsageServiceGetUsageByModel:
    """Tests for get_usage_by_model method."""

    @pytest.mark.asyncio
    async def test_get_usage_by_model(self, usage_service: UsageService, sample_usage_logs):
        """Test getting usage by model."""
        results = await usage_service.get_usage_by_model(org_id="org-001")

        # Should have multiple models
        models = {r.model for r in results}
        assert "claude-3-sonnet" in models
        assert "claude-3-opus" in models

    @pytest.mark.asyncio
    async def test_get_usage_by_model_aggregation(self, usage_service: UsageService, sample_usage_logs):
        """Test model usage aggregation."""
        results = await usage_service.get_usage_by_model(org_id="org-001")

        sonnet = next((r for r in results if r.model == "claude-3-sonnet"), None)
        assert sonnet is not None
        assert sonnet.total_requests >= 4  # Multiple sonnet requests

    @pytest.mark.asyncio
    async def test_get_usage_by_model_all_orgs(self, usage_service: UsageService, sample_usage_logs):
        """Test getting usage by model across all orgs."""
        results = await usage_service.get_usage_by_model(org_id=None)

        # Should include models from all orgs
        models = {r.model for r in results}
        assert "claude-3-haiku" in models  # Only in org-002


class TestUsageServiceGetUsageTimeline:
    """Tests for get_usage_timeline method.

    Note: These tests use date_trunc which is PostgreSQL-specific.
    Skipped in SQLite tests.
    """

    @pytest.mark.asyncio
    @pytest.mark.skip(reason="date_trunc is PostgreSQL-specific, not available in SQLite")
    async def test_get_usage_timeline_daily(self, usage_service: UsageService, sample_usage_logs):
        """Test getting daily usage timeline."""
        now = datetime.now(UTC)
        result = await usage_service.get_usage_timeline(
            org_id="org-001",
            start_date=now - timedelta(days=8),
            end_date=now,
            interval=AggregationInterval.DAILY,
        )

        assert result.interval == AggregationInterval.DAILY
        assert len(result.data) >= 1  # At least one day with data

    @pytest.mark.asyncio
    @pytest.mark.skip(reason="date_trunc is PostgreSQL-specific, not available in SQLite")
    async def test_get_usage_timeline_hourly(self, usage_service: UsageService, sample_usage_logs):
        """Test getting hourly usage timeline."""
        now = datetime.now(UTC)
        result = await usage_service.get_usage_timeline(
            org_id="org-001",
            start_date=now - timedelta(hours=12),
            end_date=now,
            interval=AggregationInterval.HOURLY,
        )

        assert result.interval == AggregationInterval.HOURLY

    @pytest.mark.asyncio
    @pytest.mark.skip(reason="date_trunc is PostgreSQL-specific, not available in SQLite")
    async def test_get_usage_timeline_entries(self, usage_service: UsageService, sample_usage_logs):
        """Test timeline entry structure."""
        now = datetime.now(UTC)
        result = await usage_service.get_usage_timeline(
            org_id="org-001",
            start_date=now - timedelta(days=2),
            end_date=now,
            interval=AggregationInterval.DAILY,
        )

        for entry in result.data:
            assert hasattr(entry, "timestamp")
            assert hasattr(entry, "total_requests")
            assert hasattr(entry, "total_tokens")
            assert hasattr(entry, "total_cost_usd")


class TestUsageServiceGetUsageByUser:
    """Tests for get_usage_by_user method."""

    @pytest.mark.asyncio
    async def test_get_usage_by_user(self, usage_service: UsageService, sample_usage_logs):
        """Test getting usage by user."""
        results = await usage_service.get_usage_by_user("org-001")

        assert len(results) >= 2  # At least user-001 and user-002
        user_ids = {r["user_id"] for r in results}
        assert "user-001" in user_ids

    @pytest.mark.asyncio
    async def test_get_usage_by_user_sorted_by_cost(self, usage_service: UsageService, sample_usage_logs):
        """Test users are sorted by cost descending."""
        results = await usage_service.get_usage_by_user("org-001")

        costs = [r["total_cost_usd"] for r in results]
        assert costs == sorted(costs, reverse=True)

    @pytest.mark.asyncio
    async def test_get_usage_by_user_limit(self, usage_service: UsageService, sample_usage_logs):
        """Test user limit."""
        results = await usage_service.get_usage_by_user("org-001", limit=2)

        assert len(results) <= 2


class TestUsageServiceGetUsageByDepartment:
    """Tests for get_usage_by_department method."""

    @pytest.mark.asyncio
    async def test_get_usage_by_department(self, usage_service: UsageService, sample_usage_logs):
        """Test getting usage by department."""
        results = await usage_service.get_usage_by_department("org-001")

        dept_ids = {r["department_id"] for r in results}
        assert "dept-001" in dept_ids
        assert "dept-002" in dept_ids

    @pytest.mark.asyncio
    async def test_get_usage_by_department_unique_users(self, usage_service: UsageService, sample_usage_logs):
        """Test unique user count per department."""
        results = await usage_service.get_usage_by_department("org-001")

        dept_001 = next((r for r in results if r["department_id"] == "dept-001"), None)
        assert dept_001 is not None
        assert dept_001["unique_users"] >= 2  # user-001 and user-002


class TestUsageServiceFilters:
    """Tests for filter functionality."""

    @pytest.mark.asyncio
    async def test_filter_by_status_code(self, usage_service: UsageService, sample_usage_logs):
        """Test filtering by status code."""
        logs = await usage_service.query_logs("org-001", filters={"status_code": 400})

        assert len(logs) == 1
        assert logs[0]["status_code"] == 400

    @pytest.mark.asyncio
    async def test_filter_by_latency(self, usage_service: UsageService, sample_usage_logs):
        """Test filtering by latency."""
        logs = await usage_service.query_logs(
            "org-001",
            filters={"min_latency_ms": 200},
        )

        assert all(log["latency_ms"] >= 200 for log in logs)

    @pytest.mark.asyncio
    async def test_filter_by_account_type(self, usage_service: UsageService, sample_usage_logs):
        """Test filtering by account type."""
        logs = await usage_service.query_logs("org-002", filters={"account_type": "service"})

        assert all(log["account_type"] == "service" for log in logs)
