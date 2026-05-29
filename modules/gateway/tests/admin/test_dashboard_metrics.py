"""Unit tests for dashboard metrics service methods (Issue #1003)."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from src.admin.service import AdminService
from src.shared.models.usage import UsageLog


@pytest.fixture
async def sample_usage_logs(db_session):
    """Seed usage_logs with known rows for dashboard metric tests."""
    now = datetime.now(UTC)
    logs = [
        UsageLog(
            id="log-001",
            timestamp=now - timedelta(hours=1),
            org_id="org-001",
            department_id="dept-a",
            team_id="team-1",
            user_id="user-1",
            account_type="human",
            model="claude-3-sonnet",
            input_tokens=100,
            output_tokens=50,
            cost_usd=Decimal("0.001500"),
            latency_ms=200,
            status_code=200,
            request_id="req-001",
        ),
        UsageLog(
            id="log-002",
            timestamp=now - timedelta(hours=2),
            org_id="org-001",
            department_id="dept-a",
            team_id="team-1",
            user_id="user-2",
            account_type="human",
            model="claude-3-haiku",
            input_tokens=200,
            output_tokens=100,
            cost_usd=Decimal("0.000500"),
            latency_ms=150,
            status_code=200,
            request_id="req-002",
        ),
        UsageLog(
            id="log-003",
            timestamp=now - timedelta(hours=3),
            org_id="org-001",
            department_id="dept-b",
            team_id="team-2",
            user_id="user-1",
            account_type="human",
            model="claude-3-sonnet",
            input_tokens=300,
            output_tokens=150,
            cost_usd=Decimal("0.004500"),
            latency_ms=300,
            status_code=500,
            request_id="req-003",
        ),
        UsageLog(
            id="log-004",
            timestamp=now - timedelta(hours=4),
            org_id="org-002",
            department_id="dept-x",
            team_id="team-x",
            user_id="user-3",
            account_type="human",
            model="claude-3-opus",
            input_tokens=500,
            output_tokens=250,
            cost_usd=Decimal("0.075000"),
            latency_ms=500,
            status_code=200,
            request_id="req-004",
        ),
        # Old log outside 24h window — should NOT be counted
        UsageLog(
            id="log-005",
            timestamp=now - timedelta(hours=25),
            org_id="org-001",
            department_id="dept-a",
            team_id="team-1",
            user_id="user-1",
            account_type="human",
            model="claude-3-sonnet",
            input_tokens=1000,
            output_tokens=500,
            cost_usd=Decimal("0.015000"),
            latency_ms=400,
            status_code=200,
            request_id="req-005",
        ),
    ]
    for log in logs:
        db_session.add(log)
    await db_session.commit()
    return logs


class TestGetPlatformMetrics24h:
    """Tests for get_platform_metrics_24h."""

    @pytest.mark.asyncio
    async def test_returns_correct_aggregates(self, admin_service: AdminService, sample_usage_logs):
        """Test that platform metrics correctly aggregate usage_logs within 24h."""
        metrics = await admin_service.get_platform_metrics_24h()

        # 4 logs within 24h (log-005 is outside window)
        assert metrics["total_requests_24h"] == 4
        # Total tokens: (100+50) + (200+100) + (300+150) + (500+250) = 1650
        assert metrics["total_tokens_24h"] == 1650
        # Total cost: 0.001500 + 0.000500 + 0.004500 + 0.075000 = 0.081500
        assert float(metrics["total_cost_24h"]) == pytest.approx(0.0815, rel=1e-4)
        # Active users: user-1, user-2, user-3 = 3
        assert metrics["active_users_24h"] == 3
        # Active orgs: org-001, org-002 = 2
        assert metrics["total_organizations"] == 2
        # Error rate: 1 error out of 4 requests = 25.0%
        assert metrics["error_rate_24h"] == pytest.approx(25.0, rel=1e-2)

    @pytest.mark.asyncio
    async def test_empty_table_returns_zeros(self, admin_service: AdminService):
        """Test that platform metrics return zeros when usage_logs is empty."""
        metrics = await admin_service.get_platform_metrics_24h()

        assert metrics["total_requests_24h"] == 0
        assert metrics["total_tokens_24h"] == 0
        assert metrics["active_users_24h"] == 0
        assert metrics["total_organizations"] == 0
        assert metrics["error_rate_24h"] == 0.0


class TestGetOrgMetrics24h:
    """Tests for get_org_metrics_24h."""

    @pytest.mark.asyncio
    async def test_returns_correct_aggregates_for_org(self, admin_service: AdminService, sample_usage_logs):
        """Test that org metrics correctly scope to the given org_id."""
        metrics = await admin_service.get_org_metrics_24h("org-001")

        # org-001 has 3 logs within 24h (log-001, log-002, log-003)
        assert metrics["total_requests_24h"] == 3
        # Tokens: (100+50) + (200+100) + (300+150) = 900
        assert metrics["total_tokens_24h"] == 900
        # Cost: 0.001500 + 0.000500 + 0.004500 = 0.006500
        assert float(metrics["total_cost_24h"]) == pytest.approx(0.0065, rel=1e-4)
        # Active users in org-001: user-1, user-2 = 2
        assert metrics["active_users_24h"] == 2
        # Error rate: 1/3 = 33.33%
        assert metrics["error_rate_24h"] == pytest.approx(33.33, rel=1e-1)

    @pytest.mark.asyncio
    async def test_org_with_no_traffic_returns_zeros(self, admin_service: AdminService, sample_usage_logs):
        """Test that an org with no usage_logs returns zeros."""
        metrics = await admin_service.get_org_metrics_24h("org-nonexistent")

        assert metrics["total_requests_24h"] == 0
        assert metrics["total_tokens_24h"] == 0
        assert metrics["active_users_24h"] == 0
        assert metrics["error_rate_24h"] == 0.0


class TestGetTopOrganizations24h:
    """Tests for get_top_organizations_24h."""

    @pytest.mark.asyncio
    async def test_returns_orgs_ranked_by_request_count(self, admin_service: AdminService, sample_usage_logs, sample_organizations):
        """Test that top orgs are sorted by request count desc."""
        top_orgs = await admin_service.get_top_organizations_24h(limit=5)

        assert len(top_orgs) == 2
        # org-001 has 3 requests, org-002 has 1
        assert top_orgs[0]["org_id"] == "org-001"
        assert top_orgs[0]["request_count"] == 3
        assert top_orgs[0]["name"] == "Test Organization 1"
        assert top_orgs[1]["org_id"] == "org-002"
        assert top_orgs[1]["request_count"] == 1

    @pytest.mark.asyncio
    async def test_respects_limit(self, admin_service: AdminService, sample_usage_logs):
        """Test that limit parameter caps results."""
        top_orgs = await admin_service.get_top_organizations_24h(limit=1)

        assert len(top_orgs) == 1
        assert top_orgs[0]["org_id"] == "org-001"


class TestGetTopDepartments24h:
    """Tests for get_top_departments_24h."""

    @pytest.mark.asyncio
    async def test_returns_departments_for_org(self, admin_service: AdminService, sample_usage_logs):
        """Test that departments within an org are ranked by request count."""
        top_depts = await admin_service.get_top_departments_24h("org-001", limit=5)

        assert len(top_depts) == 2
        # dept-a has 2 requests, dept-b has 1
        assert top_depts[0]["department_id"] == "dept-a"
        assert top_depts[0]["request_count"] == 2
        assert top_depts[1]["department_id"] == "dept-b"
        assert top_depts[1]["request_count"] == 1


class TestGetTopModels24h:
    """Tests for get_top_models_24h."""

    @pytest.mark.asyncio
    async def test_returns_models_for_org(self, admin_service: AdminService, sample_usage_logs):
        """Test that models within an org are ranked by request count."""
        top_models = await admin_service.get_top_models_24h("org-001", limit=5)

        assert len(top_models) == 2
        # claude-3-sonnet has 2 requests, claude-3-haiku has 1
        assert top_models[0]["model"] == "claude-3-sonnet"
        assert top_models[0]["request_count"] == 2
        assert top_models[1]["model"] == "claude-3-haiku"
        assert top_models[1]["request_count"] == 1
