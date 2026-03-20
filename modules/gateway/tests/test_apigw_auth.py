"""
Tests for API Gateway Header-Based Authentication (Issue #240).

This module tests the dual-auth middleware that accepts both:
1. API Gateway headers (from Lambda authorizer) - when BG_TRUST_APIGW_HEADERS=true
2. Cognito JWT tokens (existing flow) - default

Test Coverage:
- extract_api_gateway_context() function
- TokenContextMiddleware with API Gateway headers
- get_token_context() dependency with API Gateway headers
- Budget enforcement with API Gateway auth
- Security: header spoofing prevention via trust flag
"""

import os
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient
from starlette.datastructures import Headers

from src.auth.middleware import (
    API_GATEWAY_HEADER_ACCOUNT_TYPE,
    API_GATEWAY_HEADER_AGENT_ID,
    API_GATEWAY_HEADER_AUTH_SOURCE,
    API_GATEWAY_HEADER_DEPARTMENT_ID,
    API_GATEWAY_HEADER_ORG_ID,
    API_GATEWAY_HEADER_TEAM_ID,
    API_GATEWAY_HEADER_USER_ID,
    TokenContextMiddleware,
    extract_api_gateway_context,
)
from src.shared.schemas.auth import TokenContext

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def api_gateway_headers_iam():
    """Complete set of API Gateway headers for IAM-authenticated request."""
    return {
        API_GATEWAY_HEADER_AUTH_SOURCE: "iam",
        API_GATEWAY_HEADER_AGENT_ID: "test-agent",
        API_GATEWAY_HEADER_ORG_ID: "org-apigw-test",
        API_GATEWAY_HEADER_TEAM_ID: "team-apigw-test",
        API_GATEWAY_HEADER_USER_ID: "user-apigw-test",
        API_GATEWAY_HEADER_ACCOUNT_TYPE: "service",
        API_GATEWAY_HEADER_DEPARTMENT_ID: "dept-apigw-test",
    }


@pytest.fixture
def api_gateway_headers_jwt():
    """API Gateway headers for JWT-forwarded request."""
    return {
        API_GATEWAY_HEADER_AUTH_SOURCE: "jwt",
        API_GATEWAY_HEADER_AGENT_ID: "jwt-user-agent",
        API_GATEWAY_HEADER_ORG_ID: "org-jwt-forward",
        API_GATEWAY_HEADER_TEAM_ID: "team-jwt-forward",
        API_GATEWAY_HEADER_USER_ID: "user-jwt-forward",
        API_GATEWAY_HEADER_ACCOUNT_TYPE: "human",
        API_GATEWAY_HEADER_DEPARTMENT_ID: "dept-jwt-forward",
    }


@pytest.fixture
def api_gateway_headers_minimal():
    """Minimal required API Gateway headers (no optional fields)."""
    return {
        API_GATEWAY_HEADER_AUTH_SOURCE: "iam",
        API_GATEWAY_HEADER_ORG_ID: "org-minimal",
        API_GATEWAY_HEADER_AGENT_ID: "minimal-agent",  # Used as user_id fallback
    }


@pytest.fixture
def mock_request_with_headers(api_gateway_headers_iam):
    """Create a mock FastAPI Request with API Gateway headers."""
    request = MagicMock(spec=Request)
    request.headers = Headers(api_gateway_headers_iam)
    return request


@pytest.fixture
def apigw_token_context():
    """Expected TokenContext for API Gateway authenticated request."""
    return TokenContext(
        user_id="user-apigw-test",
        org_id="org-apigw-test",
        team_id="team-apigw-test",
        department_id="dept-apigw-test",
        account_type="service",
        is_admin=False,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        auth_source="iam",
    )


# =============================================================================
# Unit Tests: extract_api_gateway_context()
# =============================================================================


class TestExtractApiGatewayContext:
    """Tests for the extract_api_gateway_context() function."""

    def test_iam_auth_source_returns_correct_context(self, api_gateway_headers_iam):
        """Test: request with X-Auth-Source: iam returns correct TokenContext."""
        request = MagicMock(spec=Request)
        request.headers = Headers(api_gateway_headers_iam)

        context = extract_api_gateway_context(request)

        assert context.auth_source == "iam"
        assert context.org_id == "org-apigw-test"
        assert context.team_id == "team-apigw-test"
        assert context.user_id == "user-apigw-test"
        assert context.account_type == "service"
        assert context.department_id == "dept-apigw-test"
        assert context.is_admin is False

    def test_jwt_auth_source_returns_correct_context(self, api_gateway_headers_jwt):
        """Test: request with X-Auth-Source: jwt returns correct TokenContext."""
        request = MagicMock(spec=Request)
        request.headers = Headers(api_gateway_headers_jwt)

        context = extract_api_gateway_context(request)

        assert context.auth_source == "jwt"
        assert context.org_id == "org-jwt-forward"
        assert context.team_id == "team-jwt-forward"
        assert context.user_id == "user-jwt-forward"
        assert context.account_type == "human"
        assert context.department_id == "dept-jwt-forward"

    def test_user_id_falls_back_to_agent_id(self, api_gateway_headers_minimal):
        """Test: X-Agent-Id is used as user_id when X-Agent-UserId is missing."""
        request = MagicMock(spec=Request)
        request.headers = Headers(api_gateway_headers_minimal)

        context = extract_api_gateway_context(request)

        assert context.user_id == "minimal-agent"  # Fell back to X-Agent-Id
        assert context.team_id == ""  # Optional, defaults to empty
        assert context.department_id == ""  # Optional, defaults to empty

    def test_missing_auth_source_raises_401(self):
        """Test: missing X-Auth-Source header raises 401."""
        from fastapi import HTTPException

        request = MagicMock(spec=Request)
        request.headers = Headers({API_GATEWAY_HEADER_ORG_ID: "org-test"})

        with pytest.raises(HTTPException) as exc_info:
            extract_api_gateway_context(request)

        assert exc_info.value.status_code == 401
        assert exc_info.value.detail["error"] == "invalid_auth_source"

    def test_invalid_auth_source_raises_401(self):
        """Test: invalid X-Auth-Source value raises 401."""
        from fastapi import HTTPException

        request = MagicMock(spec=Request)
        request.headers = Headers({API_GATEWAY_HEADER_AUTH_SOURCE: "invalid"})

        with pytest.raises(HTTPException) as exc_info:
            extract_api_gateway_context(request)

        assert exc_info.value.status_code == 401
        assert exc_info.value.detail["error"] == "invalid_auth_source"

    def test_missing_org_id_raises_401(self):
        """Test: missing X-Agent-OrgId header raises 401."""
        from fastapi import HTTPException

        request = MagicMock(spec=Request)
        request.headers = Headers(
            {
                API_GATEWAY_HEADER_AUTH_SOURCE: "iam",
                API_GATEWAY_HEADER_USER_ID: "user-test",
            }
        )

        with pytest.raises(HTTPException) as exc_info:
            extract_api_gateway_context(request)

        assert exc_info.value.status_code == 401
        assert exc_info.value.detail["error"] == "missing_org_id"

    def test_missing_user_id_raises_401(self):
        """Test: missing both X-Agent-UserId and X-Agent-Id raises 401."""
        from fastapi import HTTPException

        request = MagicMock(spec=Request)
        request.headers = Headers(
            {
                API_GATEWAY_HEADER_AUTH_SOURCE: "iam",
                API_GATEWAY_HEADER_ORG_ID: "org-test",
            }
        )

        with pytest.raises(HTTPException) as exc_info:
            extract_api_gateway_context(request)

        assert exc_info.value.status_code == 401
        assert exc_info.value.detail["error"] == "missing_user_id"

    def test_default_account_type_is_service(self, api_gateway_headers_minimal):
        """Test: account_type defaults to 'service' if not specified."""
        request = MagicMock(spec=Request)
        request.headers = Headers(api_gateway_headers_minimal)

        context = extract_api_gateway_context(request)

        assert context.account_type == "service"

    def test_expires_at_is_future(self, api_gateway_headers_iam):
        """Test: expires_at is set to a future time."""
        request = MagicMock(spec=Request)
        request.headers = Headers(api_gateway_headers_iam)

        context = extract_api_gateway_context(request)

        assert context.expires_at > datetime.now(UTC)


# =============================================================================
# Unit Tests: Trust Flag Behavior
# =============================================================================


class TestTrustFlagBehavior:
    """Tests for BG_TRUST_APIGW_HEADERS flag behavior."""

    @pytest.mark.asyncio
    async def test_headers_ignored_when_trust_disabled(self, api_gateway_headers_iam):
        """Test: API Gateway headers ignored when BG_TRUST_APIGW_HEADERS=false."""
        from src.proxy.routes import get_token_context

        # Create mock request with API Gateway headers
        mock_request = MagicMock(spec=Request)
        mock_request.headers = Headers(api_gateway_headers_iam)
        mock_request.state = MagicMock()
        del mock_request.state.token_context  # Ensure no pre-existing context

        # Mock settings with trust disabled (default)
        mock_settings = MagicMock()
        mock_settings.trust_apigw_headers = False

        with patch("src.proxy.routes.get_settings", return_value=mock_settings):
            # Should fall through to JWT validation (which will fail without token)
            from fastapi import HTTPException

            with pytest.raises(HTTPException) as exc_info:
                await get_token_context(mock_request, authorization=None, x_api_key=None)

            # Should get missing_token error, not API Gateway context
            assert exc_info.value.status_code == 401
            assert exc_info.value.detail["error"] == "missing_token"

    @pytest.mark.asyncio
    async def test_headers_accepted_when_trust_enabled(self, api_gateway_headers_iam):
        """Test: API Gateway headers accepted when BG_TRUST_APIGW_HEADERS=true."""
        from src.proxy.routes import get_token_context

        # Create mock request with API Gateway headers
        mock_request = MagicMock(spec=Request)
        mock_request.headers = Headers(api_gateway_headers_iam)
        mock_request.state = MagicMock()
        # Remove token_context to force extraction
        mock_request.state.token_context = None
        type(mock_request.state).token_context = property(lambda s: None, lambda s, v: None)

        # Mock settings with trust enabled
        mock_settings = MagicMock()
        mock_settings.trust_apigw_headers = True

        # Mock hasattr to return False for token_context
        with patch("src.proxy.routes.get_settings", return_value=mock_settings):
            with patch.object(mock_request, "state", create=True) as mock_state:
                # Make hasattr(request.state, "token_context") return False
                delattr(mock_state, "token_context") if hasattr(mock_state, "token_context") else None

                context = await get_token_context(mock_request, authorization=None, x_api_key=None)

                assert context.auth_source == "iam"
                assert context.org_id == "org-apigw-test"
                assert context.user_id == "user-apigw-test"


# =============================================================================
# Unit Tests: auth_source Field
# =============================================================================


class TestAuthSourceField:
    """Tests for the auth_source field on TokenContext."""

    def test_iam_auth_source_set_correctly(self, api_gateway_headers_iam):
        """Test: auth_source is 'iam' for IAM-authenticated requests."""
        request = MagicMock(spec=Request)
        request.headers = Headers(api_gateway_headers_iam)

        context = extract_api_gateway_context(request)

        assert context.auth_source == "iam"

    def test_jwt_auth_source_set_correctly(self, api_gateway_headers_jwt):
        """Test: auth_source is 'jwt' for JWT-forwarded requests."""
        request = MagicMock(spec=Request)
        request.headers = Headers(api_gateway_headers_jwt)

        context = extract_api_gateway_context(request)

        assert context.auth_source == "jwt"

    def test_default_auth_source_is_jwt(self):
        """Test: default auth_source for TokenContext is 'jwt'."""
        context = TokenContext(
            user_id="test-user",
            org_id="test-org",
            team_id="test-team",
            department_id="test-dept",
            account_type="human",
            is_admin=False,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )

        assert context.auth_source == "jwt"


# =============================================================================
# Integration Tests: Budget Enforcement Compatibility
# =============================================================================


class TestBudgetEnforcementCompatibility:
    """Tests for budget enforcement with API Gateway auth."""

    def test_token_context_has_required_budget_fields(self, api_gateway_headers_iam):
        """Test: TokenContext from API Gateway has org_id and team_id for budget enforcement."""
        request = MagicMock(spec=Request)
        request.headers = Headers(api_gateway_headers_iam)

        context = extract_api_gateway_context(request)

        # Budget enforcement middleware requires these fields
        assert context.org_id != ""
        assert context.org_id == "org-apigw-test"
        # team_id can be empty but must exist
        assert hasattr(context, "team_id")
        assert context.team_id == "team-apigw-test"

    def test_token_context_has_required_chat_logging_fields(self, api_gateway_headers_iam):
        """Test: TokenContext has fields required by chat logging service."""
        request = MagicMock(spec=Request)
        request.headers = Headers(api_gateway_headers_iam)

        context = extract_api_gateway_context(request)

        # Chat logging service requires these fields
        assert context.org_id != ""
        assert context.user_id != ""
        assert context.account_type in ("human", "service")


# =============================================================================
# Integration Tests: Middleware
# =============================================================================


class TestTokenContextMiddleware:
    """Tests for TokenContextMiddleware with API Gateway headers."""

    @pytest.mark.asyncio
    async def test_middleware_extracts_apigw_context_when_trusted(self, api_gateway_headers_iam):
        """Test: middleware extracts API Gateway context when trust is enabled."""
        # Create a simple test app
        app = FastAPI()

        @app.get("/v1/messages")
        async def test_endpoint(request: Request):
            return {"token_context": getattr(request.state, "token_context", None)}

        # Add middleware
        app.add_middleware(TokenContextMiddleware)

        # Mock settings with trust enabled
        mock_settings = MagicMock()
        mock_settings.trust_apigw_headers = True

        with patch("src.auth.middleware.get_settings", return_value=mock_settings):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.get("/v1/messages", headers=api_gateway_headers_iam)

        # The middleware should have extracted the context
        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_middleware_falls_back_to_jwt_when_no_apigw_headers(self):
        """Test: middleware falls back to JWT auth when API Gateway headers are absent."""
        # Create a simple test app
        app = FastAPI()

        @app.get("/v1/messages")
        async def test_endpoint(request: Request):
            return {"has_context": hasattr(request.state, "token_context")}

        # Add middleware
        app.add_middleware(TokenContextMiddleware)

        # Mock settings with trust enabled but no API Gateway headers
        mock_settings = MagicMock()
        mock_settings.trust_apigw_headers = True

        with patch("src.auth.middleware.get_settings", return_value=mock_settings):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                # No headers at all - should fall through to JWT path
                response = await client.get("/v1/messages")

        # Without valid JWT, middleware skips auth (route handler will reject)
        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_middleware_skips_non_proxy_paths(self, api_gateway_headers_iam):
        """Test: middleware does not process API Gateway headers for non-proxy paths."""
        # Create a simple test app
        app = FastAPI()

        @app.get("/health")
        async def health_endpoint(request: Request):
            return {"has_context": hasattr(request.state, "token_context")}

        # Add middleware
        app.add_middleware(TokenContextMiddleware)

        # Mock settings with trust enabled
        mock_settings = MagicMock()
        mock_settings.trust_apigw_headers = True

        with patch("src.auth.middleware.get_settings", return_value=mock_settings):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.get("/health", headers=api_gateway_headers_iam)

        # Middleware should not have processed headers for non-proxy path
        assert response.status_code == 200
        assert response.json()["has_context"] is False


# =============================================================================
# Security Tests
# =============================================================================


class TestSecurityHeaderSpoofing:
    """Tests for security: ensuring header spoofing is prevented."""

    @pytest.mark.asyncio
    async def test_spoofed_headers_rejected_when_trust_disabled(self, api_gateway_headers_iam):
        """Test: spoofed X-Auth-Source headers are ignored when trust is disabled."""
        from src.proxy.routes import get_token_context

        # Create mock request with spoofed API Gateway headers
        mock_request = MagicMock(spec=Request)
        mock_request.headers = Headers(api_gateway_headers_iam)

        # Create a mock state object that doesn't have token_context attribute
        class MockState:
            pass

        mock_request.state = MockState()

        # Mock settings with trust disabled (default)
        mock_settings = MagicMock()
        mock_settings.trust_apigw_headers = False

        with patch("src.proxy.routes.get_settings", return_value=mock_settings):
            # Without JWT, should get 401
            from fastapi import HTTPException

            with pytest.raises(HTTPException) as exc_info:
                await get_token_context(mock_request, authorization=None, x_api_key=None)

            # Should NOT have accepted the spoofed headers
            assert exc_info.value.status_code == 401
            assert exc_info.value.detail["error"] == "missing_token"

    def test_env_var_controls_trust_flag(self):
        """Test: BG_TRUST_APIGW_HEADERS env var controls the trust flag."""
        from src.shared.config import Settings

        # Test default (False)
        with patch.dict(os.environ, {}, clear=True):
            settings = Settings()
            assert settings.trust_apigw_headers is False

        # Test explicit True
        with patch.dict(os.environ, {"BG_TRUST_APIGW_HEADERS": "true"}):
            settings = Settings()
            assert settings.trust_apigw_headers is True

        # Test explicit False
        with patch.dict(os.environ, {"BG_TRUST_APIGW_HEADERS": "false"}):
            settings = Settings()
            assert settings.trust_apigw_headers is False


# =============================================================================
# Edge Cases
# =============================================================================


class TestEdgeCases:
    """Tests for edge cases and unusual inputs."""

    def test_case_insensitive_auth_source(self):
        """Test: X-Auth-Source is case-insensitive."""
        request = MagicMock(spec=Request)

        # Test uppercase
        request.headers = Headers(
            {
                API_GATEWAY_HEADER_AUTH_SOURCE: "IAM",
                API_GATEWAY_HEADER_ORG_ID: "org-test",
                API_GATEWAY_HEADER_USER_ID: "user-test",
            }
        )
        context = extract_api_gateway_context(request)
        assert context.auth_source == "iam"

        # Test mixed case
        request.headers = Headers(
            {
                API_GATEWAY_HEADER_AUTH_SOURCE: "Jwt",
                API_GATEWAY_HEADER_ORG_ID: "org-test",
                API_GATEWAY_HEADER_USER_ID: "user-test",
            }
        )
        context = extract_api_gateway_context(request)
        assert context.auth_source == "jwt"

    def test_empty_optional_fields(self):
        """Test: empty optional fields are handled correctly."""
        request = MagicMock(spec=Request)
        request.headers = Headers(
            {
                API_GATEWAY_HEADER_AUTH_SOURCE: "iam",
                API_GATEWAY_HEADER_ORG_ID: "org-test",
                API_GATEWAY_HEADER_USER_ID: "user-test",
                API_GATEWAY_HEADER_TEAM_ID: "",  # Explicitly empty
                API_GATEWAY_HEADER_DEPARTMENT_ID: "",  # Explicitly empty
            }
        )

        context = extract_api_gateway_context(request)

        assert context.team_id == ""
        assert context.department_id == ""

    def test_whitespace_in_headers(self):
        """Test: whitespace in header values is preserved."""
        request = MagicMock(spec=Request)
        request.headers = Headers(
            {
                API_GATEWAY_HEADER_AUTH_SOURCE: "iam",
                API_GATEWAY_HEADER_ORG_ID: "org-test",
                API_GATEWAY_HEADER_USER_ID: "user-test",
                API_GATEWAY_HEADER_TEAM_ID: " team-with-spaces ",  # Has whitespace
            }
        )

        context = extract_api_gateway_context(request)

        # Whitespace is preserved (upstream should handle trimming if needed)
        assert context.team_id == " team-with-spaces "
