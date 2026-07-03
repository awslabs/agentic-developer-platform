"""Unit tests for GitHub App registration via manifest conversion flow.

Issue #2593: Platform-admin endpoints for registering a GitHub App.
Tests cover:
- Platform-admin access control (403 for non-platform-admins)
- State nonce generation and verification
- Already-registered detection
- Private key never in responses or logs
- Manifest structure validation
- Callback error handling
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.admin.connections.routes import router
from src.admin.connections.schemas import RegisterAppStartResponse
from src.auth.dependencies import get_current_user
from src.shared.database import get_db
from src.shared.schemas.auth import TokenContext

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_user(
    *,
    is_admin: bool = False,
    org_id: str = "org-001",
    user_id: str = "user-001",
) -> TokenContext:
    return TokenContext(
        user_id=user_id,
        org_id=org_id,
        team_id="team-001",
        department_id="dept-001",
        account_type="human",
        is_admin=is_admin,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )


@pytest.fixture
def app():
    application = FastAPI()
    application.include_router(router)
    return application


@pytest.fixture
def mock_db():
    return MagicMock()


def _make_client(
    app: FastAPI,
    *,
    user: TokenContext,
    mock_db: MagicMock,
) -> TestClient:
    async def override_get_current_user():
        return user

    async def override_get_db():
        yield mock_db

    app.dependency_overrides[get_current_user] = override_get_current_user
    app.dependency_overrides[get_db] = override_get_db
    return TestClient(app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# POST /admin/connections/github/app/register-start
# ---------------------------------------------------------------------------


class TestRegisterAppStartRoute:
    """Tests for the register-start endpoint."""

    def test_non_admin_returns_403(self, app, mock_db):
        """Non-admin users must be rejected with 403."""
        user = _make_user(is_admin=False)
        client = _make_client(app, user=user, mock_db=mock_db)

        resp = client.post(
            "/admin/connections/github/app/register-start",
            json={"owner_type": "org", "org": "test-org"},
        )
        assert resp.status_code == 403
        assert "platform administrator" in resp.json()["detail"].lower()

    def test_platform_admin_gets_manifest(self, app, mock_db):
        """Platform admin receives a ready response with manifest."""
        user = _make_user(is_admin=True)
        client = _make_client(app, user=user, mock_db=mock_db)

        expected = RegisterAppStartResponse(
            status="ready",
            manifest={
                "name": "adp-agent-platform",
                "url": "https://github.com/apps/adp-agent-platform",
                "hook_attributes": {"url": "https://webhook.example.com", "active": True},
                "redirect_url": "https://gw.example.com/api/admin/connections/github/app/register-callback",
                "public": False,
                "default_permissions": {
                    "contents": "write",
                    "issues": "write",
                    "pull_requests": "write",
                    "checks": "write",
                    "metadata": "read",
                },
                "default_events": [
                    "issues",
                    "issue_comment",
                    "pull_request",
                    "pull_request_review",
                    "pull_request_review_comment",
                    "label",
                ],
            },
            post_url="https://github.com/organizations/test-org/settings/apps/new",
            state="test-state-123",
        )

        with patch(
            "src.admin.connections.routes.register_app_start",
            new=AsyncMock(return_value=expected),
        ):
            resp = client.post(
                "/admin/connections/github/app/register-start",
                json={"owner_type": "org", "org": "test-org"},
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ready"
        assert body["manifest"] is not None
        assert body["post_url"] == "https://github.com/organizations/test-org/settings/apps/new"
        assert body["state"] == "test-state-123"

    def test_already_registered_returns_status(self, app, mock_db):
        """When an App is already registered, status='already_registered'."""
        user = _make_user(is_admin=True)
        client = _make_client(app, user=user, mock_db=mock_db)

        expected = RegisterAppStartResponse(
            status="already_registered",
            app_slug="adp-agent-platform",
            app_id="3410773",
        )

        with patch(
            "src.admin.connections.routes.register_app_start",
            new=AsyncMock(return_value=expected),
        ):
            resp = client.post(
                "/admin/connections/github/app/register-start",
                json={"owner_type": "org", "org": "test-org"},
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "already_registered"
        assert body["app_slug"] == "adp-agent-platform"
        assert body["app_id"] == "3410773"
        # Manifest should not be present for already_registered
        assert body["manifest"] is None

    def test_user_owner_type_uses_personal_url(self, app, mock_db):
        """owner_type='user' yields the personal GitHub settings URL."""
        user = _make_user(is_admin=True)
        client = _make_client(app, user=user, mock_db=mock_db)

        expected = RegisterAppStartResponse(
            status="ready",
            manifest={"name": "adp-agent-platform"},
            post_url="https://github.com/settings/apps/new",
            state="state-456",
        )

        with patch(
            "src.admin.connections.routes.register_app_start",
            new=AsyncMock(return_value=expected),
        ):
            resp = client.post(
                "/admin/connections/github/app/register-start",
                json={"owner_type": "user"},
            )

        assert resp.status_code == 200
        assert resp.json()["post_url"] == "https://github.com/settings/apps/new"

    def test_returns_500_on_unexpected_error(self, app, mock_db):
        """Unexpected exceptions return 500."""
        user = _make_user(is_admin=True)
        client = _make_client(app, user=user, mock_db=mock_db)

        with patch(
            "src.admin.connections.routes.register_app_start",
            new=AsyncMock(side_effect=RuntimeError("boom")),
        ):
            resp = client.post(
                "/admin/connections/github/app/register-start",
                json={"owner_type": "org", "org": "test-org"},
            )

        assert resp.status_code == 500


# ---------------------------------------------------------------------------
# GET /admin/connections/github/app/register-callback
# ---------------------------------------------------------------------------


class TestRegisterAppCallbackRoute:
    """Tests for the register-callback endpoint."""

    def test_successful_callback_redirects(self, app, mock_db):
        """Successful code exchange redirects to frontend."""
        user = _make_user(is_admin=True)
        client = _make_client(app, user=user, mock_db=mock_db)

        redirect_url = "/settings/connections?github_app=registered"

        with patch(
            "src.admin.connections.routes.register_app_callback",
            new=AsyncMock(return_value=redirect_url),
        ):
            resp = client.get(
                "/admin/connections/github/app/register-callback?code=abc123&state=state-xyz",
                follow_redirects=False,
            )

        assert resp.status_code == 302
        assert "github_app=registered" in resp.headers["location"]

    def test_missing_code_redirects_to_error(self, app, mock_db):
        """Missing code parameter results in error redirect."""
        user = _make_user(is_admin=True)
        client = _make_client(app, user=user, mock_db=mock_db)

        resp = client.get(
            "/admin/connections/github/app/register-callback?state=state-xyz",
            follow_redirects=False,
        )
        assert resp.status_code == 302
        assert "error=missing_code" in resp.headers["location"]

    def test_missing_state_redirects_to_error(self, app, mock_db):
        """Missing state parameter results in error redirect."""
        user = _make_user(is_admin=True)
        client = _make_client(app, user=user, mock_db=mock_db)

        resp = client.get(
            "/admin/connections/github/app/register-callback?code=abc123",
            follow_redirects=False,
        )
        assert resp.status_code == 302
        assert "error=missing_state" in resp.headers["location"]

    def test_expired_state_redirects_to_error(self, app, mock_db):
        """Expired state nonce results in error redirect."""
        user = _make_user(is_admin=True)
        client = _make_client(app, user=user, mock_db=mock_db)

        from src.auth.magic_link import TokenExpiredError

        with patch(
            "src.admin.connections.routes.register_app_callback",
            new=AsyncMock(side_effect=TokenExpiredError("expired")),
        ):
            resp = client.get(
                "/admin/connections/github/app/register-callback?code=abc123&state=old-state",
                follow_redirects=False,
            )

        assert resp.status_code == 302
        assert "error=invalid_state" in resp.headers["location"]

    def test_consumed_state_redirects_to_error(self, app, mock_db):
        """Already-consumed state nonce results in error redirect."""
        user = _make_user(is_admin=True)
        client = _make_client(app, user=user, mock_db=mock_db)

        from src.auth.magic_link import NonceAlreadyConsumedError

        with patch(
            "src.admin.connections.routes.register_app_callback",
            new=AsyncMock(side_effect=NonceAlreadyConsumedError("used")),
        ):
            resp = client.get(
                "/admin/connections/github/app/register-callback?code=abc123&state=used-state",
                follow_redirects=False,
            )

        assert resp.status_code == 302
        assert "error=state_replayed" in resp.headers["location"]

    def test_nonce_not_found_redirects_to_error(self, app, mock_db):
        """Unknown state nonce results in error redirect."""
        user = _make_user(is_admin=True)
        client = _make_client(app, user=user, mock_db=mock_db)

        from src.auth.magic_link import NonceNotFoundError

        with patch(
            "src.admin.connections.routes.register_app_callback",
            new=AsyncMock(side_effect=NonceNotFoundError("not found")),
        ):
            resp = client.get(
                "/admin/connections/github/app/register-callback?code=abc123&state=bad-state",
                follow_redirects=False,
            )

        assert resp.status_code == 302
        assert "error=invalid_state" in resp.headers["location"]

    def test_github_api_error_redirects(self, app, mock_db):
        """GitHub API failures result in error redirect."""
        user = _make_user(is_admin=True)
        client = _make_client(app, user=user, mock_db=mock_db)

        from fastapi import HTTPException

        with patch(
            "src.admin.connections.routes.register_app_callback",
            new=AsyncMock(side_effect=HTTPException(status_code=502, detail="GitHub error")),
        ):
            resp = client.get(
                "/admin/connections/github/app/register-callback?code=abc123&state=state-xyz",
                follow_redirects=False,
            )

        assert resp.status_code == 302
        assert "error=github_error" in resp.headers["location"]


# ---------------------------------------------------------------------------
# Service-level tests
# ---------------------------------------------------------------------------


class TestRegisterAppStartService:
    """Tests for the register_app_start service function."""

    @pytest.mark.asyncio
    async def test_checks_existing_secret_first(self):
        """register_app_start returns already_registered if secret exists."""
        from src.admin.connections.service import register_app_start

        mock_db = AsyncMock()

        with patch(
            "src.admin.connections.service._check_existing_app_secret",
            return_value=("3410773", "adp-agent-platform"),
        ):
            result = await register_app_start(
                owner_type="org",
                org="test-org",
                cognito_sub="sub-123",
                user_id="user-001",
                db=mock_db,
            )

        assert result.status == "already_registered"
        assert result.app_id == "3410773"
        assert result.app_slug == "adp-agent-platform"
        # No manifest should be generated
        assert result.manifest is None

    @pytest.mark.asyncio
    async def test_generates_manifest_when_no_existing_app(self):
        """register_app_start generates manifest with org-prefixed name (#2677)."""
        from src.admin.connections.service import register_app_start

        mock_db = AsyncMock()

        with (
            patch(
                "src.admin.connections.service._check_existing_app_secret",
                return_value=None,
            ),
            patch(
                "src.admin.connections.service.store_nonce",
                new=AsyncMock(),
            ),
            patch.dict("os.environ", {"WEBHOOK_URL": "https://webhook.test.com/github"}),
            patch(
                "src.admin.connections.service.get_settings",
                return_value=MagicMock(gateway_base_url="https://gw.test.com", github_app_slug=""),
            ),
        ):
            result = await register_app_start(
                owner_type="org",
                org="my-org",
                cognito_sub="sub-123",
                user_id="user-001",
                db=mock_db,
            )

        assert result.status == "ready"
        assert result.manifest is not None
        # Issue #2677: name is org-prefixed for global uniqueness
        assert result.manifest["name"] == "my-org-adp-agent-platform"
        assert result.manifest["url"] == "https://github.com/apps/my-org-adp-agent-platform"
        assert result.manifest["hook_attributes"]["url"] == "https://webhook.test.com/github"
        assert result.manifest["redirect_url"] == "https://gw.test.com/api/admin/connections/github/app/register-callback"
        # Issue #2823: setup_url built from the same base as callback_url
        assert result.manifest["setup_url"] == "https://gw.test.com/api/admin/connections/github/install-callback"
        assert result.manifest["setup_on_update"] is True
        assert result.manifest["public"] is False
        assert result.manifest["default_permissions"]["contents"] == "write"
        assert result.manifest["default_permissions"]["metadata"] == "read"
        assert "issues" in result.manifest["default_events"]
        assert result.post_url == "https://github.com/organizations/my-org/settings/apps/new"
        assert result.state is not None
        assert result.suggested_app_name == "my-org-adp-agent-platform"

    @pytest.mark.asyncio
    async def test_custom_app_name_overrides_default(self):
        """Explicit app_name in request overrides the org-prefixed default (#2677)."""
        from src.admin.connections.service import register_app_start

        mock_db = AsyncMock()

        with (
            patch(
                "src.admin.connections.service._check_existing_app_secret",
                return_value=None,
            ),
            patch(
                "src.admin.connections.service.store_nonce",
                new=AsyncMock(),
            ),
            patch.dict("os.environ", {"WEBHOOK_URL": "https://webhook.test.com/github"}),
            patch(
                "src.admin.connections.service.get_settings",
                return_value=MagicMock(gateway_base_url="https://gw.test.com", github_app_slug=""),
            ),
        ):
            result = await register_app_start(
                owner_type="org",
                org="my-org",
                app_name="custom-app-name",
                cognito_sub="sub-123",
                user_id="user-001",
                db=mock_db,
            )

        assert result.manifest["name"] == "custom-app-name"
        assert result.manifest["url"] == "https://github.com/apps/custom-app-name"
        assert result.suggested_app_name == "custom-app-name"

    @pytest.mark.asyncio
    async def test_user_owner_type_url(self):
        """owner_type='user' produces the personal settings URL and base name (#2677)."""
        from src.admin.connections.service import register_app_start

        mock_db = AsyncMock()

        with (
            patch(
                "src.admin.connections.service._check_existing_app_secret",
                return_value=None,
            ),
            patch(
                "src.admin.connections.service.store_nonce",
                new=AsyncMock(),
            ),
            patch.dict("os.environ", {"WEBHOOK_URL": "https://hook.example.com"}),
            patch(
                "src.admin.connections.service.get_settings",
                return_value=MagicMock(gateway_base_url="https://gw.example.com", github_app_slug=""),
            ),
        ):
            result = await register_app_start(
                owner_type="user",
                org=None,
                cognito_sub="sub-123",
                user_id="user-001",
                db=mock_db,
            )

        assert result.post_url == "https://github.com/settings/apps/new"
        # No org owner context → falls back to base name
        assert result.manifest["name"] == "adp-agent-platform"

    @pytest.mark.asyncio
    async def test_invalid_owner_type_raises_400(self):
        """Invalid owner_type raises HTTPException 400."""
        from fastapi import HTTPException

        from src.admin.connections.service import register_app_start

        mock_db = AsyncMock()

        with patch(
            "src.admin.connections.service._check_existing_app_secret",
            return_value=None,
        ):
            with pytest.raises(HTTPException) as exc_info:
                await register_app_start(
                    owner_type="invalid",
                    org=None,
                    cognito_sub="sub-123",
                    user_id="user-001",
                    db=mock_db,
                )
            assert exc_info.value.status_code == 400

    @pytest.mark.asyncio
    async def test_org_required_when_owner_type_org(self):
        """owner_type='org' without org raises HTTPException 400."""
        from fastapi import HTTPException

        from src.admin.connections.service import register_app_start

        mock_db = AsyncMock()

        with patch(
            "src.admin.connections.service._check_existing_app_secret",
            return_value=None,
        ):
            with pytest.raises(HTTPException) as exc_info:
                await register_app_start(
                    owner_type="org",
                    org=None,
                    cognito_sub="sub-123",
                    user_id="user-001",
                    db=mock_db,
                )
            assert exc_info.value.status_code == 400

    @pytest.mark.asyncio
    async def test_empty_webhook_url_raises_422(self):
        """Issue #2674: missing webhook endpoint raises 422, not a blank manifest."""
        from fastapi import HTTPException

        from src.admin.connections.service import register_app_start

        mock_db = AsyncMock()

        with (
            patch(
                "src.admin.connections.service._check_existing_app_secret",
                return_value=None,
            ),
            patch.dict("os.environ", {"WEBHOOK_URL": ""}, clear=False),
            patch("boto3.client") as mock_boto,
        ):
            # Simulate SSM ParameterNotFound → webhook_url stays empty
            mock_ssm = MagicMock()
            mock_ssm.get_parameter.side_effect = Exception("ParameterNotFound")
            mock_boto.return_value = mock_ssm

            with pytest.raises(HTTPException) as exc_info:
                await register_app_start(
                    owner_type="org",
                    org="test-org",
                    cognito_sub="sub-123",
                    user_id="user-001",
                    db=mock_db,
                )

            assert exc_info.value.status_code == 422
            assert "webhook endpoint not configured" in exc_info.value.detail.lower()

    @pytest.mark.asyncio
    async def test_reads_correct_ssm_param_name(self):
        """Issue #2674: register_app_start reads /adp/<env>/webhook-ingress/endpoint."""
        from src.admin.connections.service import register_app_start

        mock_db = AsyncMock()

        with (
            patch(
                "src.admin.connections.service._check_existing_app_secret",
                return_value=None,
            ),
            patch(
                "src.admin.connections.service.store_nonce",
                new=AsyncMock(),
            ),
            patch.dict(
                "os.environ",
                {"WEBHOOK_URL": "", "ENVIRONMENT": "dev", "AWS_REGION": "us-east-1"},
                clear=False,
            ),
            patch("boto3.client") as mock_boto,
            patch(
                "src.admin.connections.service.get_settings",
                return_value=MagicMock(gateway_base_url="https://gw.test.com", github_app_slug=""),
            ),
        ):
            mock_ssm = MagicMock()
            mock_ssm.get_parameter.return_value = {"Parameter": {"Value": "https://webhook.test.com/github"}}
            mock_boto.return_value = mock_ssm

            result = await register_app_start(
                owner_type="org",
                org="my-org",
                cognito_sub="sub-123",
                user_id="user-001",
                db=mock_db,
            )

            # Verify correct SSM parameter name was used (first call is webhook)
            mock_ssm.get_parameter.assert_any_call(Name="/adp/dev/webhook-ingress/endpoint")
            assert result.status == "ready"
            assert result.manifest["hook_attributes"]["url"] == "https://webhook.test.com/github"


class TestRegisterAppCallbackService:
    """Tests for the register_app_callback service function."""

    @pytest.mark.asyncio
    async def test_private_key_not_in_redirect_url(self):
        """The redirect URL must never contain the private key."""
        from src.admin.connections.service import register_app_callback

        mock_db = AsyncMock()
        mock_nonce = MagicMock()
        mock_nonce.expires_at = datetime.now(UTC) + timedelta(minutes=10)
        mock_nonce.consumed_at = None

        # Mock the DB query to return a valid nonce
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_nonce
        mock_db.execute = AsyncMock(return_value=mock_result)
        mock_db.commit = AsyncMock()

        # Mock the consume step to succeed
        mock_consume_result = MagicMock()
        mock_consume_result.scalar_one_or_none.return_value = "consumed-jti"

        call_count = [0]

        async def mock_execute(stmt):
            call_count[0] += 1
            if call_count[0] == 1:
                return mock_result
            return mock_consume_result

        mock_db.execute = mock_execute

        github_response = {
            "id": 12345,
            "slug": "test-app",
            "pem": "-----BEGIN RSA PRIVATE KEY-----\nSECRET\n-----END RSA PRIVATE KEY-----",
            "client_id": "Iv1.abc123",
            "client_secret": "secret_value",
            "webhook_secret": "whsec_123",
        }

        mock_http_response = MagicMock()
        mock_http_response.status_code = 201
        mock_http_response.json.return_value = github_response

        with (
            patch("httpx.AsyncClient") as mock_client_cls,
            patch(
                "src.admin.connections.service._store_app_credentials",
                new=AsyncMock(),
            ),
        ):
            mock_client = AsyncMock()
            mock_client.post.return_value = mock_http_response
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            result = await register_app_callback(
                code="test-code",
                state="test-state",
                db=mock_db,
            )

        # The redirect URL should never contain the private key
        assert "PRIVATE KEY" not in result
        assert "SECRET" not in result
        assert "secret_value" not in result
        assert "github_app=registered" in result
        # Issue #2682: redirect must be a relative path (same-tab flow)
        assert result == "/settings/connections?github_app=registered"


class TestManifestStructure:
    """Tests for the manifest builder."""

    def test_manifest_has_required_fields(self):
        """Manifest contains all required GitHub App manifest fields."""
        from src.admin.connections.service import _build_app_manifest

        manifest = _build_app_manifest(
            webhook_url="https://webhook.example.com/github",
            callback_url="https://gw.example.com/api/admin/connections/github/app/register-callback",
            app_name="test-org-adp-agent-platform",
        )

        assert manifest["name"] == "test-org-adp-agent-platform"
        assert manifest["url"] == "https://github.com/apps/test-org-adp-agent-platform"
        assert manifest["hook_attributes"]["url"] == "https://webhook.example.com/github"
        assert manifest["hook_attributes"]["active"] is True
        assert manifest["redirect_url"] == "https://gw.example.com/api/admin/connections/github/app/register-callback"
        assert manifest["public"] is False

    def test_manifest_permissions(self):
        """Manifest contains the expected permissions for agent operations."""
        from src.admin.connections.service import _build_app_manifest

        manifest = _build_app_manifest(
            webhook_url="https://webhook.example.com",
            callback_url="https://callback.example.com",
        )

        perms = manifest["default_permissions"]
        assert perms["contents"] == "write"
        assert perms["issues"] == "write"
        assert perms["pull_requests"] == "write"
        assert perms["checks"] == "write"
        assert perms["metadata"] == "read"

    def test_manifest_events(self):
        """Manifest subscribes to the expected webhook events."""
        from src.admin.connections.service import _build_app_manifest

        manifest = _build_app_manifest(
            webhook_url="https://webhook.example.com",
            callback_url="https://callback.example.com",
        )

        events = manifest["default_events"]
        assert "issues" in events
        assert "issue_comment" in events
        assert "pull_request" in events
        assert "pull_request_review" in events
        assert "pull_request_review_comment" in events
        assert "label" in events

    def test_webhook_url_in_manifest(self):
        """hook_attributes.url resolves to the deployment's webhook API GW."""
        from src.admin.connections.service import _build_app_manifest

        webhook = "https://abc123.execute-api.us-east-1.amazonaws.com/prod/github"
        manifest = _build_app_manifest(
            webhook_url=webhook,
            callback_url="https://callback.example.com",
        )

        assert manifest["hook_attributes"]["url"] == webhook

    def test_manifest_includes_callback_urls_when_oauth_url_provided(self):
        """Issue #2607: manifest includes callback_urls for user-authorization OAuth."""
        from src.admin.connections.service import _build_app_manifest

        oauth_url = "https://api.example.com/auth/github/callback"
        manifest = _build_app_manifest(
            webhook_url="https://webhook.example.com",
            callback_url="https://callback.example.com",
            oauth_callback_url=oauth_url,
            app_name="my-org-adp-agent-platform",
        )

        assert manifest["callback_urls"] == [oauth_url]
        assert manifest["request_oauth_on_install"] is False
        # redirect_url (manifest conversion) must still be present
        assert manifest["redirect_url"] == "https://callback.example.com"
        # App name must be the one passed in
        assert manifest["name"] == "my-org-adp-agent-platform"

    def test_manifest_omits_callback_urls_when_no_oauth_url(self):
        """When oauth_callback_url is empty, manifest has no callback_urls field."""
        from src.admin.connections.service import _build_app_manifest

        manifest = _build_app_manifest(
            webhook_url="https://webhook.example.com",
            callback_url="https://callback.example.com",
            oauth_callback_url="",
        )

        assert "callback_urls" not in manifest
        assert "request_oauth_on_install" not in manifest

    def test_manifest_without_oauth_url_arg_omits_callback_urls(self):
        """Default (no oauth_callback_url kwarg) omits callback_urls."""
        from src.admin.connections.service import _build_app_manifest

        manifest = _build_app_manifest(
            webhook_url="https://webhook.example.com",
            callback_url="https://callback.example.com",
        )

        assert "callback_urls" not in manifest
        assert "request_oauth_on_install" not in manifest

    def test_manifest_includes_setup_url_when_provided(self):
        """Issue #2823: manifest includes setup_url + setup_on_update when set."""
        from src.admin.connections.service import _build_app_manifest

        setup_url = "https://gw.example.com/api/admin/connections/github/install-callback"
        manifest = _build_app_manifest(
            webhook_url="https://webhook.example.com",
            callback_url="https://callback.example.com",
            setup_url=setup_url,
        )

        assert manifest["setup_url"] == setup_url
        assert manifest["setup_url"].endswith("/api/admin/connections/github/install-callback")
        assert manifest["setup_on_update"] is True

    def test_manifest_omits_setup_url_when_not_provided(self):
        """Issue #2823: no setup_url/setup_on_update when the kwarg is empty."""
        from src.admin.connections.service import _build_app_manifest

        manifest = _build_app_manifest(
            webhook_url="https://webhook.example.com",
            callback_url="https://callback.example.com",
        )

        assert "setup_url" not in manifest
        assert "setup_on_update" not in manifest


# ---------------------------------------------------------------------------
# Issue #2607: Broker OAuth credential write-through tests
# ---------------------------------------------------------------------------


class TestBrokerOAuthWriteThrough:
    """Tests for the write-through of OAuth creds to broker secret path."""

    @pytest.mark.asyncio
    async def test_store_credentials_writes_broker_oauth_secret(self):
        """_store_app_credentials writes client_id/secret to broker's SM path."""
        from src.admin.connections.service import _store_app_credentials

        created_secrets = {}

        class FakeSM:
            def create_secret(self, **kwargs):
                name = kwargs["Name"]
                created_secrets[name] = kwargs["SecretString"]

        with (
            patch.dict("os.environ", {"ENVIRONMENT": "dev", "AWS_REGION": "us-east-1"}),
            patch("boto3.client", return_value=FakeSM()),
        ):
            await _store_app_credentials(
                app_id="123456",
                app_slug="adp-agent-platform",
                pem="-----BEGIN RSA PRIVATE KEY-----\ntest\n-----END RSA PRIVATE KEY-----",
                client_id="Iv1.abc123",
                client_secret="s3cr3t_value",
                webhook_secret="whsec_xyz",
            )

        # The broker's OAuth secret should be created
        oauth_path = "adp/dev/cognito/github-oauth-credentials"
        assert oauth_path in created_secrets
        import json

        oauth_data = json.loads(created_secrets[oauth_path])
        assert oauth_data["client_id"] == "Iv1.abc123"
        assert oauth_data["client_secret"] == "s3cr3t_value"

    @pytest.mark.asyncio
    async def test_store_credentials_skips_broker_secret_when_no_client_id(self):
        """If client_id is empty, don't write the broker OAuth secret."""
        from src.admin.connections.service import _store_app_credentials

        created_secrets = {}

        class FakeSM:
            def create_secret(self, **kwargs):
                name = kwargs["Name"]
                created_secrets[name] = kwargs["SecretString"]

        with (
            patch.dict("os.environ", {"ENVIRONMENT": "dev", "AWS_REGION": "us-east-1"}),
            patch("boto3.client", return_value=FakeSM()),
        ):
            await _store_app_credentials(
                app_id="123456",
                app_slug="adp-agent-platform",
                pem="-----BEGIN RSA PRIVATE KEY-----\ntest\n-----END RSA PRIVATE KEY-----",
                client_id="",
                client_secret="",
                webhook_secret="whsec_xyz",
            )

        oauth_path = "adp/dev/cognito/github-oauth-credentials"
        assert oauth_path not in created_secrets

    @pytest.mark.asyncio
    async def test_store_credentials_updates_existing_broker_secret(self):
        """If broker OAuth secret already exists, update it (don't fail)."""
        from botocore.exceptions import ClientError

        from src.admin.connections.service import _store_app_credentials

        put_calls = {}

        class FakeSM:
            def create_secret(self, **kwargs):
                name = kwargs["Name"]
                if "cognito/github-oauth-credentials" in name:
                    raise ClientError(
                        {"Error": {"Code": "ResourceExistsException", "Message": "exists"}},
                        "CreateSecret",
                    )
                # Other secrets succeed

            def put_secret_value(self, **kwargs):
                put_calls[kwargs["SecretId"]] = kwargs["SecretString"]

        with (
            patch.dict("os.environ", {"ENVIRONMENT": "dev", "AWS_REGION": "us-east-1"}),
            patch("boto3.client", return_value=FakeSM()),
        ):
            await _store_app_credentials(
                app_id="123456",
                app_slug="adp-agent-platform",
                pem="-----BEGIN RSA PRIVATE KEY-----\ntest\n-----END RSA PRIVATE KEY-----",
                client_id="Iv1.abc123",
                client_secret="new_secret",
                webhook_secret="whsec_xyz",
            )

        oauth_path = "adp/dev/cognito/github-oauth-credentials"
        assert oauth_path in put_calls
        import json

        oauth_data = json.loads(put_calls[oauth_path])
        assert oauth_data["client_id"] == "Iv1.abc123"
        assert oauth_data["client_secret"] == "new_secret"

    @pytest.mark.asyncio
    async def test_client_secret_never_logged_or_returned(self):
        """client_secret must not appear in log output or return values."""
        from src.admin.connections.service import register_app_callback

        mock_db = AsyncMock()
        mock_nonce = MagicMock()
        mock_nonce.expires_at = datetime.now(UTC) + timedelta(minutes=10)
        mock_nonce.consumed_at = None

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_nonce

        mock_consume_result = MagicMock()
        mock_consume_result.scalar_one_or_none.return_value = "consumed-jti"

        call_count = [0]

        async def mock_execute(stmt):
            call_count[0] += 1
            if call_count[0] == 1:
                return mock_result
            return mock_consume_result

        mock_db.execute = mock_execute
        mock_db.commit = AsyncMock()

        github_response = {
            "id": 99999,
            "slug": "my-app",
            "pem": "-----BEGIN RSA PRIVATE KEY-----\nKEY\n-----END RSA PRIVATE KEY-----",
            "client_id": "Iv1.test_id",
            "client_secret": "SUPER_SECRET_VALUE_MUST_NOT_LEAK",
            "webhook_secret": "whsec_test",
        }

        mock_http_response = MagicMock()
        mock_http_response.status_code = 201
        mock_http_response.json.return_value = github_response

        with (
            patch("httpx.AsyncClient") as mock_client_cls,
            patch(
                "src.admin.connections.service._store_app_credentials",
                new=AsyncMock(),
            ),
        ):
            mock_client = AsyncMock()
            mock_client.post.return_value = mock_http_response
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            result = await register_app_callback(
                code="test-code",
                state="test-state",
                db=mock_db,
            )

        assert "SUPER_SECRET_VALUE_MUST_NOT_LEAK" not in result
        assert "PRIVATE KEY" not in result


class TestLoginEnabledSignal:
    """Issue #2708: register flow reports whether GitHub login got wired."""

    @pytest.mark.asyncio
    async def test_store_credentials_returns_true_on_success(self):
        """_store_app_credentials returns True when the OAuth secret write lands."""
        from src.admin.connections.service import _store_app_credentials

        class FakeSM:
            def create_secret(self, **kwargs):
                pass

        with (
            patch.dict("os.environ", {"ENVIRONMENT": "dev", "AWS_REGION": "us-east-1"}),
            patch("boto3.client", return_value=FakeSM()),
        ):
            result = await _store_app_credentials(
                app_id="123456",
                app_slug="adp-agent-platform",
                pem="-----BEGIN RSA PRIVATE KEY-----\ntest\n-----END RSA PRIVATE KEY-----",
                client_id="Iv1.abc123",
                client_secret="s3cr3t",
                webhook_secret="whsec_xyz",
            )

        assert result is True

    @pytest.mark.asyncio
    async def test_store_credentials_returns_false_on_oauth_access_denied(self):
        """OAuth-secret AccessDenied → returns False; App-creds writes still succeed."""
        from botocore.exceptions import ClientError

        from src.admin.connections.service import _store_app_credentials

        class FakeSM:
            def create_secret(self, **kwargs):
                if "cognito/github-oauth-credentials" in kwargs["Name"]:
                    raise ClientError(
                        {"Error": {"Code": "AccessDeniedException", "Message": "denied"}},
                        "CreateSecret",
                    )
                # App-creds secrets succeed

            def put_secret_value(self, **kwargs):
                pass

        with (
            patch.dict("os.environ", {"ENVIRONMENT": "dev", "AWS_REGION": "us-east-1"}),
            patch("boto3.client", return_value=FakeSM()),
        ):
            result = await _store_app_credentials(
                app_id="123456",
                app_slug="adp-agent-platform",
                pem="-----BEGIN RSA PRIVATE KEY-----\ntest\n-----END RSA PRIVATE KEY-----",
                client_id="Iv1.abc123",
                client_secret="s3cr3t",
                webhook_secret="whsec_xyz",
            )

        assert result is False

    @pytest.mark.asyncio
    async def test_callback_redirect_carries_login_disabled_on_failure(self):
        """register_app_callback appends login_enabled=false when the write fails."""
        from src.admin.connections.service import register_app_callback

        mock_db = AsyncMock()
        mock_nonce = MagicMock()
        mock_nonce.expires_at = datetime.now(UTC) + timedelta(minutes=10)
        mock_nonce.consumed_at = None

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_nonce
        mock_consume_result = MagicMock()
        mock_consume_result.scalar_one_or_none.return_value = "consumed-jti"

        call_count = [0]

        async def mock_execute(stmt):
            call_count[0] += 1
            return mock_result if call_count[0] == 1 else mock_consume_result

        mock_db.execute = mock_execute
        mock_db.commit = AsyncMock()

        github_response = {
            "id": 12345,
            "slug": "test-app",
            "pem": "-----BEGIN RSA PRIVATE KEY-----\nK\n-----END RSA PRIVATE KEY-----",
            "client_id": "Iv1.abc",
            "client_secret": "s",
            "webhook_secret": "whsec",
        }
        mock_http_response = MagicMock()
        mock_http_response.status_code = 201
        mock_http_response.json.return_value = github_response

        with (
            patch("httpx.AsyncClient") as mock_client_cls,
            patch(
                "src.admin.connections.service._store_app_credentials",
                new=AsyncMock(return_value=False),
            ),
        ):
            mock_client = AsyncMock()
            mock_client.post.return_value = mock_http_response
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            result = await register_app_callback(code="c", state="s", db=mock_db)

        assert result == "/settings/connections?github_app=registered&login_enabled=false"

    @pytest.mark.asyncio
    async def test_callback_redirect_clean_on_success(self):
        """Success path redirect carries github_app=registered without the failure flag."""
        from src.admin.connections.service import register_app_callback

        mock_db = AsyncMock()
        mock_nonce = MagicMock()
        mock_nonce.expires_at = datetime.now(UTC) + timedelta(minutes=10)
        mock_nonce.consumed_at = None

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_nonce
        mock_consume_result = MagicMock()
        mock_consume_result.scalar_one_or_none.return_value = "consumed-jti"

        call_count = [0]

        async def mock_execute(stmt):
            call_count[0] += 1
            return mock_result if call_count[0] == 1 else mock_consume_result

        mock_db.execute = mock_execute
        mock_db.commit = AsyncMock()

        github_response = {
            "id": 12345,
            "slug": "test-app",
            "pem": "-----BEGIN RSA PRIVATE KEY-----\nK\n-----END RSA PRIVATE KEY-----",
            "client_id": "Iv1.abc",
            "client_secret": "s",
            "webhook_secret": "whsec",
        }
        mock_http_response = MagicMock()
        mock_http_response.status_code = 201
        mock_http_response.json.return_value = github_response

        with (
            patch("httpx.AsyncClient") as mock_client_cls,
            patch(
                "src.admin.connections.service._store_app_credentials",
                new=AsyncMock(return_value=True),
            ),
        ):
            mock_client = AsyncMock()
            mock_client.post.return_value = mock_http_response
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            result = await register_app_callback(code="c", state="s", db=mock_db)

        assert result == "/settings/connections?github_app=registered"
        assert "login_enabled" not in result

    @pytest.mark.asyncio
    async def test_callback_invalidates_login_enabled_cache(self):
        """Issue #2746: a successful register invalidates the public login_enabled cache.

        A stale cached ``False`` (login not wired) must not keep the login page's
        GitHub button disabled after the App is registered.
        """
        from src.admin.connections import service
        from src.admin.connections.service import register_app_callback

        # Seed a non-expired cached False (login previously not wired).
        service._LOGIN_ENABLED_CACHE = (service.time.monotonic() + 3600, False)

        mock_db = AsyncMock()
        mock_nonce = MagicMock()
        mock_nonce.expires_at = datetime.now(UTC) + timedelta(minutes=10)
        mock_nonce.consumed_at = None

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_nonce
        mock_consume_result = MagicMock()
        mock_consume_result.scalar_one_or_none.return_value = "consumed-jti"

        call_count = [0]

        async def mock_execute(stmt):
            call_count[0] += 1
            return mock_result if call_count[0] == 1 else mock_consume_result

        mock_db.execute = mock_execute
        mock_db.commit = AsyncMock()

        github_response = {
            "id": 12345,
            "slug": "test-app",
            "pem": "-----BEGIN RSA PRIVATE KEY-----\nK\n-----END RSA PRIVATE KEY-----",
            "client_id": "Iv1.abc",
            "client_secret": "s",
            "webhook_secret": "whsec",
        }
        mock_http_response = MagicMock()
        mock_http_response.status_code = 201
        mock_http_response.json.return_value = github_response

        with (
            patch("httpx.AsyncClient") as mock_client_cls,
            patch(
                "src.admin.connections.service._store_app_credentials",
                new=AsyncMock(return_value=True),
            ),
        ):
            mock_client = AsyncMock()
            mock_client.post.return_value = mock_http_response
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            await register_app_callback(code="c", state="s", db=mock_db)

        assert service._LOGIN_ENABLED_CACHE is None


# ---------------------------------------------------------------------------
# Issue #2677: App name uniqueness tests
# ---------------------------------------------------------------------------


class TestDeriveAppName:
    """Tests for _derive_app_name — globally unique name derivation."""

    def test_explicit_app_name_used_as_is(self):
        """Explicit app_name takes priority over owner-prefixed default."""
        from src.admin.connections.service import _derive_app_name

        result = _derive_app_name(owner="my-org", app_name="my-custom-app")
        assert result == "my-custom-app"

    def test_org_prefixed_when_no_explicit_name(self):
        """Without app_name, uses '<owner>-adp-agent-platform'."""
        from src.admin.connections.service import _derive_app_name

        result = _derive_app_name(owner="aws-sophos-test", app_name=None)
        assert result == "aws-sophos-test-adp-agent-platform"

    def test_falls_back_to_base_when_no_owner(self):
        """Without owner or app_name, falls back to base name."""
        from src.admin.connections.service import _derive_app_name

        result = _derive_app_name(owner=None, app_name=None)
        assert result == "adp-agent-platform"

    def test_explicit_name_wins_over_no_owner(self):
        """Explicit app_name used even when owner is None."""
        from src.admin.connections.service import _derive_app_name

        result = _derive_app_name(owner=None, app_name="user-custom-app")
        assert result == "user-custom-app"

    def test_empty_string_app_name_treated_as_none(self):
        """Empty string app_name falls through to owner-prefixed."""
        from src.admin.connections.service import _derive_app_name

        result = _derive_app_name(owner="acme-corp", app_name="")
        assert result == "acme-corp-adp-agent-platform"


class TestManifestAppName:
    """Tests for _build_app_manifest app_name handling (#2677)."""

    def test_default_app_name_is_base(self):
        """Without app_name kwarg, manifest uses base name (backward compat)."""
        from src.admin.connections.service import _build_app_manifest

        manifest = _build_app_manifest(
            webhook_url="https://webhook.example.com",
            callback_url="https://callback.example.com",
        )
        assert manifest["name"] == "adp-agent-platform"
        assert manifest["url"] == "https://github.com/apps/adp-agent-platform"

    def test_custom_app_name_in_manifest(self):
        """Custom app_name flows into manifest name and url fields."""
        from src.admin.connections.service import _build_app_manifest

        manifest = _build_app_manifest(
            webhook_url="https://webhook.example.com",
            callback_url="https://callback.example.com",
            app_name="my-org-adp-agent-platform",
        )
        assert manifest["name"] == "my-org-adp-agent-platform"
        assert manifest["url"] == "https://github.com/apps/my-org-adp-agent-platform"


# ---------------------------------------------------------------------------
# Issue #2682: Callback redirect uses relative path (session handoff fix)
# ---------------------------------------------------------------------------


class TestCallbackRedirectRelativePath:
    """Tests that register_app_callback returns a relative URL (Issue #2682).

    The callback must use a relative path so the SPA session (same-tab flow)
    is preserved when the browser follows the 302 redirect. Absolute URLs
    were breaking because the new tab had no auth session.
    """

    @pytest.mark.asyncio
    async def test_callback_returns_relative_redirect_path(self):
        """register_app_callback redirect URL is relative, not absolute."""
        from src.admin.connections.service import register_app_callback

        mock_db = AsyncMock()
        mock_nonce = MagicMock()
        mock_nonce.expires_at = datetime.now(UTC) + timedelta(minutes=10)
        mock_nonce.consumed_at = None

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_nonce

        mock_consume_result = MagicMock()
        mock_consume_result.scalar_one_or_none.return_value = "consumed-jti"

        call_count = [0]

        async def mock_execute(stmt):
            call_count[0] += 1
            if call_count[0] == 1:
                return mock_result
            return mock_consume_result

        mock_db.execute = mock_execute
        mock_db.commit = AsyncMock()

        github_response = {
            "id": 77777,
            "slug": "my-platform-app",
            "pem": "-----BEGIN RSA PRIVATE KEY-----\nKEY\n-----END RSA PRIVATE KEY-----",
            "client_id": "Iv1.client123",
            "client_secret": "cs_secret",
            "webhook_secret": "whsec_abc",
        }

        mock_http_response = MagicMock()
        mock_http_response.status_code = 201
        mock_http_response.json.return_value = github_response

        with (
            patch("httpx.AsyncClient") as mock_client_cls,
            patch(
                "src.admin.connections.service._store_app_credentials",
                new=AsyncMock(),
            ),
        ):
            mock_client = AsyncMock()
            mock_client.post.return_value = mock_http_response
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            result = await register_app_callback(
                code="real-code",
                state="valid-state",
                db=mock_db,
            )

        # Must be a relative path — no scheme, no host
        assert result.startswith("/")
        assert not result.startswith("http")
        assert result == "/settings/connections?github_app=registered"


class TestCallbackEntryLogging:
    """Tests that the callback route logs at entry before validation (Issue #2682)."""

    def test_callback_logs_entry_before_state_check(self, app, mock_db, caplog):
        """Callback route logs entry even when state is missing."""
        import logging

        user = _make_user(is_admin=True)
        client = _make_client(app, user=user, mock_db=mock_db)

        with caplog.at_level(logging.INFO, logger="src.admin.connections.routes"):
            resp = client.get(
                "/admin/connections/github/app/register-callback?code=abc123",
                follow_redirects=False,
            )

        # The response should be a redirect to missing_state error
        assert resp.status_code == 302
        assert "error=missing_state" in resp.headers["location"]

        # But the entry log should have been emitted BEFORE the error redirect
        assert any("register-app-callback: entry" in record.message for record in caplog.records)

    def test_callback_logs_entry_with_both_params(self, app, mock_db, caplog):
        """Callback route logs entry with both code and state present."""
        import logging

        user = _make_user(is_admin=True)
        client = _make_client(app, user=user, mock_db=mock_db)

        with (
            caplog.at_level(logging.INFO, logger="src.admin.connections.routes"),
            patch(
                "src.admin.connections.routes.register_app_callback",
                new=AsyncMock(return_value="/settings/connections?github_app=registered"),
            ),
        ):
            resp = client.get(
                "/admin/connections/github/app/register-callback?code=abc123&state=state-xyz",
                follow_redirects=False,
            )

        assert resp.status_code == 302
        assert any("register-app-callback: entry code_present=True state_present=True" in record.message for record in caplog.records)
