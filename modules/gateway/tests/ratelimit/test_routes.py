"""
Unit tests for the rate limit routes.

This module tests the FastAPI routes for rate limit configuration.
"""

from datetime import datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.ratelimit.backends.in_memory import InMemoryBackend
from src.ratelimit.config import RateLimitConfig
from src.ratelimit.routes import router, set_rate_limit_service
from src.ratelimit.service import RateLimitService
from src.shared.schemas.auth import TokenContext


class TestRateLimitRoutes:
    """Tests for rate limit API routes."""

    @pytest.fixture
    def backend(self):
        """Provide a fresh InMemoryBackend."""
        return InMemoryBackend()

    @pytest.fixture
    def config(self):
        """Provide test configuration."""
        return RateLimitConfig(
            default_rpm=60,
            default_tpm=100000,
            default_concurrent=10,
        )

    @pytest.fixture
    def service(self, backend, config):
        """Provide a RateLimitService instance."""
        return RateLimitService(backend=backend, config=config)

    @pytest.fixture
    def admin_context(self):
        """Provide an admin user context."""
        return TokenContext(
            user_id="admin-123",
            org_id="org-456",
            team_id="team-789",
            department_id="dept-012",
            account_type="human",
            is_admin=True,
            expires_at=datetime.utcnow() + timedelta(hours=1),
        )

    @pytest.fixture
    def user_context(self):
        """Provide a regular user context."""
        return TokenContext(
            user_id="user-123",
            org_id="org-456",
            team_id="team-789",
            department_id="dept-012",
            account_type="human",
            is_admin=False,
            expires_at=datetime.utcnow() + timedelta(hours=1),
        )

    @pytest.fixture
    def admin_app(self, service, admin_context):
        """Provide a FastAPI app with admin context.

        Issue #133: Updated to override get_current_user dependency
        instead of using middleware to set request.state.token_context.
        """
        from src.auth.dependencies import get_current_user

        app = FastAPI()
        app.include_router(router)
        set_rate_limit_service(service)

        # Issue #133: Override the auth dependency to return admin context
        async def override_get_current_user():
            return admin_context

        app.dependency_overrides[get_current_user] = override_get_current_user

        return app

    @pytest.fixture
    def user_app(self, service, user_context):
        """Provide a FastAPI app with regular user context.

        Issue #133: Updated to override get_current_user dependency
        instead of using middleware to set request.state.token_context.
        """
        from src.auth.dependencies import get_current_user

        app = FastAPI()
        app.include_router(router)
        set_rate_limit_service(service)

        # Issue #133: Override the auth dependency to return user context
        async def override_get_current_user():
            return user_context

        app.dependency_overrides[get_current_user] = override_get_current_user

        return app

    @pytest.fixture
    def admin_client(self, admin_app):
        """Provide a test client with admin context."""
        return TestClient(admin_app)

    @pytest.fixture
    def user_client(self, user_app):
        """Provide a test client with regular user context."""
        return TestClient(user_app)

    def test_list_rate_limits_empty(self, admin_client):
        """Test listing rate limits when none configured."""
        response = admin_client.get("/ratelimits")
        assert response.status_code == 200
        assert response.json() == []

    def test_list_rate_limits_with_data(self, admin_client):
        """Test listing rate limits after configuration."""
        # First configure some limits
        admin_client.put(
            "/ratelimits/user/user-123",
            json={"rpm": 100, "tpm": 50000},
        )

        response = admin_client.get("/ratelimits")
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["entity_type"] == "user"
        assert data[0]["entity_id"] == "user-123"

    def test_list_rate_limits_filter_by_type(self, admin_client):
        """Test filtering rate limits by entity type."""
        # Configure limits for different entity types
        admin_client.put(
            "/ratelimits/user/user-123",
            json={"rpm": 100},
        )
        admin_client.put(
            "/ratelimits/team/team-123",
            json={"rpm": 200},
        )

        # Filter by user
        response = admin_client.get("/ratelimits?entity_type=user")
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["entity_type"] == "user"

    def test_get_rate_limits(self, admin_client):
        """Test getting rate limits for specific entity."""
        # Configure limits
        admin_client.put(
            "/ratelimits/user/user-123",
            json={"rpm": 100, "tpm": 50000, "concurrent_requests": 5},
        )

        # Get limits
        response = admin_client.get("/ratelimits/user/user-123")
        assert response.status_code == 200
        data = response.json()
        assert data["entity_type"] == "user"
        assert data["entity_id"] == "user-123"
        assert data["rpm"] == 100
        assert data["tpm"] == 50000
        assert data["concurrent_requests"] == 5

    def test_get_rate_limits_defaults(self, admin_client):
        """Test getting rate limits returns defaults when not configured."""
        response = admin_client.get("/ratelimits/user/unconfigured-user")
        assert response.status_code == 200
        data = response.json()
        assert data["entity_type"] == "user"
        assert data["rpm"] == 60  # Default

    def test_get_rate_limits_invalid_entity_type(self, admin_client):
        """Test getting rate limits with invalid entity type."""
        response = admin_client.get("/ratelimits/invalid/user-123")
        assert response.status_code == 400
        assert "Invalid entity type" in response.json()["detail"]

    def test_configure_rate_limits(self, admin_client):
        """Test configuring rate limits."""
        response = admin_client.put(
            "/ratelimits/user/user-123",
            json={"rpm": 100, "tpm": 50000, "concurrent_requests": 5},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["rpm"] == 100
        assert data["tpm"] == 50000
        assert data["concurrent_requests"] == 5

    def test_configure_rate_limits_partial(self, admin_client):
        """Test configuring only some rate limits."""
        response = admin_client.put(
            "/ratelimits/user/user-123",
            json={"rpm": 100},  # Only RPM
        )
        assert response.status_code == 200
        data = response.json()
        assert data["rpm"] == 100
        assert data["tpm"] is None

    def test_configure_rate_limits_empty_fails(self, admin_client):
        """Test configuring with no limits fails."""
        response = admin_client.put(
            "/ratelimits/user/user-123",
            json={},
        )
        assert response.status_code == 400
        assert "At least one rate limit must be specified" in response.json()["detail"]

    def test_configure_rate_limits_invalid_entity_type(self, admin_client):
        """Test configuring with invalid entity type."""
        response = admin_client.put(
            "/ratelimits/invalid/user-123",
            json={"rpm": 100},
        )
        assert response.status_code == 400
        assert "Invalid entity type" in response.json()["detail"]

    def test_delete_rate_limits(self, admin_client):
        """Test deleting rate limits."""
        # First configure
        admin_client.put(
            "/ratelimits/user/user-123",
            json={"rpm": 100},
        )

        # Then delete
        response = admin_client.delete("/ratelimits/user/user-123")
        assert response.status_code == 204

        # Verify deleted (should return defaults)
        response = admin_client.get("/ratelimits/user/user-123")
        data = response.json()
        assert data["rpm"] == 60  # Back to default

    def test_delete_rate_limits_nonexistent(self, admin_client):
        """Test deleting nonexistent rate limits."""
        response = admin_client.delete("/ratelimits/user/nonexistent")
        assert response.status_code == 404

    def test_get_rate_limit_status(self, admin_client):
        """Test getting rate limit status."""
        response = admin_client.get("/ratelimits/user/user-123/status")
        assert response.status_code == 200
        data = response.json()
        assert data["entity_type"] == "user"
        assert data["entity_id"] == "user-123"
        assert "rpm_limit" in data
        assert "rpm_remaining" in data

    def test_get_rate_limit_status_service_account(self, admin_client):
        """Test getting rate limit status for service account."""
        response = admin_client.get("/ratelimits/service_account/sa-123/status")
        assert response.status_code == 200
        data = response.json()
        assert data["entity_type"] == "service_account"
        assert data["rpm_limit"] == 120  # Service account default

    def test_admin_required_for_list(self, user_client):
        """Test admin required for listing rate limits."""
        response = user_client.get("/ratelimits")
        assert response.status_code == 403

    def test_admin_required_for_configure(self, user_client):
        """Test admin required for configuring rate limits."""
        response = user_client.put(
            "/ratelimits/user/user-123",
            json={"rpm": 100},
        )
        assert response.status_code == 403

    def test_admin_required_for_delete(self, user_client):
        """Test admin required for deleting rate limits."""
        response = user_client.delete("/ratelimits/user/user-123")
        assert response.status_code == 403

    def test_self_access_for_status(self, user_client, user_context):
        """Test user can access their own status."""
        response = user_client.get(f"/ratelimits/user/{user_context.user_id}/status")
        assert response.status_code == 200

    def test_no_access_for_other_user_status(self, user_client):
        """Test user cannot access another user's status."""
        response = user_client.get("/ratelimits/user/other-user/status")
        assert response.status_code == 403


class TestRoutesAuthentication:
    """Tests for route authentication requirements."""

    @pytest.fixture
    def unauth_app(self):
        """Provide a FastAPI app without authentication."""
        app = FastAPI()
        app.include_router(router)
        set_rate_limit_service(RateLimitService())
        return app

    @pytest.fixture
    def unauth_client(self, unauth_app):
        """Provide a test client without authentication."""
        return TestClient(unauth_app)

    def test_list_requires_auth(self, unauth_client):
        """Test list endpoint requires authentication."""
        response = unauth_client.get("/ratelimits")
        assert response.status_code == 401

    def test_get_requires_auth(self, unauth_client):
        """Test get endpoint requires authentication."""
        response = unauth_client.get("/ratelimits/user/user-123")
        assert response.status_code == 401

    def test_configure_requires_auth(self, unauth_client):
        """Test configure endpoint requires authentication."""
        response = unauth_client.put(
            "/ratelimits/user/user-123",
            json={"rpm": 100},
        )
        assert response.status_code == 401

    def test_delete_requires_auth(self, unauth_client):
        """Test delete endpoint requires authentication."""
        response = unauth_client.delete("/ratelimits/user/user-123")
        assert response.status_code == 401

    def test_status_requires_auth(self, unauth_client):
        """Test status endpoint requires authentication."""
        response = unauth_client.get("/ratelimits/user/user-123/status")
        assert response.status_code == 401
