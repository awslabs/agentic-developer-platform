"""Tests for self-service signup (POST /auth/signup) and org endpoints (GET /orgs/current)."""

import uuid
from unittest.mock import MagicMock, patch

import pytest

from app.middleware.auth import create_access_token
from app.schemas.auth import SignupRequest, SignupResponse


def _auth_header(org_id: uuid.UUID | None = None) -> dict:
    """Create an Authorization header with a valid JWT."""
    if org_id is None:
        org_id = uuid.uuid4()
    token, _ = create_access_token(org_id)
    return {"Authorization": f"Bearer {token}"}


# -- Schema validation tests --


class TestSignupSchemas:
    """Test signup request/response Pydantic schemas."""

    def test_signup_request_valid(self):
        req = SignupRequest(
            email="user@example.com",
            password="SecureP@ss1",
            org_name="My Org",
        )
        assert req.email == "user@example.com"
        assert req.org_name == "My Org"

    def test_signup_request_short_password_rejected(self):
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            SignupRequest(
                email="user@example.com",
                password="short",
                org_name="My Org",
            )

    def test_signup_request_empty_email_rejected(self):
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            SignupRequest(
                email="",
                password="SecureP@ss1",
                org_name="My Org",
            )

    def test_signup_request_invalid_email_format_rejected(self):
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            SignupRequest(
                email="not-an-email",
                password="SecureP@ss1",
                org_name="My Org",
            )

    def test_signup_request_empty_org_name_rejected(self):
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            SignupRequest(
                email="user@example.com",
                password="SecureP@ss1",
                org_name="",
            )

    def test_signup_response_fields(self):
        org_id = uuid.uuid4()
        ws_id = uuid.uuid4()
        resp = SignupResponse(
            access_token="jwt.token.here",
            expires_in=3600,
            org_id=org_id,
            org_name="Test Org",
            default_workspace_id=ws_id,
        )
        assert resp.org_id == org_id
        assert resp.default_workspace_id == ws_id
        assert resp.token_type == "bearer"


# -- Org schema tests --


class TestOrgSchemas:
    """Test org response schema."""

    def test_org_response_valid(self):
        from datetime import datetime, timezone

        from app.schemas.org import OrgResponse

        now = datetime.now(timezone.utc)
        resp = OrgResponse(
            id=uuid.uuid4(),
            name="Test Org",
            billing_plan="free",
            quotas_json=None,
            created_at=now,
        )
        assert resp.billing_plan == "free"


# -- Endpoint tests --


class TestSignupEndpoint:
    """Test POST /auth/signup."""

    @pytest.mark.asyncio
    async def test_signup_without_body_returns_422(self, client):
        """Signup without JSON body returns validation error."""
        response = await client.post("/auth/signup")
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_signup_missing_fields_returns_422(self, client):
        """Signup with missing required fields returns 422."""
        response = await client.post("/auth/signup", json={"email": "a@b.com"})
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_signup_short_password_returns_422(self, client):
        """Signup with too-short password returns 422."""
        response = await client.post(
            "/auth/signup",
            json={
                "email": "user@example.com",
                "password": "short",
                "org_name": "My Org",
            },
        )
        assert response.status_code == 422


class TestGetCurrentOrg:
    """Test GET /orgs/current."""

    @pytest.mark.asyncio
    async def test_get_current_org_requires_auth(self, client):
        """Getting current org without auth returns 401/403."""
        response = await client.get("/orgs/current")
        assert response.status_code in (401, 403)

    @pytest.mark.asyncio
    async def test_get_current_org_with_auth_no_db(self, client):
        """Getting current org with valid JWT but no DB returns error (404 or 500).

        This test requires a running PostgreSQL instance. It verifies that
        the endpoint is routed correctly and returns an appropriate error
        when the org does not exist.
        """
        headers = _auth_header()
        try:
            response = await client.get("/orgs/current", headers=headers)
            # No org in test DB → 404 or 500
            assert response.status_code in (404, 500)
        except OSError:
            # No PostgreSQL available in CI — connection refused is expected
            pytest.skip("PostgreSQL not available")


# -- Cognito integration tests (mocked) --


class TestCognitoIntegration:
    """Test Cognito helper functions with mocked AWS calls."""

    @pytest.mark.asyncio
    async def test_create_cognito_user_no_config_returns_503(self):
        """When Cognito is not configured, _create_cognito_user raises 503."""
        from fastapi import HTTPException

        from app.routers.auth import _create_cognito_user

        with patch("app.routers.auth.settings") as mock_settings:
            mock_settings.cognito_user_pool_id = ""
            mock_settings.cognito_app_client_id = ""

            with pytest.raises(HTTPException) as exc_info:
                await _create_cognito_user("user@example.com", "SecureP@ss1")
            assert exc_info.value.status_code == 503

    @pytest.mark.asyncio
    async def test_create_cognito_user_success(self):
        """Successful Cognito signup returns the user sub."""
        from app.routers.auth import _create_cognito_user

        mock_client = MagicMock()
        mock_client.sign_up.return_value = {"UserSub": "cognito-sub-123"}
        mock_client.admin_confirm_sign_up.return_value = {}

        with (
            patch("app.routers.auth.settings") as mock_settings,
            patch("app.routers.auth._get_cognito_client", return_value=mock_client),
        ):
            mock_settings.cognito_user_pool_id = "us-east-1_abc123"
            mock_settings.cognito_app_client_id = "client-id-123"
            mock_settings.aws_region = "us-east-1"

            result = await _create_cognito_user("user@example.com", "SecureP@ss1")
            assert result == "cognito-sub-123"

            # Verify Cognito was called correctly
            mock_client.sign_up.assert_called_once()
            mock_client.admin_confirm_sign_up.assert_called_once_with(
                UserPoolId="us-east-1_abc123",
                Username="user@example.com",
            )

    @pytest.mark.asyncio
    async def test_create_cognito_user_duplicate_raises_409(self):
        """Duplicate user in Cognito raises HTTP 409."""
        from botocore.exceptions import ClientError
        from fastapi import HTTPException

        from app.routers.auth import _create_cognito_user

        mock_client = MagicMock()
        mock_client.sign_up.side_effect = ClientError(
            {
                "Error": {
                    "Code": "UsernameExistsException",
                    "Message": "User already exists",
                }
            },
            "SignUp",
        )

        with (
            patch("app.routers.auth.settings") as mock_settings,
            patch("app.routers.auth._get_cognito_client", return_value=mock_client),
        ):
            mock_settings.cognito_user_pool_id = "us-east-1_abc123"
            mock_settings.cognito_app_client_id = "client-id-123"
            mock_settings.aws_region = "us-east-1"

            with pytest.raises(HTTPException) as exc_info:
                await _create_cognito_user("existing@example.com", "SecureP@ss1")
            assert exc_info.value.status_code == 409

    @pytest.mark.asyncio
    async def test_create_cognito_user_invalid_password_raises_422(self):
        """Weak password in Cognito raises HTTP 422."""
        from botocore.exceptions import ClientError
        from fastapi import HTTPException

        from app.routers.auth import _create_cognito_user

        mock_client = MagicMock()
        mock_client.sign_up.side_effect = ClientError(
            {
                "Error": {
                    "Code": "InvalidPasswordException",
                    "Message": "Password too weak",
                }
            },
            "SignUp",
        )

        with (
            patch("app.routers.auth.settings") as mock_settings,
            patch("app.routers.auth._get_cognito_client", return_value=mock_client),
        ):
            mock_settings.cognito_user_pool_id = "us-east-1_abc123"
            mock_settings.cognito_app_client_id = "client-id-123"
            mock_settings.aws_region = "us-east-1"

            with pytest.raises(HTTPException) as exc_info:
                await _create_cognito_user("user@example.com", "weak")
            assert exc_info.value.status_code == 422
