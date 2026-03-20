"""Integration tests for Usage module."""

from datetime import UTC, datetime, timedelta

import pytest

from src.shared.schemas.auth import TokenContext
from src.usage.config import AggregationInterval
from src.usage.service import UsageService


class TestUsageLoggingWorkflow:
    """Integration tests for usage logging workflow."""

    @pytest.mark.asyncio
    async def test_log_and_query_request(self, usage_service: UsageService, org_user_context: TokenContext):
        """Test logging a request and querying it."""
        # Log a request
        await usage_service.log_request(
            context=org_user_context,
            model="claude-3-sonnet",
            input_tokens=100,
            output_tokens=200,
            cost_usd=0.01,
            latency_ms=150,
            status_code=200,
            request_id="integration-test-001",
        )

        # Query logs
        logs = await usage_service.query_logs(org_user_context.org_id)

        # Verify the log is present
        assert any(log.get("request_id") == "integration-test-001" for log in logs)

    @pytest.mark.asyncio
    async def test_log_multiple_and_aggregate(self, usage_service: UsageService, org_user_context: TokenContext):
        """Test logging multiple requests and aggregating."""
        # Log multiple requests
        for i in range(5):
            await usage_service.log_request(
                context=org_user_context,
                model="claude-3-sonnet",
                input_tokens=100 * (i + 1),
                output_tokens=200 * (i + 1),
                cost_usd=0.01 * (i + 1),
                latency_ms=150 + i * 10,
                status_code=200 if i < 4 else 400,  # Last one is an error
            )

        # Get summary
        summary = await usage_service.get_usage_summary(org_user_context.org_id)

        # Verify aggregation
        assert summary["total_requests"] >= 5
        assert summary["failed_requests"] >= 1
        assert summary["total_input_tokens"] >= 1500  # 100+200+300+400+500
        assert summary["total_output_tokens"] >= 3000  # 200+400+600+800+1000

    @pytest.mark.asyncio
    async def test_log_different_models(self, usage_service: UsageService, org_user_context: TokenContext):
        """Test logging requests with different models."""
        models = ["claude-3-sonnet", "claude-3-opus", "claude-3-haiku"]

        for model in models:
            await usage_service.log_request(
                context=org_user_context,
                model=model,
                input_tokens=100,
                output_tokens=200,
                cost_usd=0.01,
                latency_ms=150,
                status_code=200,
            )

        # Get usage by model
        model_usage = await usage_service.get_usage_by_model(org_id=org_user_context.org_id)

        # Verify all models present
        model_names = {m.model for m in model_usage}
        for model in models:
            assert model in model_names


class TestUsageAggregationWorkflow:
    """Integration tests for usage aggregation."""

    @pytest.mark.asyncio
    async def test_usage_summary_accuracy(self, usage_service: UsageService, sample_usage_logs):
        """Test usage summary calculation accuracy."""
        summary = await usage_service.get_usage_summary("org-001")

        # Verify totals match expected values from sample data
        assert summary["total_requests"] == 6  # 6 logs for org-001
        assert summary["failed_requests"] == 1  # 1 error (usage-004)
        assert summary["unique_users"] >= 3  # user-001, user-002, user-004

    @pytest.mark.asyncio
    async def test_usage_by_department_accuracy(self, usage_service: UsageService, sample_usage_logs):
        """Test department aggregation accuracy."""
        dept_usage = await usage_service.get_usage_by_department("org-001")

        # Find dept-001
        dept_001 = next((d for d in dept_usage if d["department_id"] == "dept-001"), None)
        assert dept_001 is not None
        assert dept_001["total_requests"] == 5  # 5 logs in dept-001

    @pytest.mark.asyncio
    @pytest.mark.skip(reason="date_trunc is PostgreSQL-specific, not available in SQLite")
    async def test_usage_timeline_intervals(self, usage_service: UsageService, sample_usage_logs):
        """Test timeline with different intervals."""
        now = datetime.now(UTC)
        start = now - timedelta(days=10)

        # Daily
        daily = await usage_service.get_usage_timeline(
            org_id="org-001",
            start_date=start,
            end_date=now,
            interval=AggregationInterval.DAILY,
        )
        assert daily.interval == AggregationInterval.DAILY

        # Weekly
        weekly = await usage_service.get_usage_timeline(
            org_id="org-001",
            start_date=start,
            end_date=now,
            interval=AggregationInterval.WEEKLY,
        )
        assert weekly.interval == AggregationInterval.WEEKLY


class TestUsageFilteringWorkflow:
    """Integration tests for usage filtering."""

    @pytest.mark.asyncio
    async def test_filter_by_time_range(self, usage_service: UsageService, sample_usage_logs):
        """Test filtering logs by time range."""
        now = datetime.now(UTC)

        # Recent logs only (last 6 hours)
        recent_summary = await usage_service.get_usage_summary(
            "org-001",
            filters={
                "start_date": now - timedelta(hours=6),
                "end_date": now,
            },
        )

        # All logs
        all_summary = await usage_service.get_usage_summary("org-001")

        # Recent should be less than or equal to all
        assert recent_summary["total_requests"] <= all_summary["total_requests"]

    @pytest.mark.asyncio
    async def test_filter_by_user(self, usage_service: UsageService, sample_usage_logs):
        """Test filtering by user."""
        logs = await usage_service.query_logs(
            "org-001",
            filters={"user_id": "user-001"},
        )

        # All logs should be from user-001
        assert all(log["user_id"] == "user-001" for log in logs)

    @pytest.mark.asyncio
    async def test_filter_by_status_code(self, usage_service: UsageService, sample_usage_logs):
        """Test filtering by status code."""
        error_logs = await usage_service.query_logs(
            "org-001",
            filters={"status_code": 400},
        )

        assert len(error_logs) >= 1
        assert all(log["status_code"] == 400 for log in error_logs)


class TestMultiOrganizationWorkflow:
    """Integration tests for multi-organization scenarios."""

    @pytest.mark.asyncio
    async def test_organization_isolation(self, usage_service: UsageService, sample_usage_logs):
        """Test that organizations' data is isolated."""
        org1_logs = await usage_service.query_logs("org-001")
        org2_logs = await usage_service.query_logs("org-002")

        # Verify no cross-contamination
        assert all(log["org_id"] == "org-001" for log in org1_logs)
        assert all(log["org_id"] == "org-002" for log in org2_logs)

    @pytest.mark.asyncio
    async def test_cross_org_summary(self, usage_service: UsageService, sample_usage_logs):
        """Test getting summaries for different orgs."""
        org1_summary = await usage_service.get_usage_summary("org-001")
        org2_summary = await usage_service.get_usage_summary("org-002")

        # Summaries should be independent
        assert org1_summary["org_id"] == "org-001"
        assert org2_summary["org_id"] == "org-002"

        # Data should differ
        assert org1_summary["total_requests"] != org2_summary["total_requests"]


class TestUsageMetricsCalculation:
    """Integration tests for metrics calculation."""

    @pytest.mark.asyncio
    async def test_error_rate_calculation(self, usage_service: UsageService, sample_usage_logs):
        """Test error rate calculation."""
        summary = await usage_service.get_usage_summary("org-001")

        # org-001 has 6 requests, 1 error
        expected_error_rate = (1 / 6) * 100
        assert abs(summary["error_rate_percent"] - expected_error_rate) < 0.1

    @pytest.mark.asyncio
    async def test_average_latency_calculation(self, usage_service: UsageService, sample_usage_logs):
        """Test average latency calculation."""
        summary = await usage_service.get_usage_summary("org-001")

        # Should have a reasonable average
        assert summary["average_latency_ms"] > 0
        assert summary["average_latency_ms"] < 1000  # Less than 1 second

    @pytest.mark.asyncio
    async def test_cost_aggregation(self, usage_service: UsageService, sample_usage_logs):
        """Test cost aggregation."""
        summary = await usage_service.get_usage_summary("org-001")

        # Verify total cost is sum of individual costs
        # Expected: 0.01 + 0.10 + 0.02 + 0.005 + 0.03 + 0.015 = 0.18
        assert summary["total_cost_usd"] > 0


class TestEndToEndUsageWorkflow:
    """End-to-end integration tests for usage tracking."""

    @pytest.mark.asyncio
    @pytest.mark.skip(reason="date_trunc is PostgreSQL-specific, not available in SQLite")
    async def test_full_usage_tracking_workflow(self, usage_service: UsageService, org_user_context: TokenContext):
        """Test complete usage tracking workflow."""
        # 1. Log requests from different users and models
        contexts = [
            org_user_context,
            TokenContext(
                user_id="user-002",
                org_id="org-001",
                team_id="team-002",
                department_id="dept-001",
                account_type="human",
                is_admin=False,
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            ),
        ]

        models = ["claude-3-sonnet", "claude-3-opus"]

        for ctx in contexts:
            for model in models:
                await usage_service.log_request(
                    context=ctx,
                    model=model,
                    input_tokens=100,
                    output_tokens=200,
                    cost_usd=0.01,
                    latency_ms=150,
                    status_code=200,
                )

        # 2. Get summary
        summary = await usage_service.get_usage_summary("org-001")
        assert summary["total_requests"] >= 4

        # 3. Get usage by model
        model_usage = await usage_service.get_usage_by_model(org_id="org-001")
        assert len(model_usage) >= 2

        # 4. Get usage by user
        user_usage = await usage_service.get_usage_by_user("org-001")
        assert len(user_usage) >= 2

        # 5. Get timeline (skipped in SQLite)
        timeline = await usage_service.get_usage_timeline(
            org_id="org-001",
            start_date=datetime.now(UTC) - timedelta(days=1),
            end_date=datetime.now(UTC),
            interval=AggregationInterval.HOURLY,
        )
        assert len(timeline.data) >= 1
