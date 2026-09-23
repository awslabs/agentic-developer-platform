"""Tests for user management endpoints and RBAC middleware."""

import uuid

import pytest

from app.middleware.auth import create_access_token
from app.middleware.rbac import _has_permission, ROLE_HIERARCHY
from app.models.user import (
    VALID_ROLES,
    USER_STATUS_INVITED,
    USER_STATUS_ACTIVE,
    USER_STATUS_DISABLED,
)


# -- RBAC hierarchy tests --


class TestRBACHierarchy:
    """Test role hierarchy and permission checks."""

    def test_valid_roles_defined(self):
        assert "developer" in VALID_ROLES
        assert "workspace-admin" in VALID_ROLES
        assert "org-admin" in VALID_ROLES

    def test_role_hierarchy_order(self):
        assert ROLE_HIERARCHY["developer"] < ROLE_HIERARCHY["workspace-admin"]
        assert ROLE_HIERARCHY["workspace-admin"] < ROLE_HIERARCHY["org-admin"]

    def test_developer_can_access_developer_endpoints(self):
        assert _has_permission("developer", "developer") is True

    def test_developer_cannot_access_admin_endpoints(self):
        assert _has_permission("developer", "workspace-admin") is False
        assert _has_permission("developer", "org-admin") is False

    def test_workspace_admin_inherits_developer(self):
        assert _has_permission("workspace-admin", "developer") is True
        assert _has_permission("workspace-admin", "workspace-admin") is True

    def test_workspace_admin_cannot_access_org_admin(self):
        assert _has_permission("workspace-admin", "org-admin") is False

    def test_org_admin_can_access_everything(self):
        assert _has_permission("org-admin", "developer") is True
        assert _has_permission("org-admin", "workspace-admin") is True
        assert _has_permission("org-admin", "org-admin") is True

    def test_unknown_role_denied(self):
        assert _has_permission("unknown", "developer") is False

    def test_unknown_required_role_denied(self):
        assert _has_permission("org-admin", "super-admin") is False


# -- JWT with user context tests --


class TestJWTUserContext:
    """Test JWT creation with user_id and role claims."""

    def test_create_token_with_user_context(self):
        org_id = uuid.uuid4()
        user_id = uuid.uuid4()
        token, expires_in = create_access_token(
            org_id, user_id=user_id, role="developer"
        )
        assert isinstance(token, str)
        assert expires_in == 3600

        from app.middleware.auth import decode_token

        payload = decode_token(token)
        assert payload.org_id == org_id
        assert payload.user_id == user_id
        assert payload.role == "developer"

    def test_create_token_without_user_context(self):
        """Backward compatibility — tokens without user_id/role still work."""
        org_id = uuid.uuid4()
        token, _ = create_access_token(org_id)

        from app.middleware.auth import decode_token

        payload = decode_token(token)
        assert payload.org_id == org_id
        assert payload.user_id is None
        assert payload.role is None

    def test_create_token_with_org_admin_role(self):
        org_id = uuid.uuid4()
        user_id = uuid.uuid4()
        token, _ = create_access_token(org_id, user_id=user_id, role="org-admin")

        from app.middleware.auth import decode_token

        payload = decode_token(token)
        assert payload.role == "org-admin"


# -- User status constants tests --


class TestUserStatusConstants:
    """Test user status constants are correct."""

    def test_status_values(self):
        assert USER_STATUS_INVITED == "invited"
        assert USER_STATUS_ACTIVE == "active"
        assert USER_STATUS_DISABLED == "disabled"


# -- User endpoint tests --


class TestInviteEndpoint:
    """Test POST /users/invite."""

    @pytest.mark.asyncio
    async def test_invite_requires_auth(self, client):
        """Invite without auth returns 401/403."""
        response = await client.post(
            "/users/invite",
            json={"email": "test@example.com", "role": "developer"},
        )
        assert response.status_code in (401, 403)

    @pytest.mark.asyncio
    async def test_invite_requires_org_admin_role(self, client):
        """Invite with developer role returns 403."""
        org_id = uuid.uuid4()
        user_id = uuid.uuid4()
        token, _ = create_access_token(org_id, user_id=user_id, role="developer")
        headers = {"Authorization": f"Bearer {token}"}
        response = await client.post(
            "/users/invite",
            json={"email": "test@example.com", "role": "developer"},
            headers=headers,
        )
        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_invite_invalid_role_returns_422(self, client):
        """Invite with invalid role returns 422."""
        org_id = uuid.uuid4()
        user_id = uuid.uuid4()
        token, _ = create_access_token(org_id, user_id=user_id, role="org-admin")
        headers = {"Authorization": f"Bearer {token}"}
        response = await client.post(
            "/users/invite",
            json={"email": "test@example.com", "role": "superuser"},
            headers=headers,
        )
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_invite_missing_email_returns_422(self, client):
        """Invite without email returns 422."""
        org_id = uuid.uuid4()
        user_id = uuid.uuid4()
        token, _ = create_access_token(org_id, user_id=user_id, role="org-admin")
        headers = {"Authorization": f"Bearer {token}"}
        response = await client.post(
            "/users/invite",
            json={"role": "developer"},
            headers=headers,
        )
        assert response.status_code == 422


class TestListUsersEndpoint:
    """Test GET /users."""

    @pytest.mark.asyncio
    async def test_list_requires_auth(self, client):
        """List users without auth returns 401/403."""
        response = await client.get("/users")
        assert response.status_code in (401, 403)

    @pytest.mark.asyncio
    async def test_list_does_not_require_admin_role(self, client):
        """List users endpoint is accessible by any authenticated user.

        We verify the route exists and does not require elevated RBAC role
        by checking the OpenAPI spec for the route.
        """
        from app.endpoint_inventory import mounted_operations
        from app.main import app as fastapi_app

        # Shared enumeration (issue #5682, A02): the previous `r.path for r in
        # fastapi_app.routes` listed only the four Starlette docs routes once
        # FastAPI began storing included routers lazily, so this asserted the
        # absence of a route that is in fact mounted.
        assert ("GET", "/users") in mounted_operations(fastapi_app)


class TestUpdateRoleEndpoint:
    """Test PATCH /users/{id}/role."""

    @pytest.mark.asyncio
    async def test_update_role_requires_auth(self, client):
        """Update role without auth returns 401/403."""
        user_id = uuid.uuid4()
        response = await client.patch(
            f"/users/{user_id}/role",
            json={"role": "workspace-admin"},
        )
        assert response.status_code in (401, 403)

    @pytest.mark.asyncio
    async def test_update_role_requires_org_admin(self, client):
        """Update role with developer role returns 403."""
        org_id = uuid.uuid4()
        user_id = uuid.uuid4()
        target_user_id = uuid.uuid4()
        token, _ = create_access_token(org_id, user_id=user_id, role="developer")
        headers = {"Authorization": f"Bearer {token}"}
        response = await client.patch(
            f"/users/{target_user_id}/role",
            json={"role": "workspace-admin"},
            headers=headers,
        )
        assert response.status_code == 403


class TestDeleteUserEndpoint:
    """Test DELETE /users/{id}."""

    @pytest.mark.asyncio
    async def test_delete_requires_auth(self, client):
        """Delete user without auth returns 401/403."""
        user_id = uuid.uuid4()
        response = await client.delete(f"/users/{user_id}")
        assert response.status_code in (401, 403)

    @pytest.mark.asyncio
    async def test_delete_requires_org_admin(self, client):
        """Delete user with developer role returns 403."""
        org_id = uuid.uuid4()
        user_id = uuid.uuid4()
        target_user_id = uuid.uuid4()
        token, _ = create_access_token(org_id, user_id=user_id, role="developer")
        headers = {"Authorization": f"Bearer {token}"}
        response = await client.delete(
            f"/users/{target_user_id}",
            headers=headers,
        )
        assert response.status_code == 403
