"""Unit tests for manual GitHub App registration endpoint.

Issue #3360: POST /admin/connections/github/app/register-manual

Tests cover:
- Platform-admin access control (403 for non-platform-admins)
- Mismatched app_id/key returns 400 (JWT validation before store)
- Invalid PEM returns 400
- Successful registration stores via _store_app_credentials
- PEM normalization (escaped \\n and real newlines)
- Missing OAuth credentials produce a warning
- Missing permissions/events produce warnings but import succeeds
- Webhook URL mismatch produces a warning
- Existing test_register_github_app.py suite unaffected (callback path untouched)
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.admin.connections.routes import router
from src.auth.dependencies import get_current_user
from src.shared.database import get_db
from src.shared.schemas.auth import TokenContext

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_TEST_PEM = "-----BEGIN RSA PRIVATE KEY-----\nMIIEpAIBAAKCAQEA0Z3VS5JJcds3xfn/ygWelFfKIRECAJSBBH2unQ3pOFRMVM3W\n-----END RSA PRIVATE KEY-----\n"

_TEST_PEM_ESCAPED = (
    "-----BEGIN RSA PRIVATE KEY-----\\nMIIEpAIBAAKCAQEA0Z3VS5JJcds3xfn/ygWelFfKIRECAJSBBH2unQ3pOFRMVM3W\\n-----END RSA PRIVATE KEY-----\\n"
)


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


def _valid_body(**overrides) -> dict:
    """Build a valid request body with optional overrides."""
    body = {
        "app_id": "123456",
        "private_key": _TEST_PEM,
        "webhook_secret": "whsec_test",
        "client_id": "Iv1.abc123",
        "client_secret": "s3cr3t",
    }
    body.update(overrides)
    return body


# ---------------------------------------------------------------------------
# Route-level tests
# ---------------------------------------------------------------------------


class TestRegisterManualRoute:
    """Tests for the register-manual endpoint routing and access control."""

    def test_non_admin_returns_403(self, app, mock_db):
        """Non-platform-admin users must be rejected with 403."""
        user = _make_user(is_admin=False)
        client = _make_client(app, user=user, mock_db=mock_db)

        resp = client.post(
            "/admin/connections/github/app/register-manual",
            json=_valid_body(),
        )
        assert resp.status_code == 403
        assert "platform administrator" in resp.json()["detail"].lower()

    def test_admin_with_valid_creds_succeeds(self, app, mock_db):
        """Platform admin with valid credentials gets a success response."""
        user = _make_user(is_admin=True)
        client = _make_client(app, user=user, mock_db=mock_db)

        mock_result = {
            "registered": True,
            "app_id": "123456",
            "app_slug": "my-app",
            "app_name": "My App",
            "login_enabled": True,
            "warnings": [],
        }

        with patch(
            "src.admin.connections.routes.register_app_manual",
            new=AsyncMock(return_value=mock_result),
        ):
            resp = client.post(
                "/admin/connections/github/app/register-manual",
                json=_valid_body(),
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["registered"] is True
        assert body["app_id"] == "123456"
        assert body["app_slug"] == "my-app"
        assert body["warnings"] == []

    def test_mismatched_credentials_returns_400(self, app, mock_db):
        """Mismatched app_id/key returns 400 from the service."""
        from fastapi import HTTPException

        user = _make_user(is_admin=True)
        client = _make_client(app, user=user, mock_db=mock_db)

        with patch(
            "src.admin.connections.routes.register_app_manual",
            new=AsyncMock(
                side_effect=HTTPException(
                    status_code=400,
                    detail="App ID and private key don't match. GitHub returned 401 Unauthorized.",
                )
            ),
        ):
            resp = client.post(
                "/admin/connections/github/app/register-manual",
                json=_valid_body(),
            )

        assert resp.status_code == 400
        assert "don't match" in resp.json()["detail"]

    def test_unexpected_error_returns_500(self, app, mock_db):
        """Unexpected exceptions return 500."""
        user = _make_user(is_admin=True)
        client = _make_client(app, user=user, mock_db=mock_db)

        with patch(
            "src.admin.connections.routes.register_app_manual",
            new=AsyncMock(side_effect=RuntimeError("boom")),
        ):
            resp = client.post(
                "/admin/connections/github/app/register-manual",
                json=_valid_body(),
            )

        assert resp.status_code == 500


# ---------------------------------------------------------------------------
# Service-level tests
# ---------------------------------------------------------------------------


class TestRegisterAppManualService:
    """Tests for the register_app_manual service function."""

    @pytest.mark.asyncio
    async def test_mismatched_app_id_key_returns_400(self):
        """When GitHub returns 401, the service raises HTTPException 400."""
        from fastapi import HTTPException

        from src.admin.connections.service import register_app_manual

        mock_response = MagicMock()
        mock_response.status_code = 401

        with (
            patch(
                "src.admin.connections.github_client._mint_app_jwt",
                return_value="fake-jwt",
            ),
            patch("httpx.AsyncClient") as mock_client_cls,
        ):
            mock_client = AsyncMock()
            mock_client.get.return_value = mock_response
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            with pytest.raises(HTTPException) as exc_info:
                await register_app_manual(
                    app_id="999999",
                    private_key=_TEST_PEM,
                )

            assert exc_info.value.status_code == 400
            assert "don't match" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_invalid_pem_returns_400(self):
        """A PEM that can't be used to encode a JWT raises 400."""
        from fastapi import HTTPException

        from src.admin.connections.service import register_app_manual

        with pytest.raises(HTTPException) as exc_info:
            await register_app_manual(
                app_id="123456",
                private_key="not-a-valid-pem-at-all",
            )

        assert exc_info.value.status_code == 400
        assert "invalid private key" in exc_info.value.detail.lower()

    @pytest.mark.asyncio
    async def test_valid_pair_stores_credentials(self):
        """Valid app_id + key stores via _store_app_credentials with same shape as callback."""
        from src.admin.connections.service import register_app_manual

        # Real GET /app response shape (no hook_config field)
        github_app_response = {
            "id": 123456,
            "slug": "test-app-slug",
            "name": "Test App",
            "permissions": {
                "contents": "write",
                "issues": "write",
                "pull_requests": "write",
                "checks": "write",
                "metadata": "read",
            },
            "events": [
                "issues",
                "issue_comment",
                "pull_request",
                "pull_request_review",
                "pull_request_review_comment",
                "label",
            ],
        }

        # GET /app/hook/config response
        hook_config_response = {
            "url": "https://webhook.test.com/github",
            "content_type": "json",
            "insecure_ssl": "0",
        }

        mock_app_resp = MagicMock()
        mock_app_resp.status_code = 200
        mock_app_resp.json.return_value = github_app_response

        mock_hook_resp = MagicMock()
        mock_hook_resp.status_code = 200
        mock_hook_resp.json.return_value = hook_config_response

        store_calls = {}

        async def mock_store(**kwargs):
            store_calls.update(kwargs)
            return True

        def _route_get(url, **kwargs):
            if "/app/hook/config" in url:
                return mock_hook_resp
            return mock_app_resp

        with (
            patch(
                "src.admin.connections.github_client._mint_app_jwt",
                return_value="fake-jwt",
            ),
            patch("httpx.AsyncClient") as mock_client_cls,
            patch(
                "src.admin.connections.service._store_app_credentials",
                new=mock_store,
            ),
            patch(
                "src.admin.connections.service.get_github_app_provider",
                return_value=MagicMock(invalidate=MagicMock()),
            ),
            patch.dict("os.environ", {"WEBHOOK_URL": "https://webhook.test.com/github"}),
        ):
            mock_client = AsyncMock()
            mock_client.get.side_effect = _route_get
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            result = await register_app_manual(
                app_id="123456",
                private_key=_TEST_PEM,
                webhook_secret="whsec_test",
                client_id="Iv1.abc",
                client_secret="s3cr3t",
            )

        assert result["registered"] is True
        assert result["app_id"] == "123456"
        assert result["app_slug"] == "test-app-slug"
        assert result["app_name"] == "Test App"
        assert result["login_enabled"] is True
        assert result["warnings"] == []

        # Verify _store_app_credentials was called with the correct args
        assert store_calls["app_id"] == "123456"
        assert store_calls["app_slug"] == "test-app-slug"
        assert store_calls["pem"] == _TEST_PEM
        assert store_calls["client_id"] == "Iv1.abc"
        assert store_calls["client_secret"] == "s3cr3t"
        assert store_calls["webhook_secret"] == "whsec_test"

    @pytest.mark.asyncio
    async def test_pem_with_escaped_newlines_accepted(self):
        """PEM with escaped \\n (from .env/JSON) is normalized and accepted."""
        from src.admin.connections.service import register_app_manual

        github_app_response = {
            "id": 123456,
            "slug": "test-app",
            "name": "Test App",
            "permissions": {
                "contents": "write",
                "issues": "write",
                "pull_requests": "write",
                "checks": "write",
                "metadata": "read",
            },
            "events": [
                "issues",
                "issue_comment",
                "pull_request",
                "pull_request_review",
                "pull_request_review_comment",
                "label",
            ],
        }

        mock_app_resp = MagicMock()
        mock_app_resp.status_code = 200
        mock_app_resp.json.return_value = github_app_response

        # /app/hook/config returns empty URL (no webhook configured)
        mock_hook_resp = MagicMock()
        mock_hook_resp.status_code = 200
        mock_hook_resp.json.return_value = {"url": "", "content_type": "json"}

        stored_pem = {}

        async def mock_store(**kwargs):
            stored_pem["pem"] = kwargs["pem"]
            return True

        def _route_get(url, **kwargs):
            if "/app/hook/config" in url:
                return mock_hook_resp
            return mock_app_resp

        with (
            patch(
                "src.admin.connections.github_client._mint_app_jwt",
                return_value="fake-jwt",
            ),
            patch("httpx.AsyncClient") as mock_client_cls,
            patch(
                "src.admin.connections.service._store_app_credentials",
                new=mock_store,
            ),
            patch(
                "src.admin.connections.service.get_github_app_provider",
                return_value=MagicMock(invalidate=MagicMock()),
            ),
            patch.dict("os.environ", {"WEBHOOK_URL": ""}),
            patch("boto3.client") as mock_boto,
        ):
            mock_ssm = MagicMock()
            mock_ssm.get_parameter.side_effect = Exception("not found")
            mock_boto.return_value = mock_ssm

            mock_client = AsyncMock()
            mock_client.get.side_effect = _route_get
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            result = await register_app_manual(
                app_id="123456",
                private_key=_TEST_PEM_ESCAPED,
            )

        assert result["registered"] is True
        # The stored PEM should have real newlines (normalized)
        assert "\\n" not in stored_pem["pem"]
        assert "\n" in stored_pem["pem"]
        assert stored_pem["pem"].startswith("-----BEGIN RSA PRIVATE KEY-----\n")

    @pytest.mark.asyncio
    async def test_pem_with_real_newlines_accepted(self):
        """PEM with real newlines (copy-paste) is accepted as-is."""
        from src.admin.connections.service import register_app_manual

        github_app_response = {
            "id": 123456,
            "slug": "test-app",
            "name": "Test",
            "permissions": {
                "contents": "write",
                "issues": "write",
                "pull_requests": "write",
                "checks": "write",
                "metadata": "read",
            },
            "events": [
                "issues",
                "issue_comment",
                "pull_request",
                "pull_request_review",
                "pull_request_review_comment",
                "label",
            ],
        }

        mock_app_resp = MagicMock()
        mock_app_resp.status_code = 200
        mock_app_resp.json.return_value = github_app_response

        mock_hook_resp = MagicMock()
        mock_hook_resp.status_code = 200
        mock_hook_resp.json.return_value = {"url": "", "content_type": "json"}

        stored_pem = {}

        async def mock_store(**kwargs):
            stored_pem["pem"] = kwargs["pem"]
            return True

        def _route_get(url, **kwargs):
            if "/app/hook/config" in url:
                return mock_hook_resp
            return mock_app_resp

        with (
            patch(
                "src.admin.connections.github_client._mint_app_jwt",
                return_value="fake-jwt",
            ),
            patch("httpx.AsyncClient") as mock_client_cls,
            patch(
                "src.admin.connections.service._store_app_credentials",
                new=mock_store,
            ),
            patch(
                "src.admin.connections.service.get_github_app_provider",
                return_value=MagicMock(invalidate=MagicMock()),
            ),
            patch.dict("os.environ", {"WEBHOOK_URL": ""}),
            patch("boto3.client") as mock_boto,
        ):
            mock_ssm = MagicMock()
            mock_ssm.get_parameter.side_effect = Exception("not found")
            mock_boto.return_value = mock_ssm

            mock_client = AsyncMock()
            mock_client.get.side_effect = _route_get
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            result = await register_app_manual(
                app_id="123456",
                private_key=_TEST_PEM,
            )

        assert result["registered"] is True
        assert stored_pem["pem"] == _TEST_PEM

    @pytest.mark.asyncio
    async def test_missing_oauth_credentials_produces_warning(self):
        """Omitting client_id/client_secret produces a non-blocking warning and login_enabled=False."""
        from src.admin.connections.service import register_app_manual

        github_app_response = {
            "id": 123456,
            "slug": "test-app",
            "name": "Test",
            "permissions": {
                "contents": "write",
                "issues": "write",
                "pull_requests": "write",
                "checks": "write",
                "metadata": "read",
            },
            "events": [
                "issues",
                "issue_comment",
                "pull_request",
                "pull_request_review",
                "pull_request_review_comment",
                "label",
            ],
        }

        hook_config_response = {
            "url": "https://webhook.test.com/github",
            "content_type": "json",
        }

        mock_app_resp = MagicMock()
        mock_app_resp.status_code = 200
        mock_app_resp.json.return_value = github_app_response

        mock_hook_resp = MagicMock()
        mock_hook_resp.status_code = 200
        mock_hook_resp.json.return_value = hook_config_response

        def _route_get(url, **kwargs):
            if "/app/hook/config" in url:
                return mock_hook_resp
            return mock_app_resp

        with (
            patch(
                "src.admin.connections.github_client._mint_app_jwt",
                return_value="fake-jwt",
            ),
            patch("httpx.AsyncClient") as mock_client_cls,
            patch(
                "src.admin.connections.service._store_app_credentials",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "src.admin.connections.service.get_github_app_provider",
                return_value=MagicMock(invalidate=MagicMock()),
            ),
            patch.dict("os.environ", {"WEBHOOK_URL": "https://webhook.test.com/github"}),
        ):
            mock_client = AsyncMock()
            mock_client.get.side_effect = _route_get
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            result = await register_app_manual(
                app_id="123456",
                private_key=_TEST_PEM,
                # No client_id / client_secret
            )

        assert result["registered"] is True
        # login_enabled must be False when OAuth creds are omitted
        assert result["login_enabled"] is False
        assert any("OAuth" in w or "client_id" in w for w in result["warnings"])

    @pytest.mark.asyncio
    async def test_missing_permissions_produces_warning(self):
        """Missing App permissions produce warnings but don't block import."""
        from src.admin.connections.service import register_app_manual

        github_app_response = {
            "id": 123456,
            "slug": "test-app",
            "name": "Test",
            "permissions": {
                "metadata": "read",
                # Missing contents, issues, pull_requests, checks
            },
            "events": [
                "issues",
                "issue_comment",
                "pull_request",
                "pull_request_review",
                "pull_request_review_comment",
                "label",
            ],
        }

        hook_config_response = {
            "url": "https://webhook.test.com/github",
            "content_type": "json",
        }

        mock_app_resp = MagicMock()
        mock_app_resp.status_code = 200
        mock_app_resp.json.return_value = github_app_response

        mock_hook_resp = MagicMock()
        mock_hook_resp.status_code = 200
        mock_hook_resp.json.return_value = hook_config_response

        def _route_get(url, **kwargs):
            if "/app/hook/config" in url:
                return mock_hook_resp
            return mock_app_resp

        with (
            patch(
                "src.admin.connections.github_client._mint_app_jwt",
                return_value="fake-jwt",
            ),
            patch("httpx.AsyncClient") as mock_client_cls,
            patch(
                "src.admin.connections.service._store_app_credentials",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "src.admin.connections.service.get_github_app_provider",
                return_value=MagicMock(invalidate=MagicMock()),
            ),
            patch.dict("os.environ", {"WEBHOOK_URL": "https://webhook.test.com/github"}),
        ):
            mock_client = AsyncMock()
            mock_client.get.side_effect = _route_get
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            result = await register_app_manual(
                app_id="123456",
                private_key=_TEST_PEM,
                client_id="Iv1.abc",
                client_secret="s3cr3t",
            )

        # Import succeeds
        assert result["registered"] is True
        # But there's a permissions warning
        perm_warnings = [w for w in result["warnings"] if "permissions" in w.lower()]
        assert len(perm_warnings) == 1
        assert "contents" in perm_warnings[0]

    @pytest.mark.asyncio
    async def test_missing_events_produces_warning(self):
        """Missing event subscriptions produce warnings but don't block import."""
        from src.admin.connections.service import register_app_manual

        github_app_response = {
            "id": 123456,
            "slug": "test-app",
            "name": "Test",
            "permissions": {
                "contents": "write",
                "issues": "write",
                "pull_requests": "write",
                "checks": "write",
                "metadata": "read",
            },
            "events": [
                "issues",
                # Missing: issue_comment, pull_request, pull_request_review,
                #          pull_request_review_comment, label
            ],
        }

        hook_config_response = {
            "url": "https://webhook.test.com/github",
            "content_type": "json",
        }

        mock_app_resp = MagicMock()
        mock_app_resp.status_code = 200
        mock_app_resp.json.return_value = github_app_response

        mock_hook_resp = MagicMock()
        mock_hook_resp.status_code = 200
        mock_hook_resp.json.return_value = hook_config_response

        def _route_get(url, **kwargs):
            if "/app/hook/config" in url:
                return mock_hook_resp
            return mock_app_resp

        with (
            patch(
                "src.admin.connections.github_client._mint_app_jwt",
                return_value="fake-jwt",
            ),
            patch("httpx.AsyncClient") as mock_client_cls,
            patch(
                "src.admin.connections.service._store_app_credentials",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "src.admin.connections.service.get_github_app_provider",
                return_value=MagicMock(invalidate=MagicMock()),
            ),
            patch.dict("os.environ", {"WEBHOOK_URL": "https://webhook.test.com/github"}),
        ):
            mock_client = AsyncMock()
            mock_client.get.side_effect = _route_get
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            result = await register_app_manual(
                app_id="123456",
                private_key=_TEST_PEM,
                client_id="Iv1.abc",
                client_secret="s3cr3t",
            )

        assert result["registered"] is True
        event_warnings = [w for w in result["warnings"] if "event" in w.lower()]
        assert len(event_warnings) == 1
        assert "issue_comment" in event_warnings[0]
        assert "pull_request" in event_warnings[0]

    @pytest.mark.asyncio
    async def test_webhook_url_mismatch_produces_warning(self):
        """Webhook URL mismatch between App and deployment produces a warning."""
        from src.admin.connections.service import register_app_manual

        github_app_response = {
            "id": 123456,
            "slug": "test-app",
            "name": "Test",
            "permissions": {
                "contents": "write",
                "issues": "write",
                "pull_requests": "write",
                "checks": "write",
                "metadata": "read",
            },
            "events": [
                "issues",
                "issue_comment",
                "pull_request",
                "pull_request_review",
                "pull_request_review_comment",
                "label",
            ],
        }

        # GET /app/hook/config returns the OLD webhook URL
        hook_config_response = {
            "url": "https://old-webhook.example.com/github",
            "content_type": "json",
        }

        mock_app_resp = MagicMock()
        mock_app_resp.status_code = 200
        mock_app_resp.json.return_value = github_app_response

        mock_hook_resp = MagicMock()
        mock_hook_resp.status_code = 200
        mock_hook_resp.json.return_value = hook_config_response

        def _route_get(url, **kwargs):
            if "/app/hook/config" in url:
                return mock_hook_resp
            return mock_app_resp

        with (
            patch(
                "src.admin.connections.github_client._mint_app_jwt",
                return_value="fake-jwt",
            ),
            patch("httpx.AsyncClient") as mock_client_cls,
            patch(
                "src.admin.connections.service._store_app_credentials",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "src.admin.connections.service.get_github_app_provider",
                return_value=MagicMock(invalidate=MagicMock()),
            ),
            patch.dict("os.environ", {"WEBHOOK_URL": "https://new-webhook.example.com/github"}),
        ):
            mock_client = AsyncMock()
            mock_client.get.side_effect = _route_get
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            result = await register_app_manual(
                app_id="123456",
                private_key=_TEST_PEM,
                client_id="Iv1.abc",
                client_secret="s3cr3t",
            )

        assert result["registered"] is True
        webhook_warnings = [w for w in result["warnings"] if "webhook" in w.lower()]
        assert len(webhook_warnings) == 1
        # Both URLs should be shown in the warning
        assert "old-webhook.example.com" in webhook_warnings[0]
        assert "new-webhook.example.com" in webhook_warnings[0]

    @pytest.mark.asyncio
    async def test_login_enabled_true_when_oauth_provided(self):
        """login_enabled is True when both client_id and client_secret are provided and store succeeds."""
        from src.admin.connections.service import register_app_manual

        github_app_response = {
            "id": 123456,
            "slug": "test-app",
            "name": "Test",
            "permissions": {
                "contents": "write",
                "issues": "write",
                "pull_requests": "write",
                "checks": "write",
                "metadata": "read",
            },
            "events": [
                "issues",
                "issue_comment",
                "pull_request",
                "pull_request_review",
                "pull_request_review_comment",
                "label",
            ],
        }

        hook_config_response = {"url": "https://webhook.test.com/github", "content_type": "json"}

        mock_app_resp = MagicMock()
        mock_app_resp.status_code = 200
        mock_app_resp.json.return_value = github_app_response

        mock_hook_resp = MagicMock()
        mock_hook_resp.status_code = 200
        mock_hook_resp.json.return_value = hook_config_response

        def _route_get(url, **kwargs):
            if "/app/hook/config" in url:
                return mock_hook_resp
            return mock_app_resp

        with (
            patch("src.admin.connections.github_client._mint_app_jwt", return_value="fake-jwt"),
            patch("httpx.AsyncClient") as mock_client_cls,
            patch("src.admin.connections.service._store_app_credentials", new=AsyncMock(return_value=True)),
            patch("src.admin.connections.service.get_github_app_provider", return_value=MagicMock(invalidate=MagicMock())),
            patch.dict("os.environ", {"WEBHOOK_URL": "https://webhook.test.com/github"}),
        ):
            mock_client = AsyncMock()
            mock_client.get.side_effect = _route_get
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            result = await register_app_manual(
                app_id="123456",
                private_key=_TEST_PEM,
                client_id="Iv1.abc",
                client_secret="s3cr3t",
            )

        assert result["login_enabled"] is True

    @pytest.mark.asyncio
    async def test_login_enabled_false_when_store_fails(self):
        """login_enabled is False when OAuth creds provided but store fails."""
        from src.admin.connections.service import register_app_manual

        github_app_response = {
            "id": 123456,
            "slug": "test-app",
            "name": "Test",
            "permissions": {
                "contents": "write",
                "issues": "write",
                "pull_requests": "write",
                "checks": "write",
                "metadata": "read",
            },
            "events": [
                "issues",
                "issue_comment",
                "pull_request",
                "pull_request_review",
                "pull_request_review_comment",
                "label",
            ],
        }

        hook_config_response = {"url": "https://webhook.test.com/github", "content_type": "json"}

        mock_app_resp = MagicMock()
        mock_app_resp.status_code = 200
        mock_app_resp.json.return_value = github_app_response

        mock_hook_resp = MagicMock()
        mock_hook_resp.status_code = 200
        mock_hook_resp.json.return_value = hook_config_response

        def _route_get(url, **kwargs):
            if "/app/hook/config" in url:
                return mock_hook_resp
            return mock_app_resp

        with (
            patch("src.admin.connections.github_client._mint_app_jwt", return_value="fake-jwt"),
            patch("httpx.AsyncClient") as mock_client_cls,
            # Store returns False (OAuth write-through failed)
            patch("src.admin.connections.service._store_app_credentials", new=AsyncMock(return_value=False)),
            patch("src.admin.connections.service.get_github_app_provider", return_value=MagicMock(invalidate=MagicMock())),
            patch.dict("os.environ", {"WEBHOOK_URL": "https://webhook.test.com/github"}),
        ):
            mock_client = AsyncMock()
            mock_client.get.side_effect = _route_get
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            result = await register_app_manual(
                app_id="123456",
                private_key=_TEST_PEM,
                client_id="Iv1.abc",
                client_secret="s3cr3t",
            )

        # Store failed, so login is not wired even though creds were given
        assert result["login_enabled"] is False

    @pytest.mark.asyncio
    async def test_hook_config_fetch_failure_graceful(self):
        """When GET /app/hook/config fails, webhook check degrades gracefully."""
        from src.admin.connections.service import register_app_manual

        github_app_response = {
            "id": 123456,
            "slug": "test-app",
            "name": "Test",
            "permissions": {
                "contents": "write",
                "issues": "write",
                "pull_requests": "write",
                "checks": "write",
                "metadata": "read",
            },
            "events": [
                "issues",
                "issue_comment",
                "pull_request",
                "pull_request_review",
                "pull_request_review_comment",
                "label",
            ],
        }

        mock_app_resp = MagicMock()
        mock_app_resp.status_code = 200
        mock_app_resp.json.return_value = github_app_response

        # /app/hook/config returns 404 (e.g. App has webhooks disabled)
        mock_hook_resp = MagicMock()
        mock_hook_resp.status_code = 404

        def _route_get(url, **kwargs):
            if "/app/hook/config" in url:
                return mock_hook_resp
            return mock_app_resp

        with (
            patch("src.admin.connections.github_client._mint_app_jwt", return_value="fake-jwt"),
            patch("httpx.AsyncClient") as mock_client_cls,
            patch("src.admin.connections.service._store_app_credentials", new=AsyncMock(return_value=True)),
            patch("src.admin.connections.service.get_github_app_provider", return_value=MagicMock(invalidate=MagicMock())),
            patch.dict("os.environ", {"WEBHOOK_URL": "https://expected.example.com/github"}),
        ):
            mock_client = AsyncMock()
            mock_client.get.side_effect = _route_get
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            result = await register_app_manual(
                app_id="123456",
                private_key=_TEST_PEM,
                client_id="Iv1.abc",
                client_secret="s3cr3t",
            )

        # Import still succeeds — hook/config failure is non-blocking
        assert result["registered"] is True
        # Should get a "could not verify" warning since we have an expected URL but no actual
        webhook_warnings = [w for w in result["warnings"] if "webhook" in w.lower()]
        assert len(webhook_warnings) == 1
        assert "could not verify" in webhook_warnings[0].lower() or "ensure" in webhook_warnings[0].lower()

    @pytest.mark.asyncio
    async def test_github_unreachable_returns_502(self):
        """When GitHub API is unreachable (network error), the service returns 502."""
        import httpx as _httpx
        from fastapi import HTTPException

        from src.admin.connections.service import register_app_manual

        with (
            patch(
                "src.admin.connections.github_client._mint_app_jwt",
                return_value="fake-jwt",
            ),
            patch("httpx.AsyncClient") as mock_client_cls,
        ):
            mock_client = AsyncMock()
            mock_client.get.side_effect = _httpx.ConnectError("Connection refused")
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            with pytest.raises(HTTPException) as exc_info:
                await register_app_manual(
                    app_id="123456",
                    private_key=_TEST_PEM,
                )

            assert exc_info.value.status_code == 502
            assert "could not reach github" in exc_info.value.detail.lower()


# ---------------------------------------------------------------------------
# PEM normalization unit tests
# ---------------------------------------------------------------------------


class TestNormalizePem:
    """Tests for _normalize_pem helper."""

    def test_escaped_newlines_converted(self):
        """Escaped \\n literals are converted to real newlines."""
        from src.admin.connections.service import _normalize_pem

        raw = "-----BEGIN RSA PRIVATE KEY-----\\nMIIE\\n-----END RSA PRIVATE KEY-----"
        result = _normalize_pem(raw)
        assert "\\n" not in result
        assert result == "-----BEGIN RSA PRIVATE KEY-----\nMIIE\n-----END RSA PRIVATE KEY-----\n"

    def test_real_newlines_preserved(self):
        """Real newlines pass through unchanged."""
        from src.admin.connections.service import _normalize_pem

        raw = "-----BEGIN RSA PRIVATE KEY-----\nMIIE\n-----END RSA PRIVATE KEY-----\n"
        result = _normalize_pem(raw)
        assert result == raw

    def test_trailing_whitespace_stripped(self):
        """Trailing whitespace and extra newlines are cleaned."""
        from src.admin.connections.service import _normalize_pem

        raw = "-----BEGIN RSA PRIVATE KEY-----\nMIIE\n-----END RSA PRIVATE KEY-----   \n\n\n"
        result = _normalize_pem(raw)
        assert result == "-----BEGIN RSA PRIVATE KEY-----\nMIIE\n-----END RSA PRIVATE KEY-----\n"

    def test_windows_line_endings_normalized(self):
        """Windows \\r\\n converted to \\n."""
        from src.admin.connections.service import _normalize_pem

        raw = "-----BEGIN RSA PRIVATE KEY-----\r\nMIIE\r\n-----END RSA PRIVATE KEY-----\r\n"
        result = _normalize_pem(raw)
        assert "\r" not in result
        assert result == "-----BEGIN RSA PRIVATE KEY-----\nMIIE\n-----END RSA PRIVATE KEY-----\n"

    def test_missing_trailing_newline_added(self):
        """PEM without trailing newline gets one appended."""
        from src.admin.connections.service import _normalize_pem

        raw = "-----BEGIN RSA PRIVATE KEY-----\nMIIE\n-----END RSA PRIVATE KEY-----"
        result = _normalize_pem(raw)
        assert result.endswith("\n")


# ---------------------------------------------------------------------------
# Meta-merge tests (Issue #3360)
# ---------------------------------------------------------------------------


class TestStoreAppCredentialsMetaMerge:
    """Tests that _store_app_credentials merges with existing meta when fields are empty."""

    @pytest.mark.asyncio
    async def test_empty_fields_merge_with_existing_meta(self):
        """When webhook_secret/client_id/client_secret are empty, existing values are preserved."""
        import json

        from src.admin.connections.service import _store_app_credentials

        existing_meta = json.dumps(
            {
                "app_id": "123456",
                "app_slug": "old-slug",
                "client_id": "Iv1.existing",
                "client_secret": "existing_secret",
                "webhook_secret": "whsec_existing",
            }
        )

        sm_calls = []

        def mock_get_secret_value(secret_id=None, **kwargs):  # noqa: ARG001
            if secret_id and "meta" in secret_id:
                return {"SecretString": existing_meta}
            raise Exception("not found")

        def mock_create_secret(**kwargs):
            sm_calls.append(("create", kwargs))
            # Simulate ResourceExistsException for put_secret_value path
            from botocore.exceptions import ClientError

            raise ClientError(
                {"Error": {"Code": "ResourceExistsException", "Message": "exists"}},
                "CreateSecret",
            )

        def mock_put_secret_value(**kwargs):
            sm_calls.append(("put", kwargs))

        mock_sm = MagicMock()
        mock_sm.get_secret_value.side_effect = lambda **kw: mock_get_secret_value(secret_id=kw.get("SecretId"))
        mock_sm.create_secret.side_effect = mock_create_secret
        mock_sm.put_secret_value.side_effect = mock_put_secret_value

        with (
            patch("boto3.client", return_value=mock_sm),
            patch.dict("os.environ", {"AWS_REGION": "us-east-1", "ENVIRONMENT": "dev"}),
        ):
            await _store_app_credentials(
                app_id="123456",
                app_slug="my-app",
                pem=_TEST_PEM,
                client_id="",  # Empty — should merge from existing
                client_secret="",  # Empty — should merge from existing
                webhook_secret="",  # Empty — should merge from existing
            )

        # Find the meta write call
        meta_writes = [c for c in sm_calls if c[0] == "put" and "meta" in c[1].get("SecretId", "")]
        assert len(meta_writes) >= 1

        written_meta = json.loads(meta_writes[0][1]["SecretString"])
        # Should preserve existing values since we passed empty strings
        assert written_meta["client_id"] == "Iv1.existing"
        assert written_meta["client_secret"] == "existing_secret"
        assert written_meta["webhook_secret"] == "whsec_existing"

    @pytest.mark.asyncio
    async def test_provided_fields_overwrite_existing(self):
        """When fields are provided, they overwrite existing meta values."""
        import json

        from src.admin.connections.service import _store_app_credentials

        existing_meta = json.dumps(
            {
                "app_id": "123456",
                "app_slug": "old-slug",
                "client_id": "Iv1.old",
                "client_secret": "old_secret",
                "webhook_secret": "whsec_old",
            }
        )

        sm_calls = []

        def mock_get_secret_value(secret_id=None, **kwargs):  # noqa: ARG001
            if secret_id and "meta" in secret_id:
                return {"SecretString": existing_meta}
            raise Exception("not found")

        def mock_create_secret(**kwargs):
            sm_calls.append(("create", kwargs))
            from botocore.exceptions import ClientError

            raise ClientError(
                {"Error": {"Code": "ResourceExistsException", "Message": "exists"}},
                "CreateSecret",
            )

        def mock_put_secret_value(**kwargs):
            sm_calls.append(("put", kwargs))

        mock_sm = MagicMock()
        mock_sm.get_secret_value.side_effect = lambda **kw: mock_get_secret_value(secret_id=kw.get("SecretId"))
        mock_sm.create_secret.side_effect = mock_create_secret
        mock_sm.put_secret_value.side_effect = mock_put_secret_value

        with (
            patch("boto3.client", return_value=mock_sm),
            patch.dict("os.environ", {"AWS_REGION": "us-east-1", "ENVIRONMENT": "dev"}),
        ):
            await _store_app_credentials(
                app_id="123456",
                app_slug="my-app",
                pem=_TEST_PEM,
                client_id="Iv1.new",
                client_secret="new_secret",
                webhook_secret="whsec_new",
            )

        # Find the meta write call
        meta_writes = [c for c in sm_calls if c[0] == "put" and "meta" in c[1].get("SecretId", "")]
        assert len(meta_writes) >= 1

        written_meta = json.loads(meta_writes[0][1]["SecretString"])
        # Should use newly provided values, not existing
        assert written_meta["client_id"] == "Iv1.new"
        assert written_meta["client_secret"] == "new_secret"
        assert written_meta["webhook_secret"] == "whsec_new"
