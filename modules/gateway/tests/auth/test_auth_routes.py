"""
Unit tests for the Auth Routes module.

These tests cover FastAPI authentication endpoints with proper mocking
to avoid actual service dependencies during testing.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.auth.exceptions import DuplicateServiceAccountError, ServiceAccountNotFoundError
from src.auth.middleware import get_current_user_context
from src.auth.routes import router
from src.auth.schemas import (
    ServiceAccountResponse,
)
from src.shared.exceptions import UnknownOrganizationError, UnregisteredServiceAccountError
from src.shared.schemas.auth import AuthExchangeResponse, TokenContext

# Create a test FastAPI app with the auth router
test_app = FastAPI()
test_app.include_router(router)
client = TestClient(test_app)


def create_mock_context_dependency(mock_context: TokenContext):
    """Create a mock dependency function that returns the given context."""

    async def mock_dependency():
        return mock_context

    return mock_dependency


@pytest.mark.unit
class TestAuthRoutes:
    """Test suite for auth routes."""

    def test_exchange_credentials_disabled_by_default(self):
        """Test that /auth/exchange endpoint is disabled by default (Issue #133).

        Issue #133: Security Fix - The /auth/exchange endpoint is disabled by default
        to prevent credential exposure risk. It returns 410 Gone when disabled.
        """
        request_data = {
            "aws_access_key_id": "AKIAIOSFODNN7EXAMPLE",
            "aws_secret_access_key": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYzEXAMPLEKEY",
            "aws_session_token": "session-token",
        }

        response = client.post("/auth/exchange", json=request_data)

        assert response.status_code == 410  # Gone - endpoint disabled
        data = response.json()
        assert data["detail"]["error"] == "endpoint_disabled"
        assert "deprecated" in data["detail"]["message"].lower()

    def test_exchange_credentials_success_when_enabled(self):
        """Test successful credential exchange when endpoint is enabled."""
        request_data = {
            "aws_access_key_id": "AKIAIOSFODNN7EXAMPLE",
            "aws_secret_access_key": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYzEXAMPLEKEY",
            "aws_session_token": "session-token",
        }

        expected_response = AuthExchangeResponse(
            token="mock-jwt-token",
            expires_at=datetime.now(UTC) + timedelta(hours=12),
            user_id="user-123",
            org_id="org-456",
            team_id="team-789",
            department_id="dept-abc",
            account_type="human",
        )

        # Enable the legacy endpoint via feature flag
        with patch("src.auth.routes.ENABLE_LEGACY_AUTH_EXCHANGE", True):
            with patch("src.auth.routes.auth_service.exchange_credentials", return_value=expected_response):
                response = client.post("/auth/exchange", json=request_data)

                assert response.status_code == 200
                data = response.json()
                assert data["token"] == "mock-jwt-token"
                assert data["user_id"] == "user-123"
                assert data["account_type"] == "human"

    def test_exchange_credentials_invalid_request(self):
        """Test credential exchange with invalid request data."""
        # Missing required fields
        request_data = {
            "aws_access_key_id": "AKIAIOSFODNN7EXAMPLE"
            # Missing aws_secret_access_key and aws_session_token
        }

        response = client.post("/auth/exchange", json=request_data)

        assert response.status_code == 422  # Validation error

    def test_exchange_credentials_unknown_organization_when_enabled(self):
        """Test credential exchange with unknown organization (when enabled)."""
        request_data = {
            "aws_access_key_id": "AKIAIOSFODNN7EXAMPLE",
            "aws_secret_access_key": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYzEXAMPLEKEY",
            "aws_session_token": "session-token",
        }

        with patch("src.auth.routes.ENABLE_LEGACY_AUTH_EXCHANGE", True):
            with patch("src.auth.routes.auth_service.exchange_credentials", side_effect=UnknownOrganizationError("123456789012")):
                response = client.post("/auth/exchange", json=request_data)

                assert response.status_code == 403
                data = response.json()
                assert data["detail"]["error"] == "unknown_organization"
                assert "123456789012" in data["detail"]["message"]

    def test_exchange_credentials_unregistered_service_account_when_enabled(self):
        """Test credential exchange with unregistered service account (when enabled)."""
        request_data = {
            "aws_access_key_id": "AKIAIOSFODNN7EXAMPLE",
            "aws_secret_access_key": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYzEXAMPLEKEY",
            "aws_session_token": "session-token",
        }

        role_arn = "arn:aws:iam::123456789012:role/unregistered-role"
        with patch("src.auth.routes.ENABLE_LEGACY_AUTH_EXCHANGE", True):
            with patch("src.auth.routes.auth_service.exchange_credentials", side_effect=UnregisteredServiceAccountError(role_arn)):
                response = client.post("/auth/exchange", json=request_data)

                assert response.status_code == 403
                data = response.json()
                assert data["detail"]["error"] == "unregistered_service_account"
                # Error message no longer exposes role ARN for security
                assert "not registered" in data["detail"]["message"].lower()

    def test_exchange_credentials_unexpected_error_when_enabled(self):
        """Test credential exchange with unexpected error (when enabled)."""
        request_data = {
            "aws_access_key_id": "AKIAIOSFODNN7EXAMPLE",
            "aws_secret_access_key": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYzEXAMPLEKEY",
            "aws_session_token": "session-token",
        }

        with patch("src.auth.routes.ENABLE_LEGACY_AUTH_EXCHANGE", True):
            with patch("src.auth.routes.auth_service.exchange_credentials", side_effect=Exception("Unexpected error")):
                response = client.post("/auth/exchange", json=request_data)

                assert response.status_code == 500
                data = response.json()
                assert data["detail"]["error"] == "internal_error"

    def test_revoke_token_success(self):
        """Test successful token revocation."""
        mock_token_context = TokenContext(
            user_id="user-123",
            org_id="org-456",
            team_id="team-789",
            department_id="dept-abc",
            account_type="human",
            is_admin=False,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )

        # Override the dependency to return our mock context
        test_app.dependency_overrides[get_current_user_context] = create_mock_context_dependency(mock_token_context)
        try:
            with patch("src.auth.routes.auth_service.revoke_token", new_callable=AsyncMock):
                response = client.post("/auth/revoke", headers={"Authorization": "Bearer mock-token"})

                assert response.status_code == 200
                data = response.json()
                assert data["message"] == "Token revoked successfully"
        finally:
            test_app.dependency_overrides.clear()

    def test_revoke_token_unauthorized(self):
        """Test token revocation without authentication."""
        # Clear any overrides to test actual authentication
        test_app.dependency_overrides.clear()
        response = client.post("/auth/revoke")

        # FastAPI HTTPBearer returns 401 for unauthenticated requests
        assert response.status_code == 401

    def test_create_service_account_success(self):
        """Test successful service account creation."""
        request_data = {
            "name": "Test Service Account",
            "department_id": "dept-123",
            "team_id": "team-456",
            "iam_role_arn": "arn:aws:iam::123456789012:role/test-service",
        }

        mock_token_context = TokenContext(
            user_id="admin-user",
            org_id="org-456",
            team_id="team-789",
            department_id="dept-abc",
            account_type="human",
            is_admin=True,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )

        mock_response = ServiceAccountResponse(
            id="sa-123",
            org_id="org-456",
            name="Test Service Account",
            department_id="dept-123",
            team_id="team-456",
            iam_role_arn="arn:aws:iam::123456789012:role/test-service",
            created_at=datetime.now(UTC),
        )

        test_app.dependency_overrides[get_current_user_context] = create_mock_context_dependency(mock_token_context)
        try:
            with patch("src.auth.routes.service_account_service.create_service_account", new_callable=AsyncMock, return_value=mock_response):
                response = client.post("/auth/service-accounts", json=request_data, headers={"Authorization": "Bearer admin-token"})

                assert response.status_code == 200
        finally:
            test_app.dependency_overrides.clear()

    def test_create_service_account_insufficient_permissions(self):
        """Test service account creation without admin privileges."""
        request_data = {
            "name": "Test Service Account",
            "department_id": "dept-123",
            "team_id": "team-456",
            "iam_role_arn": "arn:aws:iam::123456789012:role/test-service",
        }

        mock_token_context = TokenContext(
            user_id="regular-user",
            org_id="org-456",
            team_id="team-789",
            department_id="dept-abc",
            account_type="human",
            is_admin=False,  # Not admin
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )

        test_app.dependency_overrides[get_current_user_context] = create_mock_context_dependency(mock_token_context)
        try:
            response = client.post("/auth/service-accounts", json=request_data, headers={"Authorization": "Bearer user-token"})

            assert response.status_code == 403
            data = response.json()
            assert data["detail"]["error"] == "insufficient_permissions"
        finally:
            test_app.dependency_overrides.clear()

    def test_create_service_account_duplicate_arn(self):
        """Test service account creation with duplicate IAM role ARN."""
        request_data = {
            "name": "Test Service Account",
            "department_id": "dept-123",
            "team_id": "team-456",
            "iam_role_arn": "arn:aws:iam::123456789012:role/existing-service",
        }

        mock_token_context = TokenContext(
            user_id="admin-user",
            org_id="org-456",
            team_id="team-789",
            department_id="dept-abc",
            account_type="human",
            is_admin=True,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )

        role_arn = "arn:aws:iam::123456789012:role/existing-service"
        test_app.dependency_overrides[get_current_user_context] = create_mock_context_dependency(mock_token_context)
        try:
            with patch(
                "src.auth.routes.service_account_service.create_service_account",
                new_callable=AsyncMock,
                side_effect=DuplicateServiceAccountError(role_arn),
            ):
                response = client.post("/auth/service-accounts", json=request_data, headers={"Authorization": "Bearer admin-token"})

                assert response.status_code == 409
        finally:
            test_app.dependency_overrides.clear()

    def test_list_service_accounts_success(self):
        """Test successful service account listing."""
        mock_token_context = TokenContext(
            user_id="user-123",
            org_id="org-456",
            team_id="team-789",
            department_id="dept-abc",
            account_type="human",
            is_admin=False,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )

        mock_response = MagicMock()
        mock_response.service_accounts = []
        mock_response.total_count = 0
        mock_response.page = 1
        mock_response.page_size = 50

        test_app.dependency_overrides[get_current_user_context] = create_mock_context_dependency(mock_token_context)
        try:
            with patch("src.auth.routes.service_account_service.list_service_accounts", new_callable=AsyncMock, return_value=mock_response):
                response = client.get("/auth/service-accounts", headers={"Authorization": "Bearer user-token"})

                assert response.status_code == 200
        finally:
            test_app.dependency_overrides.clear()

    def test_list_service_accounts_with_filters(self):
        """Test service account listing with filters."""
        mock_token_context = TokenContext(
            user_id="user-123",
            org_id="org-456",
            team_id="team-789",
            department_id="dept-abc",
            account_type="human",
            is_admin=False,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )

        mock_response = MagicMock()
        mock_response.service_accounts = []
        mock_response.total_count = 0
        mock_response.page = 1
        mock_response.page_size = 50

        test_app.dependency_overrides[get_current_user_context] = create_mock_context_dependency(mock_token_context)
        try:
            with patch(
                "src.auth.routes.service_account_service.list_service_accounts", new_callable=AsyncMock, return_value=mock_response
            ) as mock_list:
                response = client.get(
                    "/auth/service-accounts?department_id=dept-123&team_id=team-456&page=2&page_size=25",
                    headers={"Authorization": "Bearer user-token"},
                )

                assert response.status_code == 200
                mock_list.assert_called_once()
                # Check that the filters were passed correctly
                call_args = mock_list.call_args
                assert call_args[1]["department_id"] == "dept-123"
                assert call_args[1]["team_id"] == "team-456"
                assert call_args[1]["page"] == 2
                assert call_args[1]["page_size"] == 25
        finally:
            test_app.dependency_overrides.clear()

    def test_get_service_account_success(self):
        """Test successful service account retrieval."""
        mock_token_context = TokenContext(
            user_id="user-123",
            org_id="org-456",
            team_id="team-789",
            department_id="dept-abc",
            account_type="human",
            is_admin=False,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )

        mock_response = ServiceAccountResponse(
            id="sa-123",
            org_id="org-456",
            name="Test Service Account",
            department_id="dept-123",
            team_id="team-456",
            iam_role_arn="arn:aws:iam::123456789012:role/test-service",
            created_at=datetime.now(UTC),
        )

        test_app.dependency_overrides[get_current_user_context] = create_mock_context_dependency(mock_token_context)
        try:
            with patch("src.auth.routes.service_account_service.get_service_account", new_callable=AsyncMock, return_value=mock_response):
                response = client.get("/auth/service-accounts/sa-123", headers={"Authorization": "Bearer user-token"})

                assert response.status_code == 200
        finally:
            test_app.dependency_overrides.clear()

    def test_get_service_account_not_found(self):
        """Test getting non-existent service account."""
        mock_token_context = TokenContext(
            user_id="user-123",
            org_id="org-456",
            team_id="team-789",
            department_id="dept-abc",
            account_type="human",
            is_admin=False,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )

        test_app.dependency_overrides[get_current_user_context] = create_mock_context_dependency(mock_token_context)
        try:
            with patch(
                "src.auth.routes.service_account_service.get_service_account",
                new_callable=AsyncMock,
                side_effect=ServiceAccountNotFoundError("sa-999"),
            ):
                response = client.get("/auth/service-accounts/sa-999", headers={"Authorization": "Bearer user-token"})

                assert response.status_code == 404
        finally:
            test_app.dependency_overrides.clear()

    def test_update_service_account_success(self):
        """Test successful service account update."""
        request_data = {"name": "Updated Service Account Name"}

        mock_token_context = TokenContext(
            user_id="admin-user",
            org_id="org-456",
            team_id="team-789",
            department_id="dept-abc",
            account_type="human",
            is_admin=True,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )

        mock_response = ServiceAccountResponse(
            id="sa-123",
            org_id="org-456",
            name="Updated Service Account Name",
            department_id="dept-123",
            team_id="team-456",
            iam_role_arn="arn:aws:iam::123456789012:role/test-service",
            created_at=datetime.now(UTC),
        )

        test_app.dependency_overrides[get_current_user_context] = create_mock_context_dependency(mock_token_context)
        try:
            with patch("src.auth.routes.service_account_service.update_service_account", new_callable=AsyncMock, return_value=mock_response):
                response = client.put("/auth/service-accounts/sa-123", json=request_data, headers={"Authorization": "Bearer admin-token"})

                assert response.status_code == 200
        finally:
            test_app.dependency_overrides.clear()

    def test_update_service_account_insufficient_permissions(self):
        """Test service account update without admin privileges."""
        request_data = {"name": "Updated Service Account Name"}

        mock_token_context = TokenContext(
            user_id="regular-user",
            org_id="org-456",
            team_id="team-789",
            department_id="dept-abc",
            account_type="human",
            is_admin=False,  # Not admin
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )

        test_app.dependency_overrides[get_current_user_context] = create_mock_context_dependency(mock_token_context)
        try:
            response = client.put("/auth/service-accounts/sa-123", json=request_data, headers={"Authorization": "Bearer user-token"})

            assert response.status_code == 403
            data = response.json()
            assert data["detail"]["error"] == "insufficient_permissions"
        finally:
            test_app.dependency_overrides.clear()

    def test_delete_service_account_success(self):
        """Test successful service account deletion."""
        mock_token_context = TokenContext(
            user_id="admin-user",
            org_id="org-456",
            team_id="team-789",
            department_id="dept-abc",
            account_type="human",
            is_admin=True,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )

        test_app.dependency_overrides[get_current_user_context] = create_mock_context_dependency(mock_token_context)
        try:
            with patch("src.auth.routes.service_account_service.delete_service_account", new_callable=AsyncMock, return_value=True):
                response = client.delete("/auth/service-accounts/sa-123", headers={"Authorization": "Bearer admin-token"})

                assert response.status_code == 200
                data = response.json()
                assert data["message"] == "Service account deleted successfully"
        finally:
            test_app.dependency_overrides.clear()

    def test_delete_service_account_not_found(self):
        """Test deleting non-existent service account."""
        mock_token_context = TokenContext(
            user_id="admin-user",
            org_id="org-456",
            team_id="team-789",
            department_id="dept-abc",
            account_type="human",
            is_admin=True,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )

        test_app.dependency_overrides[get_current_user_context] = create_mock_context_dependency(mock_token_context)
        try:
            with patch(
                "src.auth.routes.service_account_service.delete_service_account",
                new_callable=AsyncMock,
                side_effect=ServiceAccountNotFoundError("sa-999"),
            ):
                response = client.delete("/auth/service-accounts/sa-999", headers={"Authorization": "Bearer admin-token"})

                assert response.status_code == 404
        finally:
            test_app.dependency_overrides.clear()

    def test_get_current_user_success(self):
        """Test successful get current user endpoint."""
        mock_token_context = TokenContext(
            user_id="user-123",
            org_id="org-456",
            team_id="team-789",
            department_id="dept-abc",
            account_type="human",
            is_admin=False,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )

        test_app.dependency_overrides[get_current_user_context] = create_mock_context_dependency(mock_token_context)
        try:
            response = client.get("/auth/me", headers={"Authorization": "Bearer valid-token"})

            assert response.status_code == 200
            data = response.json()
            assert data["user_id"] == "user-123"
            assert data["org_id"] == "org-456"
            assert data["account_type"] == "human"
            assert data["is_admin"] is False
        finally:
            test_app.dependency_overrides.clear()

    def test_get_current_user_unauthorized(self):
        """Test get current user without authentication."""
        test_app.dependency_overrides.clear()
        response = client.get("/auth/me")
        # FastAPI HTTPBearer returns 401 for unauthenticated requests
        assert response.status_code == 401

    def test_logout_success(self):
        """Test successful logout endpoint."""
        mock_token_context = TokenContext(
            user_id="user-123",
            org_id="org-456",
            team_id="team-789",
            department_id="dept-abc",
            account_type="human",
            is_admin=False,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )

        test_app.dependency_overrides[get_current_user_context] = create_mock_context_dependency(mock_token_context)
        try:
            response = client.post("/auth/logout", headers={"Authorization": "Bearer valid-token"})

            assert response.status_code == 200
            data = response.json()
            assert data["message"] == "Logout successful"
        finally:
            test_app.dependency_overrides.clear()

    def test_logout_unauthorized(self):
        """Test logout without authentication."""
        test_app.dependency_overrides.clear()
        response = client.post("/auth/logout")
        # FastAPI HTTPBearer returns 401 for unauthenticated requests
        assert response.status_code == 401

    def test_cleanup_expired_tokens_success(self):
        """Test successful token cleanup admin endpoint."""
        mock_token_context = TokenContext(
            user_id="admin-user",
            org_id="org-456",
            team_id="team-789",
            department_id="dept-abc",
            account_type="human",
            is_admin=True,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )

        test_app.dependency_overrides[get_current_user_context] = create_mock_context_dependency(mock_token_context)
        try:
            with patch("src.auth.routes.auth_service.cleanup_expired_tokens", new_callable=AsyncMock, return_value=5):
                response = client.post("/auth/admin/cleanup-tokens", headers={"Authorization": "Bearer admin-token"})

                assert response.status_code == 200
                data = response.json()
                assert data["tokens_cleaned"] == 5
        finally:
            test_app.dependency_overrides.clear()

    def test_cleanup_expired_tokens_insufficient_permissions(self):
        """Test token cleanup without admin privileges."""
        mock_token_context = TokenContext(
            user_id="regular-user",
            org_id="org-456",
            team_id="team-789",
            department_id="dept-abc",
            account_type="human",
            is_admin=False,  # Not admin
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )

        test_app.dependency_overrides[get_current_user_context] = create_mock_context_dependency(mock_token_context)
        try:
            response = client.post("/auth/admin/cleanup-tokens", headers={"Authorization": "Bearer user-token"})

            assert response.status_code == 403
            data = response.json()
            assert data["detail"]["error"] == "insufficient_permissions"
        finally:
            test_app.dependency_overrides.clear()

    def test_revoke_all_user_tokens_success(self):
        """Test successful bulk user token revocation."""
        mock_token_context = TokenContext(
            user_id="admin-user",
            org_id="org-456",
            team_id="team-789",
            department_id="dept-abc",
            account_type="human",
            is_admin=True,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )

        test_app.dependency_overrides[get_current_user_context] = create_mock_context_dependency(mock_token_context)
        try:
            with patch("src.auth.routes.auth_service.revoke_all_user_tokens", new_callable=AsyncMock, return_value=3):
                response = client.post("/auth/admin/revoke-user-tokens/user-123", headers={"Authorization": "Bearer admin-token"})

                assert response.status_code == 200
                data = response.json()
                assert data["tokens_revoked"] == 3
        finally:
            test_app.dependency_overrides.clear()
