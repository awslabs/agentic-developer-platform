"""Integration tests for onboarding routes (departments, teams, users, service accounts)."""

from datetime import datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import ASGITransport, AsyncClient

from src.admin.routes import (
    get_access_control,
    get_admin_service,
    get_cognito_service,
    get_current_user,
    router,
)
from src.shared.schemas.auth import TokenContext


# Create a minimal FastAPI app for testing
@pytest.fixture
def test_app():
    """Create a test FastAPI application."""
    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(router)
    return app


@pytest.fixture
def admin_user():
    """Create an admin user context."""
    return TokenContext(
        user_id="admin-user",
        org_id="test-org",
        team_id="test-team",
        department_id="test-dept",
        account_type="human",
        is_admin=True,
        expires_at=datetime.now(),
    )


@pytest.fixture
def non_admin_user():
    """Create a non-admin user context."""
    return TokenContext(
        user_id="regular-user",
        org_id="test-org",
        team_id="test-team",
        department_id="test-dept",
        account_type="human",
        is_admin=False,
        expires_at=datetime.now(),
    )


@pytest.fixture
def mock_db_session():
    """Create a mock database session."""
    session = AsyncMock()
    return session


@pytest.fixture
def mock_admin_service():
    """Create a mock admin service."""
    service = MagicMock()
    return service


@pytest.fixture
def mock_access_control():
    """Create a mock access control."""
    access = AsyncMock()
    access.check_permission = AsyncMock(return_value=None)
    access.get_accessible_organizations = AsyncMock(return_value=None)
    return access


class TestDepartmentEndpoints:
    """Tests for department CRUD endpoints."""

    @pytest.mark.asyncio
    async def test_create_department(self, test_app, mock_admin_service, mock_access_control, admin_user):
        """Test creating a department."""
        from src.shared.schemas.admin import DepartmentResponse

        # Mock the service response
        mock_response = DepartmentResponse(
            id="dept-123",
            org_id="test-org",
            name="Engineering",
            budget_limit=Decimal("10000.00"),
            description="Engineering department",
            cognito_group_name="dept-dept-123",
            created_at=datetime.now(),
            updated_at=None,
        )

        mock_admin_service.create_department = AsyncMock(return_value=mock_response)

        # Override dependencies
        test_app.dependency_overrides[get_admin_service] = lambda: mock_admin_service
        test_app.dependency_overrides[get_access_control] = lambda: mock_access_control
        test_app.dependency_overrides[get_current_user] = lambda: admin_user
        test_app.dependency_overrides[get_cognito_service] = lambda: None

        try:
            async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as client:
                response = await client.post(
                    "/admin/organizations/test-org/departments",
                    json={
                        "name": "Engineering",
                        "budget_limit": 10000.00,
                        "description": "Engineering department",
                    },
                )

            assert response.status_code == 201
            data = response.json()
            assert data["name"] == "Engineering"
            assert data["org_id"] == "test-org"
        finally:
            test_app.dependency_overrides.clear()

    @pytest.mark.asyncio
    async def test_list_departments(self, test_app, mock_admin_service, mock_access_control, admin_user):
        """Test listing departments."""
        from src.shared.schemas.admin import DepartmentResponse

        mock_depts = [
            DepartmentResponse(
                id="dept-1",
                org_id="test-org",
                name="Engineering",
                created_at=datetime.now(),
            ),
            DepartmentResponse(
                id="dept-2",
                org_id="test-org",
                name="Sales",
                created_at=datetime.now(),
            ),
        ]

        mock_admin_service.list_departments = AsyncMock(return_value=(mock_depts, 2))

        test_app.dependency_overrides[get_admin_service] = lambda: mock_admin_service
        test_app.dependency_overrides[get_access_control] = lambda: mock_access_control
        test_app.dependency_overrides[get_current_user] = lambda: admin_user

        try:
            async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as client:
                response = await client.get("/admin/organizations/test-org/departments")

            assert response.status_code == 200
            data = response.json()
            assert data["total"] == 2
            assert len(data["items"]) == 2
        finally:
            test_app.dependency_overrides.clear()

    @pytest.mark.asyncio
    async def test_get_department(self, test_app, mock_admin_service, mock_access_control, admin_user):
        """Test getting a specific department."""
        from src.shared.schemas.admin import DepartmentResponse

        mock_dept = DepartmentResponse(
            id="dept-123",
            org_id="test-org",
            name="Engineering",
            created_at=datetime.now(),
        )

        mock_admin_service.get_department = AsyncMock(return_value=mock_dept)

        test_app.dependency_overrides[get_admin_service] = lambda: mock_admin_service
        test_app.dependency_overrides[get_access_control] = lambda: mock_access_control
        test_app.dependency_overrides[get_current_user] = lambda: admin_user

        try:
            async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as client:
                response = await client.get("/admin/organizations/test-org/departments/dept-123")

            assert response.status_code == 200
            data = response.json()
            assert data["id"] == "dept-123"
        finally:
            test_app.dependency_overrides.clear()

    @pytest.mark.asyncio
    async def test_update_department(self, test_app, mock_admin_service, mock_access_control, admin_user):
        """Test updating a department."""
        from src.shared.schemas.admin import DepartmentResponse

        mock_dept = DepartmentResponse(
            id="dept-123",
            org_id="test-org",
            name="Updated Engineering",
            created_at=datetime.now(),
        )

        mock_admin_service.update_department = AsyncMock(return_value=mock_dept)

        test_app.dependency_overrides[get_admin_service] = lambda: mock_admin_service
        test_app.dependency_overrides[get_access_control] = lambda: mock_access_control
        test_app.dependency_overrides[get_current_user] = lambda: admin_user

        try:
            async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as client:
                response = await client.put(
                    "/admin/organizations/test-org/departments/dept-123",
                    json={"name": "Updated Engineering"},
                )

            assert response.status_code == 200
            data = response.json()
            assert data["name"] == "Updated Engineering"
        finally:
            test_app.dependency_overrides.clear()

    @pytest.mark.asyncio
    async def test_delete_department(self, test_app, mock_admin_service, mock_access_control, admin_user):
        """Test deleting a department."""
        mock_admin_service.delete_department = AsyncMock(return_value=True)

        test_app.dependency_overrides[get_admin_service] = lambda: mock_admin_service
        test_app.dependency_overrides[get_access_control] = lambda: mock_access_control
        test_app.dependency_overrides[get_current_user] = lambda: admin_user
        test_app.dependency_overrides[get_cognito_service] = lambda: None

        try:
            async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as client:
                response = await client.delete("/admin/organizations/test-org/departments/dept-123")

            assert response.status_code == 204
        finally:
            test_app.dependency_overrides.clear()


class TestTeamEndpoints:
    """Tests for team CRUD endpoints."""

    @pytest.mark.asyncio
    async def test_create_team(self, test_app, mock_admin_service, mock_access_control, admin_user):
        """Test creating a team."""
        from src.shared.schemas.admin import TeamResponse

        mock_team = TeamResponse(
            id="team-123",
            org_id="test-org",
            department_id="dept-123",
            name="Backend Team",
            description="Backend developers",
            created_at=datetime.now(),
        )

        mock_admin_service.create_team = AsyncMock(return_value=mock_team)

        test_app.dependency_overrides[get_admin_service] = lambda: mock_admin_service
        test_app.dependency_overrides[get_access_control] = lambda: mock_access_control
        test_app.dependency_overrides[get_current_user] = lambda: admin_user

        try:
            async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as client:
                response = await client.post(
                    "/admin/organizations/test-org/departments/dept-123/teams",
                    json={
                        "name": "Backend Team",
                        "description": "Backend developers",
                    },
                )

            assert response.status_code == 201
            data = response.json()
            assert data["name"] == "Backend Team"
        finally:
            test_app.dependency_overrides.clear()

    @pytest.mark.asyncio
    async def test_list_teams(self, test_app, mock_admin_service, mock_access_control, admin_user):
        """Test listing teams."""
        from src.shared.schemas.admin import TeamResponse

        mock_teams = [
            TeamResponse(
                id="team-1",
                org_id="test-org",
                department_id="dept-123",
                name="Backend",
                created_at=datetime.now(),
            ),
        ]

        mock_admin_service.list_teams = AsyncMock(return_value=(mock_teams, 1))

        test_app.dependency_overrides[get_admin_service] = lambda: mock_admin_service
        test_app.dependency_overrides[get_access_control] = lambda: mock_access_control
        test_app.dependency_overrides[get_current_user] = lambda: admin_user

        try:
            async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as client:
                response = await client.get("/admin/organizations/test-org/departments/dept-123/teams")

            assert response.status_code == 200
            data = response.json()
            assert data["total"] == 1
        finally:
            test_app.dependency_overrides.clear()

    @pytest.mark.asyncio
    async def test_update_team(self, test_app, mock_admin_service, mock_access_control, admin_user):
        """Test updating a team."""
        from src.shared.schemas.admin import TeamResponse

        mock_team = TeamResponse(
            id="team-123",
            org_id="test-org",
            department_id="dept-123",
            name="Updated Team",
            created_at=datetime.now(),
        )

        mock_admin_service.update_team = AsyncMock(return_value=mock_team)

        test_app.dependency_overrides[get_admin_service] = lambda: mock_admin_service
        test_app.dependency_overrides[get_access_control] = lambda: mock_access_control
        test_app.dependency_overrides[get_current_user] = lambda: admin_user

        try:
            async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as client:
                response = await client.put(
                    "/admin/organizations/test-org/teams/team-123",
                    json={"name": "Updated Team"},
                )

            assert response.status_code == 200
        finally:
            test_app.dependency_overrides.clear()

    @pytest.mark.asyncio
    async def test_delete_team(self, test_app, mock_admin_service, mock_access_control, admin_user):
        """Test deleting a team."""
        mock_admin_service.delete_team = AsyncMock(return_value=True)

        test_app.dependency_overrides[get_admin_service] = lambda: mock_admin_service
        test_app.dependency_overrides[get_access_control] = lambda: mock_access_control
        test_app.dependency_overrides[get_current_user] = lambda: admin_user

        try:
            async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as client:
                response = await client.delete("/admin/organizations/test-org/teams/team-123")

            assert response.status_code == 204
        finally:
            test_app.dependency_overrides.clear()


class TestUserEndpoints:
    """Tests for user management endpoints."""

    @pytest.mark.asyncio
    async def test_add_user(self, test_app, mock_admin_service, mock_access_control, admin_user):
        """Test adding a user."""
        from src.shared.schemas.admin import UserResponse

        mock_user = UserResponse(
            id="user-123",
            org_id="test-org",
            team_id="team-123",
            email="newuser@example.com",
            name="New User",
            role="user",
            created_at=datetime.now(),
        )

        mock_admin_service.add_user = AsyncMock(return_value=mock_user)

        test_app.dependency_overrides[get_admin_service] = lambda: mock_admin_service
        test_app.dependency_overrides[get_access_control] = lambda: mock_access_control
        test_app.dependency_overrides[get_current_user] = lambda: admin_user
        test_app.dependency_overrides[get_cognito_service] = lambda: None

        try:
            async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as client:
                response = await client.post(
                    "/admin/organizations/test-org/teams/team-123/users",
                    json={
                        "email": "newuser@example.com",
                        "name": "New User",
                        "role": "user",
                    },
                )

            assert response.status_code == 201
            data = response.json()
            assert data["email"] == "newuser@example.com"
        finally:
            test_app.dependency_overrides.clear()

    @pytest.mark.asyncio
    async def test_list_users_org(self, test_app, mock_admin_service, mock_access_control, admin_user):
        """Test listing users in organization."""
        from src.shared.schemas.admin import UserResponse

        mock_users = [
            UserResponse(
                id="user-1",
                org_id="test-org",
                team_id="team-1",
                email="user1@example.com",
                created_at=datetime.now(),
            ),
        ]

        mock_admin_service.list_users_org = AsyncMock(return_value=(mock_users, 1))

        test_app.dependency_overrides[get_admin_service] = lambda: mock_admin_service
        test_app.dependency_overrides[get_access_control] = lambda: mock_access_control
        test_app.dependency_overrides[get_current_user] = lambda: admin_user

        try:
            async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as client:
                response = await client.get("/admin/organizations/test-org/users")

            assert response.status_code == 200
            data = response.json()
            assert data["total"] == 1
        finally:
            test_app.dependency_overrides.clear()

    @pytest.mark.asyncio
    async def test_list_users_team(self, test_app, mock_admin_service, mock_access_control, admin_user):
        """Test listing users in a team."""
        from src.shared.schemas.admin import UserResponse

        mock_users = [
            UserResponse(
                id="user-1",
                org_id="test-org",
                team_id="team-123",
                email="user1@example.com",
                created_at=datetime.now(),
            ),
        ]

        mock_admin_service.list_users_team = AsyncMock(return_value=(mock_users, 1))

        test_app.dependency_overrides[get_admin_service] = lambda: mock_admin_service
        test_app.dependency_overrides[get_access_control] = lambda: mock_access_control
        test_app.dependency_overrides[get_current_user] = lambda: admin_user

        try:
            async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as client:
                response = await client.get("/admin/organizations/test-org/teams/team-123/users")

            assert response.status_code == 200
            data = response.json()
            assert data["total"] == 1
        finally:
            test_app.dependency_overrides.clear()

    @pytest.mark.asyncio
    async def test_remove_user(self, test_app, mock_admin_service, mock_access_control, admin_user):
        """Test removing a user."""
        mock_admin_service.remove_user = AsyncMock(return_value=True)

        test_app.dependency_overrides[get_admin_service] = lambda: mock_admin_service
        test_app.dependency_overrides[get_access_control] = lambda: mock_access_control
        test_app.dependency_overrides[get_current_user] = lambda: admin_user
        test_app.dependency_overrides[get_cognito_service] = lambda: None

        try:
            async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as client:
                response = await client.delete("/admin/organizations/test-org/users/user-123")

            assert response.status_code == 204
        finally:
            test_app.dependency_overrides.clear()


class TestServiceAccountEndpoints:
    """Tests for service account endpoints."""

    @pytest.mark.asyncio
    async def test_create_service_account(self, test_app, mock_admin_service, mock_access_control, admin_user):
        """Test creating a service account."""
        from src.shared.schemas.admin import ServiceAccountResponse

        mock_sa = ServiceAccountResponse(
            id="sa-123",
            org_id="test-org",
            department_id="default",
            team_id="default",
            name="ci-cd-pipeline",
            description="CI/CD pipeline service account",
            iam_role_arn="arn:aws:iam::123456789012:role/ci-cd-role",
            created_at=datetime.now(),
        )

        mock_admin_service.create_service_account = AsyncMock(return_value=mock_sa)

        test_app.dependency_overrides[get_admin_service] = lambda: mock_admin_service
        test_app.dependency_overrides[get_access_control] = lambda: mock_access_control
        test_app.dependency_overrides[get_current_user] = lambda: admin_user

        try:
            async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as client:
                response = await client.post(
                    "/admin/organizations/test-org/service-accounts",
                    json={
                        "name": "ci-cd-pipeline",
                        "description": "CI/CD pipeline service account",
                        "iam_role_arn": "arn:aws:iam::123456789012:role/ci-cd-role",
                    },
                )

            assert response.status_code == 201
            data = response.json()
            assert data["name"] == "ci-cd-pipeline"
        finally:
            test_app.dependency_overrides.clear()

    @pytest.mark.asyncio
    async def test_list_service_accounts(self, test_app, mock_admin_service, mock_access_control, admin_user):
        """Test listing service accounts."""
        from src.shared.schemas.admin import ServiceAccountResponse

        mock_sas = [
            ServiceAccountResponse(
                id="sa-1",
                org_id="test-org",
                department_id="default",
                team_id="default",
                name="sa-1",
                iam_role_arn="arn:aws:iam::123456789012:role/sa-1",
                created_at=datetime.now(),
            ),
        ]

        mock_admin_service.list_service_accounts = AsyncMock(return_value=(mock_sas, 1))

        test_app.dependency_overrides[get_admin_service] = lambda: mock_admin_service
        test_app.dependency_overrides[get_access_control] = lambda: mock_access_control
        test_app.dependency_overrides[get_current_user] = lambda: admin_user

        try:
            async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as client:
                response = await client.get("/admin/organizations/test-org/service-accounts")

            assert response.status_code == 200
            data = response.json()
            assert data["total"] == 1
        finally:
            test_app.dependency_overrides.clear()

    @pytest.mark.asyncio
    async def test_delete_service_account(self, test_app, mock_admin_service, mock_access_control, admin_user):
        """Test deleting a service account."""
        mock_admin_service.delete_service_account = AsyncMock(return_value=True)

        test_app.dependency_overrides[get_admin_service] = lambda: mock_admin_service
        test_app.dependency_overrides[get_access_control] = lambda: mock_access_control
        test_app.dependency_overrides[get_current_user] = lambda: admin_user

        try:
            async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as client:
                response = await client.delete("/admin/organizations/test-org/service-accounts/sa-123")

            assert response.status_code == 204
        finally:
            test_app.dependency_overrides.clear()


class TestPermissionChecks:
    """Tests for permission enforcement."""

    @pytest.mark.asyncio
    async def test_permission_check_called(self, test_app, mock_admin_service, mock_access_control, admin_user):
        """Test that permission checks are called."""
        from src.admin.config import Permission
        from src.shared.schemas.admin import DepartmentResponse

        mock_dept = DepartmentResponse(
            id="dept-123",
            org_id="test-org",
            name="Test",
            created_at=datetime.now(),
        )
        mock_admin_service.get_department = AsyncMock(return_value=mock_dept)

        test_app.dependency_overrides[get_admin_service] = lambda: mock_admin_service
        test_app.dependency_overrides[get_access_control] = lambda: mock_access_control
        test_app.dependency_overrides[get_current_user] = lambda: admin_user

        try:
            async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as client:
                await client.get("/admin/organizations/test-org/departments/dept-123")

            # Verify permission check was called
            mock_access_control.check_permission.assert_called_once()
            call_args = mock_access_control.check_permission.call_args
            assert call_args[0][1] == Permission.ORG_READ
        finally:
            test_app.dependency_overrides.clear()


class TestTenantIsolation:
    """Tests for tenant isolation."""

    @pytest.mark.asyncio
    async def test_tenant_isolation_enforced(self, test_app, mock_admin_service, mock_access_control, admin_user):
        """Test that org_id is properly passed to service methods."""
        from src.shared.schemas.admin import DepartmentResponse

        mock_dept = DepartmentResponse(
            id="dept-123",
            org_id="test-org",
            name="Test",
            created_at=datetime.now(),
        )
        mock_admin_service.get_department = AsyncMock(return_value=mock_dept)

        test_app.dependency_overrides[get_admin_service] = lambda: mock_admin_service
        test_app.dependency_overrides[get_access_control] = lambda: mock_access_control
        test_app.dependency_overrides[get_current_user] = lambda: admin_user

        try:
            async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as client:
                await client.get("/admin/organizations/different-org/departments/dept-123")

            # Verify org_id was passed correctly
            mock_admin_service.get_department.assert_called_once_with("different-org", "dept-123")
        finally:
            test_app.dependency_overrides.clear()
