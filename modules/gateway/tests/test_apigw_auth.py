"""
Tests for API Gateway header-based authentication.

Issue #240 added a path where the API Gateway Lambda authorizer asserted an
identity to this service via X-Auth-Source + X-Agent-* headers, which
extract_api_gateway_context() turned into a full TokenContext.

Issue #3985 REMOVED that path. The Lambda authorizer was deprecated and
unattached and /{proxy+} is authorization NONE, so nothing trusted was setting
those headers — but the code still believed them, which let any client able to
reach the pod (including via the public CloudFront edge) mint an identity for an
arbitrary org_id/user_id by setting X-Auth-Source: iam.

The ~20 tests that exercised extract_api_gateway_context() were deleted with the
function. What remains here:
- X-Auth-Source / X-Agent-* no longer produce a TokenContext (the inverted
  assertions — these are the regression guard against reintroducing the sink)
- BG_TRUST_APIGW_HEADERS flag plumbing
- TokenContextMiddleware IAM path (X-Caller-Identity -> agent registry) and its
  ENFORCED_PATHS gating

The surviving IAM path is covered in tests/auth/test_iam_identity.py.
"""

import os
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient

from src.auth.middleware import (
    API_GATEWAY_HEADER_CALLER_IDENTITY,
    TokenContextMiddleware,
)

# =============================================================================
# Fixtures
# =============================================================================

# The header set that used to authenticate a request. Kept verbatim so the tests
# below prove these exact values are now inert.
LEGACY_APIGW_HEADERS_IAM = {
    "X-Auth-Source": "iam",
    "X-Agent-Id": "test-agent",
    "X-Agent-OrgId": "org-apigw-test",
    "X-Agent-TeamId": "team-apigw-test",
    "X-Agent-UserId": "user-apigw-test",
    "X-Agent-AccountType": "service",
    "X-Agent-DepartmentId": "dept-apigw-test",
}


@pytest.fixture
def legacy_apigw_headers_iam():
    return dict(LEGACY_APIGW_HEADERS_IAM)


# =============================================================================
# Issue #3985: X-Auth-Source / X-Agent-* must not mint an identity
# =============================================================================


class TestApiGatewayHeadersNoLongerAuthenticate:
    """The #240 header path is gone. These are the inverted assertions.

    Previously TestExtractApiGatewayContext asserted that this header set
    produced a TokenContext with org_id="org-apigw-test" — including with trust
    ENABLED. Now it must never authenticate, regardless of the trust flag.
    """

    def test_extract_api_gateway_context_is_gone(self):
        """The function itself must not come back."""
        import src.auth.middleware as mw

        assert not hasattr(mw, "extract_api_gateway_context"), (
            "extract_api_gateway_context was removed by #3985 — it built a TokenContext from unauthenticated headers. Do not reintroduce it."
        )

    @pytest.mark.asyncio
    async def test_headers_do_not_authenticate_with_trust_enabled(self, legacy_apigw_headers_iam):
        """Inverted from test_headers_accepted_when_trust_enabled.

        This is the core f-72ed7277 assertion: even with
        BG_TRUST_APIGW_HEADERS=true, X-Auth-Source + X-Agent-OrgId must not
        produce a context, so LLM spend cannot be attributed to an arbitrary org.
        """
        from fastapi import HTTPException

        from src.proxy.routes import get_token_context

        mock_request = MagicMock(spec=Request)
        mock_request.headers = legacy_apigw_headers_iam

        class MockState:
            pass

        mock_request.state = MockState()

        mock_settings = MagicMock()
        mock_settings.trust_apigw_headers = True

        with patch("src.proxy.routes.get_settings", return_value=mock_settings):
            with pytest.raises(HTTPException) as exc_info:
                await get_token_context(mock_request, authorization=None, x_api_key=None)

        assert exc_info.value.status_code == 401
        assert exc_info.value.detail["error"] == "missing_token"

    @pytest.mark.asyncio
    async def test_headers_do_not_authenticate_with_trust_disabled(self, legacy_apigw_headers_iam):
        """Same, with the flag off — the pre-existing behavior, kept as a guard."""
        from fastapi import HTTPException

        from src.proxy.routes import get_token_context

        mock_request = MagicMock(spec=Request)
        mock_request.headers = legacy_apigw_headers_iam

        class MockState:
            pass

        mock_request.state = MockState()

        mock_settings = MagicMock()
        mock_settings.trust_apigw_headers = False

        with patch("src.proxy.routes.get_settings", return_value=mock_settings):
            with pytest.raises(HTTPException) as exc_info:
                await get_token_context(mock_request, authorization=None, x_api_key=None)

        assert exc_info.value.status_code == 401
        assert exc_info.value.detail["error"] == "missing_token"

    @pytest.mark.asyncio
    async def test_middleware_sets_no_context_from_legacy_headers(self, legacy_apigw_headers_iam):
        """Inverted from test_middleware_extracts_apigw_context_when_trusted.

        That test only asserted a 200; this asserts the stronger property the
        original missed — that no token_context is produced.
        """
        app = FastAPI()

        @app.get("/v1/messages")
        async def endpoint(request: Request):
            return {"has_context": hasattr(request.state, "token_context")}

        app.add_middleware(TokenContextMiddleware)

        mock_settings = MagicMock()
        mock_settings.trust_apigw_headers = True

        with patch("src.auth.middleware.get_settings", return_value=mock_settings):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.get("/v1/messages", headers=legacy_apigw_headers_iam)

        assert response.status_code == 200
        assert response.json()["has_context"] is False

    @pytest.mark.asyncio
    async def test_budget_middleware_ignores_agent_budget_config_header(self):
        """Issue #3985: X-Agent-BudgetConfigId is no longer read.

        Replaces test_middleware_get_agent_budget_config_id_with_trust in
        tests/test_agent_budget.py. The header let a caller name any budget
        config — e.g. a fresh, unspent one — and dodge its own agent budget.
        """
        from src.budget.enforcement_middleware import BudgetEnforcementMiddleware

        assert not hasattr(BudgetEnforcementMiddleware, "_get_agent_budget_config_id"), (
            "_get_agent_budget_config_id was removed by #3985 — it trusted "
            "X-Agent-BudgetConfigId on presence alone. Resolve the config id from "
            "the agent registry instead."
        )


# =============================================================================
# Trust flag plumbing
# =============================================================================


class TestTrustFlagBehavior:
    """BG_TRUST_APIGW_HEADERS still gates the surviving IAM path."""

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
# TokenContextMiddleware — surviving behaviors
# =============================================================================


class TestTokenContextMiddleware:
    """Tests for TokenContextMiddleware fall-through and path gating."""

    @pytest.mark.asyncio
    async def test_middleware_falls_back_to_jwt_when_no_headers(self):
        """Test: middleware falls back to JWT auth when identity headers are absent."""
        app = FastAPI()

        @app.get("/v1/messages")
        async def endpoint(request: Request):
            return {"has_context": hasattr(request.state, "token_context")}

        app.add_middleware(TokenContextMiddleware)

        mock_settings = MagicMock()
        mock_settings.trust_apigw_headers = True

        with patch("src.auth.middleware.get_settings", return_value=mock_settings):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.get("/v1/messages")

        # Without a valid JWT, middleware skips auth (route handler will reject)
        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_middleware_skips_non_proxy_paths(self):
        """Test: middleware does not run IAM extraction for non-proxy paths."""
        app = FastAPI()

        @app.get("/health")
        async def health_endpoint(request: Request):
            return {"has_context": hasattr(request.state, "token_context")}

        app.add_middleware(TokenContextMiddleware)

        mock_settings = MagicMock()
        mock_settings.trust_apigw_headers = True

        with patch("src.auth.middleware.get_settings", return_value=mock_settings):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.get(
                    "/health",
                    headers={API_GATEWAY_HEADER_CALLER_IDENTITY: "arn:aws:sts::111122223333:assumed-role/agent/session"},
                )

        assert response.status_code == 200
        assert response.json()["has_context"] is False


# =============================================================================
# Issue #2809: Auth middleware must gate IAM extraction on the shared
# ENFORCED_PATHS registry (not a private duplicate that missed the mantle path)
# =============================================================================


class TestAuthMiddlewareEnforcedPaths:
    """Tests that TokenContextMiddleware runs IAM extraction for every enforced
    proxy path — including the OpenAI Responses passthrough (#2809) — and skips
    non-proxy paths.
    """

    def _build_app(self):
        app = FastAPI()

        @app.get("/{full_path:path}")
        async def catch_all(request: Request, full_path: str):
            return {"has_context": hasattr(request.state, "token_context")}

        app.add_middleware(TokenContextMiddleware)
        return app

    @pytest.mark.asyncio
    async def test_iam_extraction_runs_for_mantle_path(self):
        """Regression (#2809): the OpenAI Responses path must trigger IAM
        extraction. Before the fix, /openai/v1/responses was absent from the
        auth middleware's path list, so extraction never ran and Codex 401'd.
        """
        app = self._build_app()

        mock_settings = MagicMock()
        mock_settings.trust_apigw_headers = True

        with (
            patch("src.auth.middleware.get_settings", return_value=mock_settings),
            patch("src.auth.middleware.extract_iam_identity_from_headers", return_value=None) as mock_extract,
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.get(
                    "/openai/v1/responses",
                    headers={API_GATEWAY_HEADER_CALLER_IDENTITY: "arn:aws:sts::111122223333:assumed-role/agent/session"},
                )

        assert response.status_code == 200
        mock_extract.assert_called_once()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "path",
        [
            "/v1/chat/completions",
            "/v1/messages",
            "/bedrock/invoke",
            "/bedrock/invoke-with-response-stream",
            "/model/some-model/invoke",
        ],
    )
    async def test_iam_extraction_runs_for_preexisting_proxy_paths(self, path):
        """Guards against a list swap dropping the five pre-existing proxy
        paths — every one must still trigger IAM extraction.
        """
        app = self._build_app()

        mock_settings = MagicMock()
        mock_settings.trust_apigw_headers = True

        with (
            patch("src.auth.middleware.get_settings", return_value=mock_settings),
            patch("src.auth.middleware.extract_iam_identity_from_headers", return_value=None) as mock_extract,
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.get(
                    path,
                    headers={API_GATEWAY_HEADER_CALLER_IDENTITY: "arn:aws:sts::111122223333:assumed-role/agent/session"},
                )

        assert response.status_code == 200
        mock_extract.assert_called_once()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("path", ["/health", "/admin/models", "/"])
    async def test_iam_extraction_skipped_for_non_proxy_paths(self, path):
        """Non-proxy paths (health, admin, root) must NOT trigger IAM extraction
        — widening the list to these routes would risk auth bypass / spurious
        errors on admin and health surfaces.
        """
        app = self._build_app()

        mock_settings = MagicMock()
        mock_settings.trust_apigw_headers = True

        with (
            patch("src.auth.middleware.get_settings", return_value=mock_settings),
            patch("src.auth.middleware.extract_iam_identity_from_headers", return_value=None) as mock_extract,
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.get(
                    path,
                    headers={API_GATEWAY_HEADER_CALLER_IDENTITY: "arn:aws:sts::111122223333:assumed-role/agent/session"},
                )

        assert response.status_code == 200
        assert response.json()["has_context"] is False
        mock_extract.assert_not_called()

    def test_auth_middleware_uses_shared_enforced_paths(self):
        """Single-source guarantee (#2809): the auth middleware must reference
        the SAME ENFORCED_PATHS object as the shared registry — not a private
        duplicate. Mirrors tests/shared/test_enforced_paths.py.
        """
        from src.auth import middleware as auth_mw
        from src.shared.enforced_paths import ENFORCED_PATHS

        assert auth_mw.ENFORCED_PATHS is ENFORCED_PATHS
