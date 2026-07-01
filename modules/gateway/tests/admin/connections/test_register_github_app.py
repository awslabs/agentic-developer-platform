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
        """register_app_start generates manifest if no existing App."""
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
        assert result.manifest["name"] == "adp-agent-platform"
        assert result.manifest["hook_attributes"]["url"] == "https://webhook.test.com/github"
        assert result.manifest["redirect_url"] == "https://gw.test.com/api/admin/connections/github/app/register-callback"
        assert result.manifest["public"] is False
        assert result.manifest["default_permissions"]["contents"] == "write"
        assert result.manifest["default_permissions"]["metadata"] == "read"
        assert "issues" in result.manifest["default_events"]
        assert result.post_url == "https://github.com/organizations/my-org/settings/apps/new"
        assert result.state is not None

    @pytest.mark.asyncio
    async def test_user_owner_type_url(self):
        """owner_type='user' produces the personal settings URL."""
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
            patch(
                "src.admin.connections.service.get_settings",
                return_value=MagicMock(gateway_base_url="https://gw.test.com"),
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


class TestManifestStructure:
    """Tests for the manifest builder."""

    def test_manifest_has_required_fields(self):
        """Manifest contains all required GitHub App manifest fields."""
        from src.admin.connections.service import _build_app_manifest

        manifest = _build_app_manifest(
            webhook_url="https://webhook.example.com/github",
            callback_url="https://gw.example.com/api/admin/connections/github/app/register-callback",
        )

        assert manifest["name"] == "adp-agent-platform"
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
