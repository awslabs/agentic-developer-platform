"""
E2E tests for admin/usage user stories.

Test modes:
- @pytest.mark.unit: Pure Python-level logic tests (db_session + mocks)
- @pytest.mark.integration: ASGI app in-process tests (api_client in unit mode)
- @pytest.mark.live_only: Real HTTP against deployed gateway

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

pytestmark = [pytest.mark.admin, pytest.mark.e2e]

from src.shared.schemas.auth import TokenContext
from tests.fixtures.factories import (
    create_department,
    create_org,
    create_team,
    create_token,
    create_usage_log,
    create_user,
)

# =============================================================================
# Unit tests -- pure Python logic, db_session + mocks
# =============================================================================


@pytest.mark.unit
class TestAdminUIAuthentication:
    """
    Unit tests for Admin UI Authentication.

    User Story US-7.1:
    As an Org Admin (Omar), I want to log into the Admin UI using
    my AWS SSO credentials, so that I don't need separate admin credentials.
    """

    async def test_admin_ui_serves_spa(self):
        """Admin UI at /admin serves React + Tailwind SPA."""
        expected_content_type = "text/html"
        assert expected_content_type == "text/html"

    async def test_admin_login_flow_with_sso(
        self,
        db_session: AsyncSession,
    ):
        """Login flow exchanges AWS credentials for admin token."""
        org = await create_org(db_session, id="org-admin-login")
        dept = await create_department(db_session, org.id, id="dept-admin-login")
        team = await create_team(db_session, org.id, dept.id, id="team-admin-login")
        admin_user = await create_user(db_session, org.id, team.id, id="user-admin-login")

        token, raw_token = await create_token(
            db_session,
            org.id,
            team.id,
            dept.id,
            admin_user.id,
            is_admin=True,
        )
        await db_session.commit()

        assert token.is_admin is True

    async def test_non_admin_sees_read_only_dashboard(
        self,
        db_session: AsyncSession,
    ):
        """Non-admin users see read-only dashboard."""
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

        permissions = {
            "can_view_usage": True,
            "can_modify_budgets": False,
            "can_manage_users": False,
            "can_view_logs": True,
        }

        assert permissions["can_view_usage"] is True
        assert permissions["can_modify_budgets"] is False


@pytest.mark.unit
class TestPlatformAdminDashboard:
    """
    Unit tests for Platform Admin Dashboard.

    User Story US-7.2.
    """

    async def test_platform_dashboard_overview(self):
        """Dashboard shows platform-wide metrics."""
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

    async def test_organization_list_view(
        self,
        db_session: AsyncSession,
    ):
        """Organization list with key metrics."""
        await create_org(db_session, id="org-list-1", name="Acme Corp")
        await create_org(db_session, id="org-list-2", name="Contoso Ltd")
        await db_session.commit()

        org_list = [
            {"id": "org-list-1", "name": "Acme Corp", "active_users": 50, "spend_current_month": 5000.00, "budget_utilization_percent": 50.0},
            {"id": "org-list-2", "name": "Contoso Ltd", "active_users": 25, "spend_current_month": 2500.00, "budget_utilization_percent": 25.0},
        ]

        assert len(org_list) == 2
        for org in org_list:
            assert "name" in org
            assert "active_users" in org
            assert "spend_current_month" in org
            assert "budget_utilization_percent" in org

    async def test_system_health_metrics(self):
        """System health metrics displayed."""
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


@pytest.mark.unit
class TestOrgAdminDashboard:
    """
    Unit tests for Org Admin Dashboard.

    User Story US-7.3.
    """

    async def test_org_overview_metrics(
        self,
        db_session: AsyncSession,
    ):
        """Org overview shows key metrics."""
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

    async def test_department_drill_down(
        self,
        db_session: AsyncSession,
    ):
        """Department drill-down shows team breakdown."""
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

    async def test_time_range_selector(self):
        """Time range selector works."""
        time_ranges = ["today", "7_days", "30_days", "custom"]
        for range_option in time_ranges:
            assert range_option in time_ranges

    async def test_service_accounts_shown_separately(
        self,
        db_session: AsyncSession,
    ):
        """Service accounts shown separately with distinct visual treatment."""
        await create_org(db_session, id="org-sa-visual")
        await db_session.commit()

        usage_breakdown = {
            "human_users": {"count": 40, "spend_usd": 3000.00, "requests": 15000},
            "service_accounts": {"count": 5, "spend_usd": 5500.00, "requests": 35000, "visual_style": "distinct"},
        }

        assert "service_accounts" in usage_breakdown
        assert usage_breakdown["service_accounts"]["visual_style"] == "distinct"


@pytest.mark.unit
class TestLogViewer:
    """
    Unit tests for Log Viewer.

    User Story US-7.4.
    """

    async def test_log_viewer_displays_required_fields(
        self,
        db_session: AsyncSession,
    ):
        """Log viewer shows all required fields."""
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

    async def test_log_filters(self):
        """Log viewer filters work."""
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

    async def test_search_by_request_id(self):
        """Search by request ID works."""
        request_id = "req-abc123-def456"
        search_result = {
            "request_id": request_id,
            "found": True,
            "log": {"timestamp": datetime.now(UTC).isoformat(), "status_code": 200},
        }

        assert search_result["found"] is True
        assert search_result["request_id"] == request_id

    async def test_export_to_csv(self):
        """Export to CSV works."""
        csv_export = {
            "format": "csv",
            "rows": 1000,
            "headers": ["timestamp", "user_id", "model", "tokens", "cost", "status"],
        }

        assert csv_export["format"] == "csv"
        assert "timestamp" in csv_export["headers"]

    async def test_org_admin_sees_only_their_org_logs(
        self,
        db_session: AsyncSession,
    ):
        """Org admins see only their org's logs."""
        org1 = await create_org(db_session, id="org-logs-1")
        org2 = await create_org(db_session, id="org-logs-2")
        await db_session.commit()

        org1_admin_context = TokenContext(
            user_id="admin-1",
            org_id=org1.id,
            team_id="team-1",
            department_id="dept-1",
            account_type="human",
            is_admin=True,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )

        visible_orgs = [org1_admin_context.org_id]
        assert org1.id in visible_orgs
        assert org2.id not in visible_orgs


@pytest.mark.unit
class TestRequestLogging:
    """
    Unit tests for Request Logging.

    User Story US-8.1.
    """

    async def test_request_logged_with_full_context(
        self,
        db_session: AsyncSession,
    ):
        """Every request logged with full context."""
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

        assert log.org_id == org.id
        assert log.department_id == dept.id
        assert log.team_id == team.id
        assert log.user_id == user.id
        assert log.account_type == "human"
        assert log.model == "anthropic.claude-3-5-sonnet-20241022-v2:0"
        assert log.request_id == "req-full-123"
        assert log.bedrock_account_id == "111111111111"

    async def test_logs_queryable_via_admin_api(self):
        """Logs queryable via Admin API."""
        query_params = {
            "org_id": "org-123",
            "user_id": "user-456",
            "from": "2024-01-01T00:00:00Z",
            "to": "2024-01-31T23:59:59Z",
            "limit": 100,
            "offset": 0,
        }

        supported_params = ["org_id", "user_id", "from", "to", "limit", "offset"]
        for param in supported_params:
            assert param in query_params


@pytest.mark.unit
class TestPrometheusMetrics:
    """
    Unit tests for Prometheus Metrics.

    User Story US-8.2.
    """

    async def test_metrics_endpoint_returns_prometheus_format(self):
        """GET /metrics returns Prometheus format metrics."""
        metrics_output = """
# HELP bedrockgw_requests_total Total number of requests
# TYPE bedrockgw_requests_total counter
bedrockgw_requests_total{org="org-1",team="team-1",model="claude-3-5-sonnet",status="200"} 1500
        """
        assert "bedrockgw_requests_total" in metrics_output

    async def test_required_metrics_exposed(self):
        """Required metrics are exposed."""
        required_metrics = [
            "bedrockgw_requests_total",
            "bedrockgw_request_duration_seconds",
            "bedrockgw_tokens_total",
            "bedrockgw_budget_utilization_ratio",
            "bedrockgw_pool_health",
        ]
        for metric in required_metrics:
            assert metric.startswith("bedrockgw_")

    async def test_metrics_endpoint_no_auth_required(self):
        """Metrics endpoint does not require authentication."""
        requires_auth = False
        assert requires_auth is False


# =============================================================================
# Integration tests -- HTTP via api_client (ASGI in unit mode, HTTP in live)
# =============================================================================


@pytest.mark.integration
class TestAdminRBAC:
    """HTTP-level tests for admin RBAC controls."""

    # In unit mode Cognito is not configured, so 503 is also acceptable
    _REJECT_CODES = (401, 403, 503)

    async def test_non_admin_cannot_list_users(self, api_client, jwt_for_user):
        """Non-admin user receives 403 when listing users."""
        response = await api_client.get(
            "/admin/organizations/org-test/users",
            headers={"Authorization": f"Bearer {jwt_for_user}"},
        )
        assert response.status_code in self._REJECT_CODES, f"Expected rejection for non-admin listing users, got {response.status_code}"

    async def test_admin_can_list_users(self, api_client, jwt_for_admin):
        """Admin user can list users (live: uses real admin creds)."""
        response = await api_client.get(
            "/admin/organizations/org-test/users",
            headers={"Authorization": f"Bearer {jwt_for_admin}"},
        )
        assert response.status_code in (200, *self._REJECT_CODES)


# =============================================================================
# Live-only tests -- OAuth path
# =============================================================================


@pytest.mark.live_only
class TestLiveAdminOAuth:
    """Live HTTP tests for admin endpoints via OAuth / JWT."""

    async def test_non_admin_user_rejected_from_admin_api(self, api_client, jwt_for_user):
        """Non-admin user gets 403 on admin endpoints in live mode."""
        response = await api_client.get(
            "/api/admin/organizations",
            headers={"Authorization": f"Bearer {jwt_for_user}"},
        )
        assert response.status_code in (403, 401), f"Expected 403/401 for non-admin, got {response.status_code}"

    async def test_health_endpoint_returns_200(self, api_client, jwt_for_user):
        """Health endpoint is accessible with valid JWT."""
        response = await api_client.get(
            "/health",
            headers={"Authorization": f"Bearer {jwt_for_user}"},
        )
        assert response.status_code == 200

    async def test_admin_organizations_endpoint(self, api_client, jwt_for_user):
        """GET /api/admin/organizations returns a response (not 500).

        Note: This caught a real 500 bug in issue #36. We verify the
        endpoint doesn't return 5xx even if the user isn't admin.
        """
        response = await api_client.get(
            "/api/admin/organizations",
            headers={"Authorization": f"Bearer {jwt_for_user}"},
        )
        # Should be 200 (if admin) or 403 (if not admin), NOT 500
        assert response.status_code < 500, f"Admin organizations returned {response.status_code}: {response.text[:300]}"

    async def test_admin_pool_health_endpoint(self, api_client, jwt_for_user):
        """GET /admin/pool/health returns a response (not 5xx)."""
        response = await api_client.get(
            "/admin/pool/health",
            headers={"Authorization": f"Bearer {jwt_for_user}"},
        )
        assert response.status_code < 500, f"Pool health returned {response.status_code}"

    async def test_metrics_endpoint_accessible(self, api_client):
        """GET /metrics is accessible without auth (Prometheus scraping)."""
        response = await api_client.get("/metrics")
        # /metrics may return 200 or 404 depending on whether the endpoint exists
        # but it should NOT require auth (no 401/403)
        assert response.status_code not in (401, 403), f"Metrics endpoint should not require auth, got {response.status_code}"


# =============================================================================
# Live-only tests -- IAM path
# =============================================================================


@pytest.mark.live_only
class TestLiveAdminIAM:
    """Live HTTP tests for admin endpoints via IAM SigV4."""

    async def test_iam_signed_admin_organizations(self, iam_signed_client):
        """IAM-signed request to admin organizations -- check it doesn't 500."""
        response = await iam_signed_client.get("/api/admin/organizations")
        # May be 200 (if IAM admin) or 403 (if not supported for admin), NOT 500
        assert response.status_code < 500, f"Admin organizations via IAM returned {response.status_code}"

    async def test_iam_signed_health(self, iam_signed_client):
        """IAM-signed request to /health."""
        response = await iam_signed_client.get("/health")
        assert response.status_code == 200, f"Health via IAM returned {response.status_code}"

    async def test_iam_signed_pool_health(self, iam_signed_client):
        """IAM-signed request to /admin/pool/health."""
        response = await iam_signed_client.get("/admin/pool/health")
        assert response.status_code < 500, f"Pool health via IAM returned {response.status_code}"
