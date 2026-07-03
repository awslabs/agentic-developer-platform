"""Unit tests for GitHub App lifecycle endpoints (status, rotate-key, disconnect).

Issue #2595: Platform-admin endpoints for App status, key rotation, and disconnect.
Tests cover:
- All three endpoints return 403 for non-platform_admin users
- Status endpoint never leaks private key or client secret
- rotate-key and disconnect both call invalidate_app_credentials_cache()
- Status returns correct registration state
- Disconnect counts affected installations
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.admin.connections.routes import router
from src.admin.connections.schemas import (
    AppStatusResponse,
    DisconnectAppResponse,
    RotateKeyResponse,
)
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
# GET /admin/connections/github/app/status
# ---------------------------------------------------------------------------


class TestAppStatus:
    """Tests for the status endpoint."""

    def test_non_admin_returns_403(self, app, mock_db):
        """Non-admin users must be rejected with 403."""
        user = _make_user(is_admin=False)
        client = _make_client(app, user=user, mock_db=mock_db)

        resp = client.get("/admin/connections/github/app/status")
        assert resp.status_code == 403
        assert "platform administrator" in resp.json()["detail"].lower()

    def test_status_registered(self, app, mock_db):
        """Platform admin sees registered=True when App exists."""
        user = _make_user(is_admin=True)
        client = _make_client(app, user=user, mock_db=mock_db)

        expected = AppStatusResponse(
            registered=True,
            app_slug="adp-agent-platform",
            app_id="3410773",
            owner_type=None,
            created_at="2026-06-15T10:00:00+00:00",
        )

        with patch(
            "src.admin.connections.routes.get_app_status",
            new=AsyncMock(return_value=expected),
        ):
            resp = client.get("/admin/connections/github/app/status")

        assert resp.status_code == 200
        body = resp.json()
        assert body["registered"] is True
        assert body["app_slug"] == "adp-agent-platform"
        assert body["app_id"] == "3410773"

    def test_status_not_registered(self, app, mock_db):
        """Platform admin sees registered=False when no App exists."""
        user = _make_user(is_admin=True)
        client = _make_client(app, user=user, mock_db=mock_db)

        expected = AppStatusResponse(registered=False)

        with patch(
            "src.admin.connections.routes.get_app_status",
            new=AsyncMock(return_value=expected),
        ):
            resp = client.get("/admin/connections/github/app/status")

        assert resp.status_code == 200
        body = resp.json()
        assert body["registered"] is False
        assert body["app_slug"] is None
        assert body["app_id"] is None

    def test_status_never_leaks_private_key(self, app, mock_db):
        """Status response must never contain the private key or client secret."""
        user = _make_user(is_admin=True)
        client = _make_client(app, user=user, mock_db=mock_db)

        expected = AppStatusResponse(
            registered=True,
            app_slug="adp-agent-platform",
            app_id="3410773",
            owner_type=None,
            created_at="2026-06-15T10:00:00+00:00",
        )

        with patch(
            "src.admin.connections.routes.get_app_status",
            new=AsyncMock(return_value=expected),
        ):
            resp = client.get("/admin/connections/github/app/status")

        body = resp.text
        assert "PRIVATE KEY" not in body
        assert "client_secret" not in body
        assert "private_key" not in body

    def test_status_500_on_unexpected_error(self, app, mock_db):
        """Unexpected exceptions return 500."""
        user = _make_user(is_admin=True)
        client = _make_client(app, user=user, mock_db=mock_db)

        with patch(
            "src.admin.connections.routes.get_app_status",
            new=AsyncMock(side_effect=RuntimeError("boom")),
        ):
            resp = client.get("/admin/connections/github/app/status")

        assert resp.status_code == 500


# ---------------------------------------------------------------------------
# POST /admin/connections/github/app/rotate-key
# ---------------------------------------------------------------------------


class TestAppRotateKey:
    """Tests for the rotate-key endpoint."""

    def test_non_admin_returns_403(self, app, mock_db):
        """Non-admin users must be rejected with 403."""
        user = _make_user(is_admin=False)
        client = _make_client(app, user=user, mock_db=mock_db)

        resp = client.post("/admin/connections/github/app/rotate-key")
        assert resp.status_code == 403
        assert "platform administrator" in resp.json()["detail"].lower()

    def test_rotate_key_success(self, app, mock_db):
        """Platform admin can rotate the key successfully."""
        user = _make_user(is_admin=True)
        client = _make_client(app, user=user, mock_db=mock_db)

        expected = RotateKeyResponse(
            rotated=True,
            app_id="3410773",
            message="Private key rotated successfully. New key is active immediately.",
        )

        with patch(
            "src.admin.connections.routes.rotate_app_key",
            new=AsyncMock(return_value=expected),
        ):
            resp = client.post("/admin/connections/github/app/rotate-key")

        assert resp.status_code == 200
        body = resp.json()
        assert body["rotated"] is True
        assert body["app_id"] == "3410773"

    def test_rotate_key_no_app_registered(self, app, mock_db):
        """rotate-key returns 404 when no App is registered."""
        from fastapi import HTTPException

        user = _make_user(is_admin=True)
        client = _make_client(app, user=user, mock_db=mock_db)

        with patch(
            "src.admin.connections.routes.rotate_app_key",
            new=AsyncMock(side_effect=HTTPException(status_code=404, detail="No GitHub App registered.")),
        ):
            resp = client.post("/admin/connections/github/app/rotate-key")

        assert resp.status_code == 404

    def test_rotate_key_500_on_unexpected_error(self, app, mock_db):
        """Unexpected exceptions return 500."""
        user = _make_user(is_admin=True)
        client = _make_client(app, user=user, mock_db=mock_db)

        with patch(
            "src.admin.connections.routes.rotate_app_key",
            new=AsyncMock(side_effect=RuntimeError("boom")),
        ):
            resp = client.post("/admin/connections/github/app/rotate-key")

        assert resp.status_code == 500


# ---------------------------------------------------------------------------
# POST /admin/connections/github/app/disconnect
# ---------------------------------------------------------------------------


class TestAppDisconnect:
    """Tests for the disconnect endpoint."""

    def test_non_admin_returns_403(self, app, mock_db):
        """Non-admin users must be rejected with 403."""
        user = _make_user(is_admin=False)
        client = _make_client(app, user=user, mock_db=mock_db)

        resp = client.post("/admin/connections/github/app/disconnect")
        assert resp.status_code == 403
        assert "platform administrator" in resp.json()["detail"].lower()

    def test_disconnect_success(self, app, mock_db):
        """Platform admin can disconnect the App successfully."""
        user = _make_user(is_admin=True)
        client = _make_client(app, user=user, mock_db=mock_db)

        expected = DisconnectAppResponse(
            disconnected=True,
            app_id="3410773",
            message="GitHub App disconnected from this deployment.",
            affected_installations=5,
        )

        with patch(
            "src.admin.connections.routes.disconnect_app",
            new=AsyncMock(return_value=expected),
        ):
            resp = client.post("/admin/connections/github/app/disconnect")

        assert resp.status_code == 200
        body = resp.json()
        assert body["disconnected"] is True
        assert body["app_id"] == "3410773"
        assert body["affected_installations"] == 5

    def test_disconnect_no_app_registered(self, app, mock_db):
        """disconnect returns 404 when no App is registered."""
        from fastapi import HTTPException

        user = _make_user(is_admin=True)
        client = _make_client(app, user=user, mock_db=mock_db)

        with patch(
            "src.admin.connections.routes.disconnect_app",
            new=AsyncMock(side_effect=HTTPException(status_code=404, detail="No GitHub App registered.")),
        ):
            resp = client.post("/admin/connections/github/app/disconnect")

        assert resp.status_code == 404

    def test_disconnect_500_on_unexpected_error(self, app, mock_db):
        """Unexpected exceptions return 500."""
        user = _make_user(is_admin=True)
        client = _make_client(app, user=user, mock_db=mock_db)

        with patch(
            "src.admin.connections.routes.disconnect_app",
            new=AsyncMock(side_effect=RuntimeError("boom")),
        ):
            resp = client.post("/admin/connections/github/app/disconnect")

        assert resp.status_code == 500


# ---------------------------------------------------------------------------
# Service-level tests: invalidate() is called
# ---------------------------------------------------------------------------


class TestRotateKeyCallsInvalidate:
    """Verify rotate_app_key calls invalidate_app_credentials_cache."""

    @pytest.mark.asyncio
    async def test_rotate_calls_invalidate(self):
        """rotate_app_key must call invalidate_app_credentials_cache on success."""
        from src.admin.connections.service import rotate_app_key

        mock_sm = MagicMock()
        mock_sm.get_secret_value.side_effect = [
            {"SecretString": "123456"},  # app_id
            {"SecretString": "-----BEGIN RSA PRIVATE KEY-----\nfake\n-----END RSA PRIVATE KEY-----"},  # key
        ]
        mock_sm.put_secret_value = MagicMock()

        mock_http_resp = MagicMock()
        mock_http_resp.status_code = 201
        mock_http_resp.json.return_value = {
            "pem": "-----BEGIN RSA PRIVATE KEY-----\nnewkey\n-----END RSA PRIVATE KEY-----",
        }

        with (
            patch("boto3.client", return_value=mock_sm),
            patch.dict("os.environ", {"ENVIRONMENT": "dev", "AWS_REGION": "us-east-1"}),
            patch("src.admin.connections.service.GitHubAppClient") as mock_client_cls,
            patch("src.admin.connections.service.invalidate_app_credentials_cache") as mock_invalidate,
            patch("asyncio.to_thread", new=AsyncMock()),
        ):
            mock_client = MagicMock()
            mock_client._http_client = MagicMock()
            mock_client._http_client.post = AsyncMock(return_value=mock_http_resp)
            mock_client._auth_headers.return_value = {"Authorization": "Bearer fake"}
            mock_client_cls.return_value = mock_client

            result = await rotate_app_key()

        assert result.rotated is True
        mock_invalidate.assert_called_once()


class TestDisconnectCallsInvalidate:
    """Verify disconnect_app calls invalidate_app_credentials_cache."""

    @pytest.mark.asyncio
    async def test_disconnect_calls_invalidate(self):
        """disconnect_app must call invalidate_app_credentials_cache on success."""
        from src.admin.connections.service import disconnect_app

        mock_sm = MagicMock()
        mock_sm.get_secret_value.return_value = {"SecretString": "123456"}
        mock_sm.delete_secret = MagicMock()

        with (
            patch("boto3.client", return_value=mock_sm),
            patch.dict("os.environ", {"ENVIRONMENT": "dev", "AWS_REGION": "us-east-1"}),
            patch("src.admin.connections.service.invalidate_app_credentials_cache") as mock_invalidate,
            patch("asyncio.to_thread", new=AsyncMock()),
            patch("src.shared.database.get_session_factory") as mock_get_factory,
        ):
            # Mock the DB session for counting affected installations
            mock_db = AsyncMock()
            mock_result = MagicMock()
            mock_result.scalar.return_value = 3
            mock_db.execute = AsyncMock(return_value=mock_result)
            mock_db.__aenter__ = AsyncMock(return_value=mock_db)
            mock_db.__aexit__ = AsyncMock(return_value=None)
            mock_factory = MagicMock()
            mock_factory.return_value = mock_db
            mock_get_factory.return_value = mock_factory

            result = await disconnect_app()

        assert result.disconnected is True
        assert result.affected_installations == 3
        mock_invalidate.assert_called_once()


class TestGetAppStatusService:
    """Tests for the get_app_status service function."""

    @pytest.mark.asyncio
    async def test_returns_not_registered_when_no_secret(self):
        """get_app_status returns registered=False when secret doesn't exist."""
        from botocore.exceptions import ClientError

        from src.admin.connections.service import get_app_status

        mock_sm = MagicMock()
        mock_sm.get_secret_value.side_effect = ClientError(
            {"Error": {"Code": "ResourceNotFoundException", "Message": "not found"}},
            "GetSecretValue",
        )

        with (
            patch("boto3.client", return_value=mock_sm),
            patch.dict("os.environ", {"ENVIRONMENT": "dev", "AWS_REGION": "us-east-1"}),
        ):
            result = await get_app_status()

        assert result.registered is False
        assert result.app_id is None
        assert result.app_slug is None

    @pytest.mark.asyncio
    async def test_returns_503_on_access_denied(self):
        """get_app_status returns 503 (not 500) when IAM denies access to the secret.

        Issue #2619: AccessDenied must NOT masquerade as 'unregistered' or as a bare
        500 that the UI interprets as 'unregistered'. The 503 signals 'status unavailable'
        so the frontend can show an error state instead of the dangerous registration CTA.
        """
        from botocore.exceptions import ClientError
        from fastapi import HTTPException

        from src.admin.connections.service import get_app_status

        mock_sm = MagicMock()
        mock_sm.get_secret_value.side_effect = ClientError(
            {"Error": {"Code": "AccessDeniedException", "Message": "User: arn:... is not authorized"}},
            "GetSecretValue",
        )

        with (
            patch("boto3.client", return_value=mock_sm),
            patch.dict("os.environ", {"ENVIRONMENT": "dev", "AWS_REGION": "us-east-1"}),
        ):
            with pytest.raises(HTTPException) as exc_info:
                await get_app_status()

        assert exc_info.value.status_code == 503
        assert "access denied" in exc_info.value.detail.lower()

    @pytest.mark.asyncio
    async def test_returns_registered_with_metadata(self):
        """get_app_status returns correct data when App is registered."""
        import json

        from src.admin.connections.service import get_app_status

        mock_sm = MagicMock()
        mock_sm.get_secret_value.side_effect = [
            {"SecretString": "3410773"},  # id_path
            {"SecretString": json.dumps({"app_slug": "adp-agent-platform", "client_id": "Iv1.abc"})},  # meta_path
        ]
        mock_sm.describe_secret.return_value = {
            "CreatedDate": datetime(2026, 6, 15, 10, 0, 0),
        }

        with (
            patch("boto3.client", return_value=mock_sm),
            patch.dict("os.environ", {"ENVIRONMENT": "dev", "AWS_REGION": "us-east-1"}),
        ):
            result = await get_app_status()

        assert result.registered is True
        assert result.app_id == "3410773"
        assert result.app_slug == "adp-agent-platform"

    @pytest.mark.asyncio
    async def test_status_never_returns_sensitive_fields(self):
        """get_app_status must never expose private_key or client_secret."""
        import json

        from src.admin.connections.service import get_app_status

        mock_sm = MagicMock()
        mock_sm.get_secret_value.side_effect = [
            {"SecretString": "3410773"},  # id_path
            {
                "SecretString": json.dumps(
                    {
                        "app_slug": "adp-agent-platform",
                        "client_id": "Iv1.abc",
                        "client_secret": "super_secret_value",
                        "webhook_secret": "whsec_secret",
                    }
                )
            },  # meta_path
        ]
        mock_sm.describe_secret.return_value = {"CreatedDate": datetime(2026, 6, 15, 10, 0, 0)}

        with (
            patch("boto3.client", return_value=mock_sm),
            patch.dict("os.environ", {"ENVIRONMENT": "dev", "AWS_REGION": "us-east-1"}),
        ):
            result = await get_app_status()

        # Serialize to dict and check no sensitive fields leaked
        result_dict = result.model_dump()
        result_str = str(result_dict)
        assert "super_secret_value" not in result_str
        assert "whsec_secret" not in result_str
        assert "private_key" not in result_str
        assert "client_secret" not in result_str


class TestInstallStartUnmasking:
    """Issue #2700: install-start must surface deliberate HTTPExceptions."""

    def test_503_not_masked_as_500(self, app, mock_db):
        """service.install_start raising HTTPException(503) → response is 503, not 500."""
        from fastapi import HTTPException

        from src.admin.connections import routes as routes_mod

        user = _make_user(is_admin=True)
        client = _make_client(app, user=user, mock_db=mock_db)

        detail = "GitHub App not configured. Register via Settings > Connections"
        with patch.object(
            routes_mod,
            "install_start",
            new=AsyncMock(side_effect=HTTPException(status_code=503, detail=detail)),
        ):
            resp = client.post("/admin/connections/github/install-start")

        assert resp.status_code == 503
        assert "not configured" in resp.json()["detail"].lower()

    def test_unexpected_error_still_500(self, app, mock_db):
        """Non-HTTPException errors still produce a generic 500."""
        from src.admin.connections import routes as routes_mod

        user = _make_user(is_admin=True)
        client = _make_client(app, user=user, mock_db=mock_db)

        with patch.object(
            routes_mod,
            "install_start",
            new=AsyncMock(side_effect=RuntimeError("boom")),
        ):
            resp = client.post("/admin/connections/github/install-start")

        assert resp.status_code == 500


class TestStatusInstallReady:
    """Issue #2700: get_app_status surfaces install_ready."""

    @pytest.mark.asyncio
    async def test_install_ready_true_when_slug_resolves(self):
        """Slug present in meta → install_ready True."""
        import json

        from src.admin.connections.service import get_app_status

        mock_sm = MagicMock()
        mock_sm.get_secret_value.side_effect = [
            {"SecretString": "3410773"},
            {"SecretString": json.dumps({"app_slug": "adp-agent-platform"})},
        ]
        mock_sm.describe_secret.return_value = {"CreatedDate": datetime(2026, 6, 15, 10, 0, 0)}

        with (
            patch("boto3.client", return_value=mock_sm),
            patch.dict("os.environ", {"ENVIRONMENT": "dev", "AWS_REGION": "us-east-1"}),
        ):
            result = await get_app_status()

        assert result.registered is True
        assert result.install_ready is True

    @pytest.mark.asyncio
    async def test_install_ready_false_when_slug_unresolvable(self):
        """Registered but no slug anywhere → install_ready False."""
        import json

        from src.admin.connections.service import get_app_status

        mock_sm = MagicMock()
        mock_sm.get_secret_value.side_effect = [
            {"SecretString": "3410773"},
            {"SecretString": json.dumps({"client_id": "Iv1.abc"})},  # no app_slug
        ]
        mock_sm.describe_secret.return_value = {"CreatedDate": datetime(2026, 6, 15, 10, 0, 0)}

        # Ensure env-var fallback is also empty so slug stays None
        with (
            patch("boto3.client", return_value=mock_sm),
            patch.dict("os.environ", {"ENVIRONMENT": "dev", "AWS_REGION": "us-east-1", "BG_GITHUB_APP_SLUG": ""}),
        ):
            result = await get_app_status()

        assert result.registered is True
        assert result.install_ready is False


class TestInvalidateAppCredentialsCache:
    """Tests for the invalidate function itself."""

    def test_invalidate_clears_metadata_cache(self):
        """invalidate_app_credentials_cache clears the _metadata_cache."""
        from src.admin.connections.service import (
            _metadata_cache,
            invalidate_app_credentials_cache,
        )

        # Seed cache with some data
        _metadata_cache[12345] = (0.0, {"account": {"login": "test"}})
        _metadata_cache[67890] = (0.0, {"account": {"login": "other"}})

        invalidate_app_credentials_cache()

        assert len(_metadata_cache) == 0
