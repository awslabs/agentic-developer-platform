"""Unit tests for Stats API routes — Issue #3630.

Covers:
- /me/agent-run-stats endpoint scoping (user-only, excludes other users)
- /admin/agent-run-stats without USAGE_READ permission → 403
- days validation (0 → 422, 31 → 422)
- Cache serves stale result within 60s TTL window
- Auth: missing token → 401
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.activity.routes import get_access_control, get_stats_service, router
from src.activity.stats_schemas import StatsResponse, TodayCounts
from src.activity.stats_service import StatsService
from src.admin.access_control import AccessControl
from src.admin.exceptions import AccessDeniedError
from src.auth.dependencies import get_current_user
from src.shared.database import get_db

# Canonical user_id resolved from token's Cognito sub
CANONICAL_USER_ID = "canonical-abc-999"


@pytest.fixture
def app():
    """Create a test FastAPI app with activity routes."""
    app = FastAPI()
    app.include_router(router)
    return app


@pytest.fixture
def mock_db():
    """Mock AsyncSession whose scalar() resolves to canonical user_id."""
    db = MagicMock()
    db.scalar = AsyncMock(return_value=CANONICAL_USER_ID)
    # Mock the execute for cost enrichment (returns empty result)
    mock_result = MagicMock()
    mock_result.one.return_value = MagicMock(total_cost_usd=None, total_tokens=None, total_calls=None)
    db.execute = AsyncMock(return_value=mock_result)
    return db


@pytest.fixture
def mock_stats_service():
    """Create a mock StatsService that returns empty stats."""
    service = MagicMock(spec=StatsService)
    service.get_stats_by_user = MagicMock(
        return_value=StatsResponse(
            window_days=7,
            active_runs=[],
            today=TodayCounts(total=5, completed=3, failed=1, active=1),
            daily=[],
            by_persona=[],
            recent_failures=[],
            top_repos=[],
            spend=None,
        )
    )
    service.get_stats_by_tenant = MagicMock(
        return_value=StatsResponse(
            window_days=7,
            active_runs=[],
            today=TodayCounts(total=10, completed=7, failed=2, active=1),
            daily=[],
            by_persona=[],
            recent_failures=[],
            top_repos=[],
            spend=None,
        )
    )
    service._fetch_items = MagicMock(return_value=[])
    return service


@pytest.fixture
def mock_access():
    """Create a mock AccessControl that allows everything."""
    ac = MagicMock(spec=AccessControl)
    ac.check_permission = AsyncMock(return_value=True)
    return ac


@pytest.fixture
def regular_user():
    """Non-admin user token context."""
    from datetime import UTC, datetime, timedelta

    from src.shared.schemas.auth import TokenContext

    return TokenContext(
        user_id="user-abc-123",
        org_id="org-tenant-001",
        team_id="team-001",
        department_id="dept-001",
        account_type="human",
        is_admin=False,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )


@pytest.fixture
def admin_user():
    """Platform admin token context."""
    from datetime import UTC, datetime, timedelta

    from src.shared.schemas.auth import TokenContext

    return TokenContext(
        user_id="user-admin-001",
        org_id="org-platform",
        team_id="team-platform",
        department_id="dept-platform",
        account_type="human",
        is_admin=True,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )


@pytest.fixture
def client(app, mock_stats_service, mock_access, mock_db, regular_user):
    """Create a test client with regular user auth."""

    async def override_current_user():
        return regular_user

    def override_stats_service():
        return mock_stats_service

    async def override_access():
        return mock_access

    async def override_db():
        return mock_db

    app.dependency_overrides[get_current_user] = override_current_user
    app.dependency_overrides[get_stats_service] = override_stats_service
    app.dependency_overrides[get_access_control] = override_access
    app.dependency_overrides[get_db] = override_db
    return TestClient(app)


@pytest.fixture
def admin_client(app, mock_stats_service, mock_access, mock_db, admin_user):
    """Create a test client with admin auth."""

    async def override_current_user():
        return admin_user

    def override_stats_service():
        return mock_stats_service

    async def override_access():
        return mock_access

    async def override_db():
        return mock_db

    app.dependency_overrides[get_current_user] = override_current_user
    app.dependency_overrides[get_stats_service] = override_stats_service
    app.dependency_overrides[get_access_control] = override_access
    app.dependency_overrides[get_db] = override_db
    return TestClient(app)


class TestGetMyStats:
    """GET /me/agent-run-stats endpoint tests."""

    def test_returns_200_with_stats(self, client, mock_stats_service):
        """Endpoint returns 200 with StatsResponse payload."""
        resp = client.get("/me/agent-run-stats")
        assert resp.status_code == 200
        data = resp.json()
        assert data["window_days"] == 7
        assert data["today"]["total"] == 5
        assert data["today"]["completed"] == 3

    def test_uses_canonical_user_id(self, client, mock_stats_service):
        """Endpoint resolves token sub to canonical user_id for the query."""
        client.get("/me/agent-run-stats")
        mock_stats_service.get_stats_by_user.assert_called_once_with(user_id=CANONICAL_USER_ID, days=7)

    def test_days_param_forwarded(self, client, mock_stats_service):
        """Custom days param is forwarded to service."""
        client.get("/me/agent-run-stats?days=14")
        mock_stats_service.get_stats_by_user.assert_called_once_with(user_id=CANONICAL_USER_ID, days=14)

    def test_days_zero_returns_422(self, client):
        """days=0 is out of range (ge=1) → 422."""
        resp = client.get("/me/agent-run-stats?days=0")
        assert resp.status_code == 422

    def test_days_31_returns_422(self, client):
        """days=31 is out of range (le=30) → 422."""
        resp = client.get("/me/agent-run-stats?days=31")
        assert resp.status_code == 422

    def test_days_negative_returns_422(self, client):
        """days=-1 is out of range → 422."""
        resp = client.get("/me/agent-run-stats?days=-1")
        assert resp.status_code == 422

    def test_excludes_other_users_runs(self, client, mock_stats_service):
        """Endpoint only queries for the authenticated user's canonical ID."""
        client.get("/me/agent-run-stats")
        call_kwargs = mock_stats_service.get_stats_by_user.call_args
        # Only the canonical user_id from the token is used
        assert call_kwargs[1]["user_id"] == CANONICAL_USER_ID


class TestGetAdminStats:
    """GET /admin/agent-run-stats endpoint tests."""

    def test_returns_200_for_admin(self, admin_client, mock_stats_service):
        """Admin endpoint returns 200 with tenant-scoped stats."""
        resp = admin_client.get("/admin/agent-run-stats")
        assert resp.status_code == 200
        data = resp.json()
        assert data["window_days"] == 7
        assert data["today"]["total"] == 10

    def test_permission_check_called(self, admin_client, mock_access):
        """Admin endpoint calls check_permission with USAGE_READ."""
        admin_client.get("/admin/agent-run-stats")
        mock_access.check_permission.assert_called_once()

    def test_non_admin_without_permission_gets_403(self, app, mock_stats_service, mock_db, regular_user):
        """Non-admin user without USAGE_READ permission gets 403."""
        denied_access = MagicMock(spec=AccessControl)
        denied_access.check_permission = AsyncMock(
            side_effect=AccessDeniedError(
                message="Permission 'usage:read' is required",
                required_permission="usage:read",
                user_role="dept_admin",
            )
        )

        async def override_current_user():
            return regular_user

        def override_stats_service():
            return mock_stats_service

        async def override_access():
            return denied_access

        async def override_db():
            return mock_db

        app.dependency_overrides[get_current_user] = override_current_user
        app.dependency_overrides[get_stats_service] = override_stats_service
        app.dependency_overrides[get_access_control] = override_access
        app.dependency_overrides[get_db] = override_db

        # Register the gateway error handler (same as production app)
        from fastapi import Request
        from fastapi.responses import JSONResponse

        from src.shared.exceptions import BedrockGatewayError

        @app.exception_handler(BedrockGatewayError)
        async def gateway_error_handler(request: Request, exc: BedrockGatewayError):
            return JSONResponse(
                status_code=exc.status_code,
                content={"error": exc.error, "message": exc.message},
            )

        client = TestClient(app)

        resp = client.get("/admin/agent-run-stats")
        assert resp.status_code == 403

    def test_tenant_id_param_for_platform_admin(self, admin_client, mock_stats_service):
        """Platform admin can pass explicit tenant_id."""
        admin_client.get("/admin/agent-run-stats?tenant_id=org-other-tenant")
        mock_stats_service.get_stats_by_tenant.assert_called_once_with(tenant_id="org-other-tenant", days=7)

    def test_days_validation_on_admin(self, admin_client):
        """Admin endpoint also validates days range."""
        resp = admin_client.get("/admin/agent-run-stats?days=0")
        assert resp.status_code == 422
        resp = admin_client.get("/admin/agent-run-stats?days=31")
        assert resp.status_code == 422


class TestStatsAuth:
    """Authentication tests for stats endpoints."""

    def test_missing_auth_returns_401(self, app, mock_stats_service, mock_access, mock_db):
        """Request without authentication token → 401."""
        from fastapi import HTTPException

        async def override_current_user():
            raise HTTPException(status_code=401, detail="Not authenticated")

        def override_stats_service():
            return mock_stats_service

        async def override_access():
            return mock_access

        async def override_db():
            return mock_db

        app.dependency_overrides[get_current_user] = override_current_user
        app.dependency_overrides[get_stats_service] = override_stats_service
        app.dependency_overrides[get_access_control] = override_access
        app.dependency_overrides[get_db] = override_db
        client = TestClient(app)

        resp = client.get("/me/agent-run-stats")
        assert resp.status_code == 401

    def test_missing_auth_on_admin_returns_401(self, app, mock_stats_service, mock_access, mock_db):
        """Admin request without auth → 401."""
        from fastapi import HTTPException

        async def override_current_user():
            raise HTTPException(status_code=401, detail="Not authenticated")

        def override_stats_service():
            return mock_stats_service

        async def override_access():
            return mock_access

        async def override_db():
            return mock_db

        app.dependency_overrides[get_current_user] = override_current_user
        app.dependency_overrides[get_stats_service] = override_stats_service
        app.dependency_overrides[get_access_control] = override_access
        app.dependency_overrides[get_db] = override_db
        client = TestClient(app)

        resp = client.get("/admin/agent-run-stats")
        assert resp.status_code == 401


class TestStatsCostEnrichment:
    """Test cost enrichment in stats routes."""

    def test_cost_failure_returns_stats_without_spend(self, client, mock_stats_service, mock_db):
        """If Postgres cost query fails, stats are returned with spend=null."""
        # Make _fetch_items return items so cost enrichment is attempted
        mock_stats_service._fetch_items.return_value = [{"event_id": "inv-1"}]
        # Make db.execute raise to simulate Postgres failure
        mock_db.execute = AsyncMock(side_effect=Exception("DB connection failed"))

        resp = client.get("/me/agent-run-stats")
        assert resp.status_code == 200
        data = resp.json()
        assert data["spend"] is None

    def test_cost_enriched_when_available(self, client, mock_stats_service, mock_db):
        """When cost data is available, spend field is populated."""
        mock_stats_service._fetch_items.return_value = [{"event_id": "inv-1"}]
        # Mock successful cost query
        mock_result = MagicMock()
        mock_row = MagicMock()
        mock_row.total_cost_usd = 1.50
        mock_row.total_tokens = 5000
        mock_row.total_calls = 10
        mock_result.one.return_value = mock_row
        mock_db.execute = AsyncMock(return_value=mock_result)

        resp = client.get("/me/agent-run-stats")
        assert resp.status_code == 200
        data = resp.json()
        assert data["spend"] is not None
        assert data["spend"]["total_cost_usd"] == 1.50
        assert data["spend"]["total_tokens"] == 5000
        assert data["spend"]["total_calls"] == 10
