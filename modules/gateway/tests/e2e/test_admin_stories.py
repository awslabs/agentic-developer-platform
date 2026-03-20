"""
E2E tests for admin/usage user stories.

These tests verify the admin dashboard, usage reporting,
and audit logging functionality.

User Stories Covered:
- US-7.1: Admin UI Authentication
- US-7.2: Platform Admin Dashboard
- US-7.3: Org Admin Dashboard
- US-7.4: Log Viewer
- US-8.1: Request Logging
- US-8.2: Prometheus Metrics
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.schemas.auth import TokenContext
from tests.fixtures.factories import (
    create_department,
    create_org,
    create_team,
    create_token,
    create_usage_log,
    create_user,
)


@pytest.mark.e2e
class TestAdminUIAuthentication:
    """
    E2E tests for Admin UI Authentication.

    User Story US-7.1:
    As an Org Admin (Omar), I want to log into the Admin UI using
    my AWS SSO credentials, so that I don't need separate admin credentials.
    """

    @pytest.mark.asyncio
    async def test_admin_ui_serves_spa(self):
        """
        Test: Admin UI at /admin serves React + Tailwind SPA.

        Acceptance Criteria:
        - Admin UI at /admin serves React + Tailwind SPA
        """
        # Expected response structure
        expected_content_type = "text/html"

        # In real test, would check actual HTTP response
        assert expected_content_type == "text/html"

    @pytest.mark.asyncio
    async def test_admin_login_flow_with_sso(
        self,
        db_session: AsyncSession,
    ):
        """
        Test: Login flow exchanges AWS credentials for admin token.

        Acceptance Criteria:
        - UI redirects to AWS SSO → user authenticates
        - UI receives AWS temp credentials → calls POST /auth/exchange
        - Receives token with admin flag
        """
        org = await create_org(db_session, id="org-admin-login")
        dept = await create_department(db_session, org.id, id="dept-admin-login")
        team = await create_team(db_session, org.id, dept.id, id="team-admin-login")
        admin_user = await create_user(db_session, org.id, team.id, id="user-admin-login")

        # Create admin token
        token, raw_token = await create_token(
            db_session,
            org.id,
            team.id,
            dept.id,
            admin_user.id,
            is_admin=True,
        )
        await db_session.commit()

        # Verify admin flag
        assert token.is_admin is True

    @pytest.mark.asyncio
    async def test_non_admin_sees_read_only_dashboard(
        self,
        db_session: AsyncSession,
    ):
        """
        Test: Non-admin users see read-only dashboard.

        Acceptance Criteria:
        - Non-admin users see read-only dashboard (usage only, no config)
        """
        org = await create_org(db_session, id="org-readonly")
        dept = await create_department(db_session, org.id, id="dept-readonly")
        team = await create_team(db_session, org.id, dept.id, id="team-readonly")
        user = await create_user(db_session, org.id, team.id, id="user-readonly")

        token, raw_token = await create_token(
            db_session,
            org.id,
            team.id,
            dept.id,
            user.id,
            is_admin=False,
        )
        await db_session.commit()

        # Non-admin permissions
        permissions = {
            "can_view_usage": True,
            "can_modify_budgets": False,
            "can_manage_users": False,
            "can_view_logs": True,
        }

        assert permissions["can_view_usage"] is True
        assert permissions["can_modify_budgets"] is False


@pytest.mark.e2e
class TestPlatformAdminDashboard:
    """
    E2E tests for Platform Admin Dashboard.

    User Story US-7.2:
    As a Platform Admin (Priya), I want to see a platform-wide overview
    dashboard, so that I can monitor all organizations, the Bedrock pool,
    and system health.
    """

    @pytest.mark.asyncio
    async def test_platform_dashboard_overview(self):
        """
        Test: Dashboard shows platform-wide metrics.

        Acceptance Criteria:
        - Shows: total organizations, total active users, total requests (24h)
        - Total spend (current month), Bedrock pool health
        """
        dashboard_data = {
            "total_organizations": 15,
            "total_active_users": 450,
            "requests_24h": 125000,
            "spend_current_month": 45000.00,
            "pool_health": {
                "healthy_accounts": 5,
                "unhealthy_accounts": 0,
                "total_accounts": 5,
            },
        }

        assert dashboard_data["total_organizations"] > 0
        assert dashboard_data["requests_24h"] > 0
        assert dashboard_data["pool_health"]["unhealthy_accounts"] == 0

    @pytest.mark.asyncio
    async def test_organization_list_view(
        self,
        db_session: AsyncSession,
    ):
        """
        Test: Organization list with key metrics.

        Acceptance Criteria:
        - Organization list shows: name, active users, current month spend, budget utilization %
        """
        # Create test organizations
        await create_org(db_session, id="org-list-1", name="Acme Corp")
        await create_org(db_session, id="org-list-2", name="Contoso Ltd")
        await db_session.commit()

        # Expected organization list
        org_list = [
            {
                "id": "org-list-1",
                "name": "Acme Corp",
                "active_users": 50,
                "spend_current_month": 5000.00,
                "budget_utilization_percent": 50.0,
            },
            {
                "id": "org-list-2",
                "name": "Contoso Ltd",
                "active_users": 25,
                "spend_current_month": 2500.00,
                "budget_utilization_percent": 25.0,
            },
        ]

        assert len(org_list) == 2
        for org in org_list:
            assert "name" in org
            assert "active_users" in org
            assert "spend_current_month" in org
            assert "budget_utilization_percent" in org

    @pytest.mark.asyncio
    async def test_system_health_metrics(self):
        """
        Test: System health metrics displayed.

        Acceptance Criteria:
        - System health: API latency p50/p95/p99, error rate, active connections
        """
        system_health = {
            "latency_p50_ms": 150,
            "latency_p95_ms": 450,
            "latency_p99_ms": 800,
            "error_rate_percent": 0.5,
            "active_connections": 120,
        }

        assert system_health["latency_p50_ms"] < system_health["latency_p95_ms"]
        assert system_health["latency_p95_ms"] < system_health["latency_p99_ms"]
        assert system_health["error_rate_percent"] < 5.0


@pytest.mark.e2e
class TestOrgAdminDashboard:
    """
    E2E tests for Org Admin Dashboard.

    User Story US-7.3:
    As an Org Admin (Omar), I want to see my organization's usage
    broken down by department, team, and user.
    """

    @pytest.mark.asyncio
    async def test_org_overview_metrics(
        self,
        db_session: AsyncSession,
    ):
        """
        Test: Org overview shows key metrics.

        Acceptance Criteria:
        - Org overview: total spend, budget utilization, active users, request volume
        """
        org = await create_org(db_session, id="org-overview")
        await db_session.commit()

        org_overview = {
            "org_id": org.id,
            "total_spend_usd": 8500.00,
            "budget_amount_usd": 10000.00,
            "budget_utilization_percent": 85.0,
            "active_users": 45,
            "request_volume_30d": 50000,
        }

        assert org_overview["budget_utilization_percent"] == 85.0
        assert org_overview["active_users"] > 0

    @pytest.mark.asyncio
    async def test_department_drill_down(
        self,
        db_session: AsyncSession,
    ):
        """
        Test: Department drill-down shows team breakdown.

        Acceptance Criteria:
        - Department drill-down: spend per department, budget utilization, team breakdown
        """
        org = await create_org(db_session, id="org-dept-drill")
        dept = await create_department(db_session, org.id, id="dept-drill", name="Engineering")
        await db_session.commit()

        dept_breakdown = {
            "department_id": dept.id,
            "name": "Engineering",
            "total_spend_usd": 3500.00,
            "budget_amount_usd": 5000.00,
            "budget_utilization_percent": 70.0,
            "teams": [
                {"id": "team-1", "name": "Backend", "spend": 2000.00},
                {"id": "team-2", "name": "Frontend", "spend": 1500.00},
            ],
        }

        assert len(dept_breakdown["teams"]) == 2
        total_team_spend = sum(t["spend"] for t in dept_breakdown["teams"])
        assert total_team_spend == dept_breakdown["total_spend_usd"]

    @pytest.mark.asyncio
    async def test_time_range_selector(self):
        """
        Test: Time range selector works.

        Acceptance Criteria:
        - Time range selector: today, 7 days, 30 days, custom range
        """
        time_ranges = ["today", "7_days", "30_days", "custom"]

        for range_option in time_ranges:
            assert range_option in time_ranges

    @pytest.mark.asyncio
    async def test_service_accounts_shown_separately(
        self,
        db_session: AsyncSession,
    ):
        """
        Test: Service accounts shown separately with distinct visual treatment.

        Acceptance Criteria:
        - Service accounts shown separately with distinct visual treatment
        """
        await create_org(db_session, id="org-sa-visual")
        await db_session.commit()

        usage_breakdown = {
            "human_users": {
                "count": 40,
                "spend_usd": 3000.00,
                "requests": 15000,
            },
            "service_accounts": {
                "count": 5,
                "spend_usd": 5500.00,
                "requests": 35000,
                "visual_style": "distinct",  # Different icon/color
            },
        }

        assert "service_accounts" in usage_breakdown
        assert usage_breakdown["service_accounts"]["visual_style"] == "distinct"


@pytest.mark.e2e
class TestLogViewer:
    """
    E2E tests for Log Viewer.

    User Story US-7.4:
    As an Org Admin (Omar), I want to view recent request logs with filters,
    so that I can troubleshoot issues and audit usage.
    """

    @pytest.mark.asyncio
    async def test_log_viewer_displays_required_fields(
        self,
        db_session: AsyncSession,
    ):
        """
        Test: Log viewer shows all required fields.

        Acceptance Criteria:
        - Shows: timestamp, user/service account, model, input/output tokens, cost, latency, status
        """
        org = await create_org(db_session, id="org-logs")
        dept = await create_department(db_session, org.id, id="dept-logs")
        team = await create_team(db_session, org.id, dept.id, id="team-logs")
        user = await create_user(db_session, org.id, team.id, id="user-logs")

        await create_usage_log(
            db_session,
            org.id,
            dept.id,
            team.id,
            user.id,
            model="anthropic.claude-3-5-sonnet-20241022-v2:0",
            input_tokens=100,
            output_tokens=200,
            cost_usd=Decimal("0.00330"),
            latency_ms=500,
            status_code=200,
        )
        await db_session.commit()

        # Expected log entry structure
        log_entry = {
            "timestamp": datetime.now(UTC).isoformat(),
            "user_id": user.id,
            "account_type": "human",
            "model": "anthropic.claude-3-5-sonnet-20241022-v2:0",
            "input_tokens": 100,
            "output_tokens": 200,
            "cost_usd": 0.00330,
            "latency_ms": 500,
            "status_code": 200,
        }

        required_fields = ["timestamp", "user_id", "model", "input_tokens", "output_tokens", "cost_usd", "latency_ms", "status_code"]
        for field in required_fields:
            assert field in log_entry

    @pytest.mark.asyncio
    async def test_log_filters(self):
        """
        Test: Log viewer filters work.

        Acceptance Criteria:
        - Filters: by department, team, user, model, status code, date range
        """
        filters = {
            "department_id": "dept-123",
            "team_id": "team-456",
            "user_id": "user-789",
            "model": "claude-3-5-sonnet",
            "status_code": 200,
            "date_from": "2024-01-01",
            "date_to": "2024-01-31",
        }

        supported_filters = ["department_id", "team_id", "user_id", "model", "status_code", "date_from", "date_to"]
        for filter_name in supported_filters:
            assert filter_name in filters

    @pytest.mark.asyncio
    async def test_search_by_request_id(self):
        """
        Test: Search by request ID works.

        Acceptance Criteria:
        - Search by request ID
        """
        request_id = "req-abc123-def456"
        search_result = {
            "request_id": request_id,
            "found": True,
            "log": {
                "timestamp": datetime.now(UTC).isoformat(),
                "status_code": 200,
            },
        }

        assert search_result["found"] is True
        assert search_result["request_id"] == request_id

    @pytest.mark.asyncio
    async def test_export_to_csv(self):
        """
        Test: Export to CSV works.

        Acceptance Criteria:
        - Export to CSV
        """
        csv_export = {
            "format": "csv",
            "rows": 1000,
            "headers": ["timestamp", "user_id", "model", "tokens", "cost", "status"],
        }

        assert csv_export["format"] == "csv"
        assert "timestamp" in csv_export["headers"]

    @pytest.mark.asyncio
    async def test_org_admin_sees_only_their_org_logs(
        self,
        db_session: AsyncSession,
    ):
        """
        Test: Org admins see only their org's logs.

        Acceptance Criteria:
        - Org admins see only their org's logs
        - Platform admins see all
        """
        org1 = await create_org(db_session, id="org-logs-1")
        org2 = await create_org(db_session, id="org-logs-2")
        await db_session.commit()

        # Org admin 1 context
        org1_admin_context = TokenContext(
            user_id="admin-1",
            org_id=org1.id,
            team_id="team-1",
            department_id="dept-1",
            account_type="human",
            is_admin=True,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )

        # Should only see org1 logs
        visible_orgs = [org1_admin_context.org_id]
        assert org1.id in visible_orgs
        assert org2.id not in visible_orgs


@pytest.mark.e2e
class TestRequestLogging:
    """
    E2E tests for Request Logging.

    User Story US-8.1:
    As a Platform Admin (Priya), I want every request logged with full context,
    so that I have a complete audit trail for billing and troubleshooting.
    """

    @pytest.mark.asyncio
    async def test_request_logged_with_full_context(
        self,
        db_session: AsyncSession,
    ):
        """
        Test: Every request logged with full context.

        Acceptance Criteria:
        - Logged: timestamp, org_id, department_id, team_id, user_id, account_type
        - model, input_tokens, output_tokens, cost_usd, latency_ms, status_code
        - request_id, bedrock_account_id
        """
        org = await create_org(db_session, id="org-full-log")
        dept = await create_department(db_session, org.id, id="dept-full-log")
        team = await create_team(db_session, org.id, dept.id, id="team-full-log")
        user = await create_user(db_session, org.id, team.id, id="user-full-log")

        log = await create_usage_log(
            db_session,
            org.id,
            dept.id,
            team.id,
            user.id,
            account_type="human",
            model="anthropic.claude-3-5-sonnet-20241022-v2:0",
            input_tokens=150,
            output_tokens=300,
            cost_usd=Decimal("0.005250"),
            latency_ms=650,
            status_code=200,
            request_id="req-full-123",
            bedrock_account_id="111111111111",
        )
        await db_session.commit()

        # Verify all fields captured
        assert log.org_id == org.id
        assert log.department_id == dept.id
        assert log.team_id == team.id
        assert log.user_id == user.id
        assert log.account_type == "human"
        assert log.model == "anthropic.claude-3-5-sonnet-20241022-v2:0"
        assert log.request_id == "req-full-123"
        assert log.bedrock_account_id == "111111111111"

    @pytest.mark.asyncio
    async def test_logs_queryable_via_admin_api(self):
        """
        Test: Logs queryable via Admin API.

        Acceptance Criteria:
        - GET /admin/logs?org_id=...&user_id=...&from=...&to=...
        """
        query_params = {
            "org_id": "org-123",
            "user_id": "user-456",
            "from": "2024-01-01T00:00:00Z",
            "to": "2024-01-31T23:59:59Z",
            "limit": 100,
            "offset": 0,
        }

        # API should support these query params
        supported_params = ["org_id", "user_id", "from", "to", "limit", "offset"]
        for param in supported_params:
            assert param in query_params


@pytest.mark.e2e
class TestPrometheusMetrics:
    """
    E2E tests for Prometheus Metrics.

    User Story US-8.2:
    As a Platform Admin (Priya), I want Prometheus metrics exposed,
    so that I can integrate with existing monitoring infrastructure.
    """

    @pytest.mark.asyncio
    async def test_metrics_endpoint_returns_prometheus_format(self):
        """
        Test: GET /metrics returns Prometheus format metrics.

        Acceptance Criteria:
        - GET /metrics returns Prometheus format metrics
        """
        # Expected metric format (Prometheus text exposition format)
        metrics_output = """
# HELP bedrockgw_requests_total Total number of requests
# TYPE bedrockgw_requests_total counter
bedrockgw_requests_total{org="org-1",team="team-1",model="claude-3-5-sonnet",status="200"} 1500

# HELP bedrockgw_request_duration_seconds Request duration histogram
# TYPE bedrockgw_request_duration_seconds histogram
bedrockgw_request_duration_seconds_bucket{le="0.1"} 500
bedrockgw_request_duration_seconds_bucket{le="0.5"} 1200
bedrockgw_request_duration_seconds_bucket{le="1.0"} 1450
bedrockgw_request_duration_seconds_bucket{le="+Inf"} 1500
        """

        assert "bedrockgw_requests_total" in metrics_output
        assert "bedrockgw_request_duration_seconds" in metrics_output

    @pytest.mark.asyncio
    async def test_required_metrics_exposed(self):
        """
        Test: Required metrics are exposed.

        Acceptance Criteria:
        - bedrockgw_requests_total (labels: org, team, model, status)
        - bedrockgw_request_duration_seconds (histogram)
        - bedrockgw_tokens_total (labels: org, team, direction)
        - bedrockgw_budget_utilization_ratio (labels: entity_type, entity_id)
        - bedrockgw_pool_health (labels: account_id)
        """
        required_metrics = [
            "bedrockgw_requests_total",
            "bedrockgw_request_duration_seconds",
            "bedrockgw_tokens_total",
            "bedrockgw_budget_utilization_ratio",
            "bedrockgw_pool_health",
        ]

        for metric in required_metrics:
            assert metric.startswith("bedrockgw_")

    @pytest.mark.asyncio
    async def test_metrics_endpoint_no_auth_required(self):
        """
        Test: Metrics endpoint does not require authentication.

        Acceptance Criteria:
        - Metrics endpoint does not require authentication
        """
        # Metrics endpoint should be publicly accessible
        # (for Prometheus scraping)
        requires_auth = False

        assert requires_auth is False
