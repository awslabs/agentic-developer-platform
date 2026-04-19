"""Unit tests for Admin API routes."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from src.admin.access_control import AccessControl
from src.admin.routes import get_access_control, get_admin_service, get_current_user, router
from src.admin.schemas import OrganizationResponse, PoolAccountResponse, PoolStatusResponse
from src.admin.service import AdminService
from src.shared.exceptions import BedrockGatewayError
from src.shared.schemas.auth import TokenContext


@pytest.fixture
def app():
    """Create a test FastAPI app."""
    app = FastAPI()
    app.include_router(router)

    # Add exception handler for BedrockGatewayError (same as in app.py)
    @app.exception_handler(BedrockGatewayError)
    async def gateway_error_handler(request: Request, exc: BedrockGatewayError):
        content = {"error": exc.error, "message": exc.message}
        if exc.details:
            content["details"] = exc.details
        return JSONResponse(status_code=exc.status_code, content=content)

    return app


@pytest.fixture
def mock_admin_service():
    """Create a mock admin service."""
    service = MagicMock(spec=AdminService)
    return service


@pytest.fixture
def mock_access_control():
    """Create a mock access control."""
    ac = MagicMock(spec=AccessControl)
    ac.check_permission = AsyncMock(return_value=True)
    ac.get_accessible_organizations = AsyncMock(return_value=None)
    ac.require_platform_admin = MagicMock()
    return ac


@pytest.fixture
def platform_admin_user():
    """Create a platform admin user context."""
    return TokenContext(
        user_id="admin-user",
        org_id="platform",
        team_id="platform",
        department_id="platform",
        account_type="service",
        is_admin=True,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )


@pytest.fixture
def client(app, mock_admin_service, mock_access_control, platform_admin_user):
    """Create a test client with mocked dependencies."""

    async def override_admin_service():
        return mock_admin_service

    async def override_access_control():
        return mock_access_control

    async def override_current_user():
        return platform_admin_user

    app.dependency_overrides[get_admin_service] = override_admin_service
    app.dependency_overrides[get_access_control] = override_access_control
    app.dependency_overrides[get_current_user] = override_current_user

    return TestClient(app)


class TestOrganizationEndpoints:
    """Tests for organization endpoints."""

    def test_create_organization(self, client, mock_admin_service):
        """Test POST /admin/organizations."""
        mock_admin_service.create_organization = AsyncMock(
            return_value=OrganizationResponse(
                id="org-new",
                name="New Org",
                aws_accounts=["123456789012"],
                role_mappings={},
                settings={},
                created_at=datetime.now(UTC),
            )
        )

        response = client.post(
            "/admin/organizations",
            json={"name": "New Org", "aws_accounts": ["123456789012"]},
        )

        assert response.status_code == 201
        data = response.json()
        assert data["name"] == "New Org"
        assert data["id"] == "org-new"

    def test_list_organizations(self, client, mock_admin_service):
        """Test GET /admin/organizations."""
        mock_admin_service.list_organizations = AsyncMock(
            return_value=(
                [
                    OrganizationResponse(
                        id="org-1",
                        name="Org 1",
                        aws_accounts=[],
                        role_mappings={},
                        settings={},
                        created_at=datetime.now(UTC),
                    ),
                    OrganizationResponse(
                        id="org-2",
                        name="Org 2",
                        aws_accounts=[],
                        role_mappings={},
                        settings={},
                        created_at=datetime.now(UTC),
                    ),
                ],
                2,
            )
        )

        response = client.get("/admin/organizations")

        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 2
        assert len(data["items"]) == 2

    def test_get_organization(self, client, mock_admin_service):
        """Test GET /admin/organizations/{org_id}."""
        mock_admin_service.get_organization = AsyncMock(
            return_value=OrganizationResponse(
                id="org-1",
                name="Test Org",
                aws_accounts=["111111111111"],
                role_mappings={"admin": "role-id"},
                settings={"feature": True},
                created_at=datetime.now(UTC),
            )
        )

        response = client.get("/admin/organizations/org-1")

        assert response.status_code == 200
        data = response.json()
        assert data["id"] == "org-1"
        assert data["name"] == "Test Org"

    def test_update_organization(self, client, mock_admin_service):
        """Test PUT /admin/organizations/{org_id}."""
        mock_admin_service.update_organization = AsyncMock(
            return_value=OrganizationResponse(
                id="org-1",
                name="Updated Org",
                aws_accounts=[],
                role_mappings={},
                settings={},
                created_at=datetime.now(UTC),
            )
        )

        response = client.put(
            "/admin/organizations/org-1",
            json={"name": "Updated Org"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "Updated Org"

    def test_delete_organization(self, client, mock_admin_service):
        """Test DELETE /admin/organizations/{org_id}."""
        mock_admin_service.delete_organization = AsyncMock(return_value=True)

        response = client.delete("/admin/organizations/org-1")

        assert response.status_code == 204


class TestPoolEndpoints:
    """Tests for pool management endpoints."""

    def test_get_pool_status(self, client, mock_admin_service):
        """Test GET /admin/pool/status."""
        mock_admin_service.get_pool_status = AsyncMock(
            return_value=PoolStatusResponse(
                total_accounts=3,
                healthy_accounts=2,
                unhealthy_accounts=1,
                accounts=[
                    PoolAccountResponse(
                        id="pool-1",
                        account_id="111111111111",
                        role_arn="arn:aws:iam::111111111111:role/Role",
                        region="us-east-1",
                        is_healthy=True,
                        last_health_check=datetime.now(UTC),
                        created_at=datetime.now(UTC),
                    )
                ],
            )
        )

        response = client.get("/admin/pool/status")

        assert response.status_code == 200
        data = response.json()
        assert data["total_accounts"] == 3
        assert data["healthy_accounts"] == 2

    def test_add_pool_account(self, client, mock_admin_service):
        """Test POST /admin/pool/accounts."""
        mock_admin_service.add_pool_account = AsyncMock(
            return_value=PoolAccountResponse(
                id="pool-new",
                account_id="999999999999",
                role_arn="arn:aws:iam::999999999999:role/BedrockRole",
                region="us-west-2",
                is_healthy=True,
                last_health_check=None,
                created_at=datetime.now(UTC),
            )
        )

        response = client.post(
            "/admin/pool/accounts",
            json={
                "account_id": "999999999999",
                "role_arn": "arn:aws:iam::999999999999:role/BedrockRole",
                "region": "us-west-2",
            },
        )

        assert response.status_code == 201
        data = response.json()
        assert data["account_id"] == "999999999999"

    def test_delete_pool_account(self, client, mock_admin_service):
        """Test DELETE /admin/pool/accounts/{account_id}."""
        mock_admin_service.remove_pool_account = AsyncMock(return_value=True)

        response = client.delete("/admin/pool/accounts/pool-1")

        assert response.status_code == 204


class TestBudgetConfigEndpoints:
    """Tests for budget configuration endpoints."""

    def test_get_budget_config(self, client, mock_admin_service):
        """Test GET /admin/organizations/{org_id}/budget/{entity_type}/{entity_id}."""
        mock_admin_service.get_budget_config = AsyncMock(return_value=None)

        response = client.get("/admin/organizations/org-1/budget/org/org-1")

        assert response.status_code == 200

    def test_update_budget_config(self, client, mock_admin_service):
        """Test PUT /admin/organizations/{org_id}/budget/{entity_type}/{entity_id}."""
        mock_admin_service.update_budget_config = AsyncMock(return_value=None)

        response = client.put(
            "/admin/organizations/org-1/budget/org/org-1",
            json={"budget_amount_usd": 1000.00},
        )

        assert response.status_code == 200


class TestRateLimitConfigEndpoints:
    """Tests for rate limit configuration endpoints."""

    def test_get_ratelimit_config(self, client, mock_admin_service):
        """Test GET /admin/organizations/{org_id}/ratelimit/{entity_type}/{entity_id}."""
        mock_admin_service.get_ratelimit_config = AsyncMock(return_value=None)

        response = client.get("/admin/organizations/org-1/ratelimit/org/org-1")

        assert response.status_code == 200

    def test_update_ratelimit_config(self, client, mock_admin_service):
        """Test PUT /admin/organizations/{org_id}/ratelimit/{entity_type}/{entity_id}."""
        from src.admin.schemas import RateLimitConfigResponse

        mock_admin_service.update_ratelimit_config = AsyncMock(
            return_value=RateLimitConfigResponse(
                org_id="org-1",
                entity_type="org",
                entity_id="org-1",
                rpm=100,
                tpm=10000,
                concurrent_requests=10,
                updated_at=datetime.now(UTC),
            )
        )

        response = client.put(
            "/admin/organizations/org-1/ratelimit/org/org-1",
            json={"rpm": 100, "tpm": 10000},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["rpm"] == 100


class TestDashboardEndpoints:
    """Tests for dashboard endpoints."""

    def test_get_platform_dashboard(self, client, mock_admin_service):
        """Test GET /admin/dashboard/platform."""
        mock_admin_service.get_pool_status = AsyncMock(
            return_value=PoolStatusResponse(
                total_accounts=2,
                healthy_accounts=2,
                unhealthy_accounts=0,
                accounts=[],
            )
        )

        response = client.get("/admin/dashboard/platform")

        assert response.status_code == 200
        data = response.json()
        assert "pool_status" in data

    def test_get_org_dashboard(self, client, mock_admin_service):
        """Test GET /admin/dashboard/org/{org_id}."""
        mock_admin_service.get_organization = AsyncMock(
            return_value=OrganizationResponse(
                id="org-1",
                name="Test Org",
                aws_accounts=[],
                role_mappings={},
                settings={},
                created_at=datetime.now(UTC),
            )
        )

        response = client.get("/admin/dashboard/org/org-1")

        assert response.status_code == 200
        data = response.json()
        assert data["org_id"] == "org-1"


class TestBudgetListCreateDeleteEndpoints:
    """Tests for budget list/create/delete endpoints (Issue #185)."""

    def test_list_budgets(self, client, mock_admin_service):
        """Test GET /admin/organizations/{org_id}/budgets."""
        from src.admin.schemas import BudgetListItem, BudgetListResponse

        mock_admin_service.get_budgets_list = AsyncMock(
            return_value=BudgetListResponse(
                items=[
                    BudgetListItem(
                        entity_type="team",
                        entity_id="team-1",
                        period_type="monthly",
                        budget_amount_usd=1000.00,
                        enforcement_mode="hard",
                        current_usage_usd=250.00,
                        utilization_pct=25.0,
                        updated_at=datetime.now(UTC),
                    )
                ],
                total=1,
                page=1,
                page_size=20,
                has_more=False,
            )
        )

        response = client.get("/admin/organizations/org-1/budgets")

        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 1
        assert len(data["items"]) == 1
        assert data["items"][0]["entity_type"] == "team"

    def test_create_budget(self, client, mock_admin_service):
        """Test POST /admin/organizations/{org_id}/budgets."""
        from src.admin.schemas import BudgetConfigResponse

        mock_admin_service.create_budget = AsyncMock(
            return_value=BudgetConfigResponse(
                org_id="org-1",
                entity_type="team",
                entity_id="team-new",
                period_type="monthly",
                budget_amount_usd=500.00,
                enforcement_mode="soft",
                updated_at=datetime.now(UTC),
            )
        )

        response = client.post(
            "/admin/organizations/org-1/budgets",
            json={
                "entity_type": "team",
                "entity_id": "team-new",
                "period_type": "monthly",
                "budget_amount_usd": 500.00,
                "enforcement_mode": "soft",
            },
        )

        assert response.status_code == 201
        data = response.json()
        assert data["entity_type"] == "team"
        # Handle both float and string serialization (depends on Decimal serialization)
        assert float(data["budget_amount_usd"]) == 500.00

    def test_delete_budget(self, client, mock_admin_service):
        """Test DELETE /admin/organizations/{org_id}/budget/{entity_type}/{entity_id}/{period_type}."""
        mock_admin_service.delete_budget = AsyncMock(return_value=None)

        response = client.delete("/admin/organizations/org-1/budget/team/team-1/monthly")

        assert response.status_code == 204


class TestRateLimitListCreateDeleteEndpoints:
    """Tests for rate limit list/create/delete endpoints (Issue #185)."""

    def test_list_ratelimits(self, client, mock_admin_service):
        """Test GET /admin/organizations/{org_id}/ratelimits."""
        from src.admin.schemas import RateLimitListItem, RateLimitListResponse

        mock_admin_service.get_ratelimits_list = AsyncMock(
            return_value=RateLimitListResponse(
                items=[
                    RateLimitListItem(
                        entity_type="team",
                        entity_id="team-1",
                        rpm=60,
                        tpm=10000,
                        concurrent_requests=5,
                        updated_at=datetime.now(UTC),
                    )
                ],
                total=1,
                page=1,
                page_size=20,
                has_more=False,
            )
        )

        response = client.get("/admin/organizations/org-1/ratelimits")

        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 1
        assert len(data["items"]) == 1
        assert data["items"][0]["rpm"] == 60

    def test_create_ratelimit(self, client, mock_admin_service):
        """Test POST /admin/organizations/{org_id}/ratelimits."""
        from src.admin.schemas import RateLimitConfigResponse

        mock_admin_service.create_ratelimit = AsyncMock(
            return_value=RateLimitConfigResponse(
                org_id="org-1",
                entity_type="team",
                entity_id="team-new",
                rpm=100,
                tpm=20000,
                concurrent_requests=10,
                updated_at=datetime.now(UTC),
            )
        )

        response = client.post(
            "/admin/organizations/org-1/ratelimits",
            json={
                "entity_type": "team",
                "entity_id": "team-new",
                "rpm": 100,
                "tpm": 20000,
                "concurrent_requests": 10,
            },
        )

        assert response.status_code == 201
        data = response.json()
        assert data["entity_type"] == "team"
        assert data["rpm"] == 100

    def test_delete_ratelimit(self, client, mock_admin_service):
        """Test DELETE /admin/organizations/{org_id}/ratelimit/{entity_type}/{entity_id}."""
        mock_admin_service.delete_ratelimit = AsyncMock(return_value=None)

        response = client.delete("/admin/organizations/org-1/ratelimit/team/team-1")

        assert response.status_code == 204


class TestValidation:
    """Tests for request validation."""

    def test_create_organization_invalid_name(self, client, mock_admin_service):
        """Test POST /admin/organizations with empty name."""
        response = client.post(
            "/admin/organizations",
            json={"name": ""},  # Empty name
        )

        assert response.status_code == 422  # Validation error

    def test_add_pool_account_invalid_account_id(self, client, mock_admin_service):
        """Test POST /admin/pool/accounts with invalid account ID."""
        response = client.post(
            "/admin/pool/accounts",
            json={
                "account_id": "invalid",  # Not 12 digits
                "role_arn": "arn:aws:iam::999999999999:role/Role",
            },
        )

        assert response.status_code == 422

    def test_add_pool_account_invalid_role_arn(self, client, mock_admin_service):
        """Test POST /admin/pool/accounts with invalid role ARN."""
        response = client.post(
            "/admin/pool/accounts",
            json={
                "account_id": "999999999999",
                "role_arn": "invalid-arn",  # Invalid ARN format
            },
        )

        assert response.status_code == 422

    def test_pagination_invalid_page(self, client, mock_admin_service):
        """Test pagination with invalid page number."""
        response = client.get("/admin/organizations?page=0")  # Page must be >= 1

        assert response.status_code == 422

    def test_pagination_invalid_page_size(self, client, mock_admin_service):
        """Test pagination with invalid page size."""
        response = client.get("/admin/organizations?page_size=500")  # Max is 100

        assert response.status_code == 422


# =============================================================================
# Issue #179: Tests for new endpoints
# =============================================================================


class TestUserRolesEndpoint:
    """Tests for GET /admin/users/roles endpoint (Issue #179)."""

    def test_get_available_roles(self, client):
        """Test GET /admin/users/roles returns static role list."""
        response = client.get("/admin/users/roles")

        assert response.status_code == 200
        data = response.json()
        assert "roles" in data
        assert isinstance(data["roles"], list)
        assert "platform_admin" in data["roles"]
        assert "org_admin" in data["roles"]
        assert "user" in data["roles"]
        assert "service_account" in data["roles"]


class TestUsageTimeseriesEndpoint:
    """Tests for GET /admin/organizations/{org_id}/usage/timeseries endpoint (Issue #179)."""

    def test_get_usage_timeseries(self, client, mock_admin_service):
        """Test GET /admin/organizations/{org_id}/usage/timeseries."""
        from decimal import Decimal

        mock_admin_service.get_usage_timeseries = AsyncMock(
            return_value=[
                {
                    "date": "2026-02-19",
                    "input_tokens": 15000,
                    "output_tokens": 8000,
                    "cost_usd": Decimal("0.45"),
                    "request_count": 120,
                },
                {
                    "date": "2026-02-20",
                    "input_tokens": 12000,
                    "output_tokens": 6500,
                    "cost_usd": Decimal("0.38"),
                    "request_count": 95,
                },
            ]
        )

        response = client.get("/admin/organizations/org-1/usage/timeseries?period=daily&start=2026-02-19&end=2026-02-20")

        assert response.status_code == 200
        data = response.json()
        assert "data" in data
        assert len(data["data"]) == 2
        assert data["period"] == "daily"
        assert data["org_id"] == "org-1"
        assert data["data"][0]["date"] == "2026-02-19"
        assert data["data"][0]["input_tokens"] == 15000

    def test_get_usage_timeseries_default_params(self, client, mock_admin_service):
        """Test GET /admin/organizations/{org_id}/usage/timeseries with default params."""
        mock_admin_service.get_usage_timeseries = AsyncMock(return_value=[])

        response = client.get("/admin/organizations/org-1/usage/timeseries")

        assert response.status_code == 200
        data = response.json()
        assert data["period"] == "daily"  # Default period


class TestMyChatsEndpoints:
    """Tests for /admin/users/me/chats endpoints (Issue #179)."""

    def test_get_my_chats(self, client, mock_admin_service):
        """Test GET /admin/users/me/chats."""
        from decimal import Decimal

        mock_admin_service.get_user_chats = AsyncMock(
            return_value=(
                [
                    {
                        "request_id": "req-123",
                        "timestamp": datetime.now(UTC),
                        "model": "global.anthropic.claude-opus-4-6-v1",
                        "input_tokens": 1500,
                        "output_tokens": 800,
                        "cost_usd": Decimal("0.045"),
                        "first_message_preview": None,
                        "stop_reason": None,
                    },
                ],
                1,
            )
        )

        response = client.get("/admin/users/me/chats")

        assert response.status_code == 200
        data = response.json()
        assert "chats" in data
        assert len(data["chats"]) == 1
        assert data["total"] == 1
        assert data["page"] == 1
        assert data["chats"][0]["request_id"] == "req-123"
        assert data["chats"][0]["model"] == "global.anthropic.claude-opus-4-6-v1"

    def test_get_my_chats_with_filters(self, client, mock_admin_service):
        """Test GET /admin/users/me/chats with filters."""
        mock_admin_service.get_user_chats = AsyncMock(return_value=([], 0))

        response = client.get("/admin/users/me/chats?model=claude&start_date=2026-02-01&end_date=2026-02-20")

        assert response.status_code == 200
        # Verify filters were passed to service
        mock_admin_service.get_user_chats.assert_called_once()
        call_kwargs = mock_admin_service.get_user_chats.call_args.kwargs
        assert call_kwargs["model_filter"] == "claude"
        assert call_kwargs["start_date"] == "2026-02-01"
        assert call_kwargs["end_date"] == "2026-02-20"

    def test_get_my_chats_pagination(self, client, mock_admin_service):
        """Test GET /admin/users/me/chats pagination."""
        mock_admin_service.get_user_chats = AsyncMock(return_value=([], 0))

        response = client.get("/admin/users/me/chats?page=2&limit=50")

        assert response.status_code == 200
        call_kwargs = mock_admin_service.get_user_chats.call_args.kwargs
        assert call_kwargs["page"] == 2
        assert call_kwargs["limit"] == 50

    def test_get_chat_detail(self, client, mock_admin_service):
        """Test GET /admin/users/me/chats/{request_id}."""
        from decimal import Decimal

        mock_admin_service.get_chat_detail = AsyncMock(
            return_value={
                "request_id": "req-123",
                "timestamp": datetime.now(UTC),
                "model": "global.anthropic.claude-opus-4-6-v1",
                "input_tokens": 1500,
                "output_tokens": 800,
                "cost_usd": Decimal("0.045"),
                "latency_ms": 2500,
                "status_code": 200,
                "stop_reason": "end_turn",
                "request_messages": None,
                "response_content": None,
                "chat_logging_available": False,
            }
        )

        response = client.get("/admin/users/me/chats/req-123")

        assert response.status_code == 200
        data = response.json()
        assert data["request_id"] == "req-123"
        assert data["latency_ms"] == 2500
        assert data["status_code"] == 200
        assert data["chat_logging_available"] is False

    def test_get_chat_detail_not_found(self, client, mock_admin_service):
        """Test GET /admin/users/me/chats/{request_id} for non-existent chat."""
        mock_admin_service.get_chat_detail = AsyncMock(return_value=None)

        response = client.get("/admin/users/me/chats/non-existent")

        assert response.status_code == 404

    def test_get_chat_detail_with_chat_logging(self, client, mock_admin_service):
        """Test GET /admin/users/me/chats/{request_id} with full chat content."""
        from decimal import Decimal

        mock_admin_service.get_chat_detail = AsyncMock(
            return_value={
                "request_id": "req-456",
                "timestamp": datetime.now(UTC),
                "model": "global.anthropic.claude-opus-4-6-v1",
                "input_tokens": 1500,
                "output_tokens": 800,
                "cost_usd": Decimal("0.045"),
                "latency_ms": 2500,
                "status_code": 200,
                "stop_reason": "end_turn",
                "request_messages": [{"role": "user", "content": "Hello"}],
                "response_content": "Hi there!",
                "chat_logging_available": True,
            }
        )

        response = client.get("/admin/users/me/chats/req-456")

        assert response.status_code == 200
        data = response.json()
        assert data["chat_logging_available"] is True
        assert data["request_messages"] == [{"role": "user", "content": "Hello"}]
        assert data["response_content"] == "Hi there!"


# =============================================================================
# Issue #226: Tests for Cognito-backed Entity List Endpoints
# =============================================================================


class TestCognitoUserListEndpoint:
    """Tests for GET /admin/organizations/{org_id}/cognito/users endpoint (Issue #226)."""

    @pytest.fixture
    def mock_cognito_service(self):
        """Create a mock Cognito service."""
        from unittest.mock import MagicMock

        from src.admin.cognito_service import CognitoService

        service = MagicMock(spec=CognitoService)
        return service

    @pytest.fixture
    def client_with_cognito(self, app, mock_admin_service, mock_access_control, platform_admin_user, mock_cognito_service):
        """Create a test client with mocked Cognito service."""
        from src.admin.routes import get_cognito_service

        async def override_admin_service():
            return mock_admin_service

        async def override_access_control():
            return mock_access_control

        async def override_current_user():
            return platform_admin_user

        def override_cognito_service():
            return mock_cognito_service

        app.dependency_overrides[get_admin_service] = override_admin_service
        app.dependency_overrides[get_access_control] = override_access_control
        app.dependency_overrides[get_current_user] = override_current_user
        app.dependency_overrides[get_cognito_service] = override_cognito_service

        return TestClient(app)

    def test_list_cognito_users(self, client_with_cognito, mock_cognito_service):
        """Test GET /admin/organizations/{org_id}/cognito/users."""
        mock_cognito_service.list_users_by_org.return_value = (
            [
                {
                    "Username": "user1@example.com",
                    "Attributes": [
                        {"Name": "email", "Value": "user1@example.com"},
                        {"Name": "custom:org_id", "Value": "org-1"},
                        {"Name": "custom:department_id", "Value": "engineering"},
                        {"Name": "custom:team_id", "Value": "platform"},
                        {"Name": "custom:role", "Value": "user"},
                    ],
                    "UserStatus": "CONFIRMED",
                    "Enabled": True,
                    "UserCreateDate": datetime.now(UTC),
                    "UserLastModifiedDate": datetime.now(UTC),
                },
            ],
            1,
        )

        response = client_with_cognito.get("/admin/organizations/org-1/cognito/users")

        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 1
        assert len(data["items"]) == 1
        assert data["items"][0]["username"] == "user1@example.com"
        assert data["items"][0]["email"] == "user1@example.com"
        assert data["items"][0]["org_id"] == "org-1"
        assert data["items"][0]["department_id"] == "engineering"

    def test_list_cognito_users_pagination(self, client_with_cognito, mock_cognito_service):
        """Test GET /admin/organizations/{org_id}/cognito/users with pagination."""
        mock_cognito_service.list_users_by_org.return_value = ([], 0)

        response = client_with_cognito.get("/admin/organizations/org-1/cognito/users?page=2&page_size=10")

        assert response.status_code == 200
        mock_cognito_service.list_users_by_org.assert_called_once_with("org-1", 2, 10)

    def test_list_cognito_users_not_configured(self, app, mock_admin_service, mock_access_control, platform_admin_user):
        """Test GET /admin/organizations/{org_id}/cognito/users when Cognito not configured."""
        from src.admin.routes import get_cognito_service

        async def override_admin_service():
            return mock_admin_service

        async def override_access_control():
            return mock_access_control

        async def override_current_user():
            return platform_admin_user

        def override_cognito_service():
            return None  # Cognito not configured

        app.dependency_overrides[get_admin_service] = override_admin_service
        app.dependency_overrides[get_access_control] = override_access_control
        app.dependency_overrides[get_current_user] = override_current_user
        app.dependency_overrides[get_cognito_service] = override_cognito_service

        client = TestClient(app)
        response = client.get("/admin/organizations/org-1/cognito/users")

        assert response.status_code == 503
        data = response.json()
        assert data["error"] == "cognito_not_configured"


class TestCognitoTeamListEndpoint:
    """Tests for GET /admin/organizations/{org_id}/cognito/teams endpoint (Issue #226)."""

    @pytest.fixture
    def mock_cognito_service(self):
        """Create a mock Cognito service."""
        from unittest.mock import MagicMock

        from src.admin.cognito_service import CognitoService

        service = MagicMock(spec=CognitoService)
        return service

    @pytest.fixture
    def client_with_cognito(self, app, mock_admin_service, mock_access_control, platform_admin_user, mock_cognito_service):
        """Create a test client with mocked Cognito service."""
        from src.admin.routes import get_cognito_service

        async def override_admin_service():
            return mock_admin_service

        async def override_access_control():
            return mock_access_control

        async def override_current_user():
            return platform_admin_user

        def override_cognito_service():
            return mock_cognito_service

        app.dependency_overrides[get_admin_service] = override_admin_service
        app.dependency_overrides[get_access_control] = override_access_control
        app.dependency_overrides[get_current_user] = override_current_user
        app.dependency_overrides[get_cognito_service] = override_cognito_service

        return TestClient(app)

    def test_list_cognito_teams(self, client_with_cognito, mock_cognito_service):
        """Test GET /admin/organizations/{org_id}/cognito/teams.

        Issue #226: Teams are now derived from unique custom:team_id user
        attributes via get_unique_teams(), not from Cognito groups.
        """
        mock_cognito_service.get_unique_teams.return_value = [
            "org-org-1-team-platform",
            "org-org-1-team-devops",
        ]

        response = client_with_cognito.get("/admin/organizations/org-1/cognito/teams")

        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 2
        assert len(data["items"]) == 2
        assert data["items"][0]["group_name"] == "org-org-1-team-platform"
        assert data["items"][1]["group_name"] == "org-org-1-team-devops"
        mock_cognito_service.get_unique_teams.assert_called_once_with("org-1")

    def test_list_cognito_teams_with_prefix(self, client_with_cognito, mock_cognito_service):
        """Test GET /admin/organizations/{org_id}/cognito/teams with prefix filter.

        Issue #226: Teams are derived from user attributes. The prefix query
        parameter is no longer used for list_groups; get_unique_teams is called.
        """
        mock_cognito_service.get_unique_teams.return_value = []

        response = client_with_cognito.get("/admin/organizations/org-1/cognito/teams?prefix=team-")

        assert response.status_code == 200
        mock_cognito_service.get_unique_teams.assert_called_once_with("org-1")


class TestCognitoDepartmentListEndpoint:
    """Tests for GET /admin/organizations/{org_id}/cognito/departments endpoint (Issue #226)."""

    @pytest.fixture
    def mock_cognito_service(self):
        """Create a mock Cognito service."""
        from unittest.mock import MagicMock

        from src.admin.cognito_service import CognitoService

        service = MagicMock(spec=CognitoService)
        return service

    @pytest.fixture
    def client_with_cognito(self, app, mock_admin_service, mock_access_control, platform_admin_user, mock_cognito_service):
        """Create a test client with mocked Cognito service."""
        from src.admin.routes import get_cognito_service

        async def override_admin_service():
            return mock_admin_service

        async def override_access_control():
            return mock_access_control

        async def override_current_user():
            return platform_admin_user

        def override_cognito_service():
            return mock_cognito_service

        app.dependency_overrides[get_admin_service] = override_admin_service
        app.dependency_overrides[get_access_control] = override_access_control
        app.dependency_overrides[get_current_user] = override_current_user
        app.dependency_overrides[get_cognito_service] = override_cognito_service

        return TestClient(app)

    def test_list_cognito_departments(self, client_with_cognito, mock_cognito_service):
        """Test GET /admin/organizations/{org_id}/cognito/departments."""
        mock_cognito_service.get_unique_departments.return_value = [
            "engineering",
            "marketing",
            "sales",
        ]

        response = client_with_cognito.get("/admin/organizations/org-1/cognito/departments")

        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 3
        assert len(data["items"]) == 3
        assert data["items"][0]["department_id"] == "engineering"
        assert data["items"][1]["department_id"] == "marketing"
        assert data["items"][2]["department_id"] == "sales"

    def test_list_cognito_departments_empty(self, client_with_cognito, mock_cognito_service):
        """Test GET /admin/organizations/{org_id}/cognito/departments when empty."""
        mock_cognito_service.get_unique_departments.return_value = []

        response = client_with_cognito.get("/admin/organizations/org-1/cognito/departments")

        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 0
        assert len(data["items"]) == 0
