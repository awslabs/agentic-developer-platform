"""Unit tests for the connections router.

Issue #465: GitHub App install + connection management endpoints.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.admin.connections.routes import router
from src.admin.connections.schemas import (
    ConnectionsListResponse,
    DeleteConnectionResponse,
    GitHubConnectionItem,
    InstallStartResponse,
)
from src.auth.dependencies import get_current_user, require_admin
from src.shared.database import get_db
from src.shared.schemas.auth import TokenContext

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_user(*, is_admin: bool = False, org_id: str = "org-001") -> TokenContext:
    return TokenContext(
        user_id="user-001",
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

    async def override_require_admin():
        if not user.is_admin:
            from fastapi import HTTPException

            raise HTTPException(status_code=403, detail="Admin required")
        return user

    async def override_get_db():
        yield mock_db

    app.dependency_overrides[get_current_user] = override_get_current_user
    app.dependency_overrides[require_admin] = override_require_admin
    app.dependency_overrides[get_db] = override_get_db
    return TestClient(app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# POST /api/admin/connections/github/install-start
# ---------------------------------------------------------------------------


class TestInstallStartRoute:
    def test_returns_install_url_and_state(self, app, mock_db):
        user = _make_user()
        client = _make_client(app, user=user, mock_db=mock_db)

        expected = InstallStartResponse(
            install_url="https://github.com/apps/test-adp-agent/installations/new?state=abc-123",
            state_token="abc-123",
            expires_at=datetime.now(UTC) + timedelta(minutes=15),
        )

        with patch(
            "src.admin.connections.routes.install_start",
            new=AsyncMock(return_value=expected),
        ):
            resp = client.post("/api/admin/connections/github/install-start")

        assert resp.status_code == 200
        body = resp.json()
        assert body["state_token"] == "abc-123"
        assert "install_url" in body
        assert "expires_at" in body

    def test_returns_500_on_service_error(self, app, mock_db):
        user = _make_user()
        client = _make_client(app, user=user, mock_db=mock_db)

        with patch(
            "src.admin.connections.routes.install_start",
            new=AsyncMock(side_effect=RuntimeError("DB down")),
        ):
            resp = client.post("/api/admin/connections/github/install-start")

        assert resp.status_code == 500

    def test_requires_authentication(self, app, mock_db):
        """Unauthenticated request (override raises 401) should return 401."""
        application = FastAPI()
        application.include_router(router)
        # No dependency overrides — real dependency raises 401 without a token
        client = TestClient(application, raise_server_exceptions=False)
        resp = client.post("/api/admin/connections/github/install-start")
        # Without overrides the real dependency raises 401 or 503 (not configured)
        assert resp.status_code in (401, 503)


# ---------------------------------------------------------------------------
# GET /api/admin/connections/github/install-callback
# ---------------------------------------------------------------------------


class TestInstallCallbackRoute:
    def test_successful_callback_redirects_to_success(self, app, mock_db):
        user = _make_user()
        client = _make_client(app, user=user, mock_db=mock_db)

        success_result = {
            "success": True,
            "installation_id": 124731131,
            "account_login": "sophos-test",
            "account_type": "Organization",
            "error_code": None,
            "error_message": None,
        }

        with patch(
            "src.admin.connections.routes.install_callback",
            new=AsyncMock(return_value=success_result),
        ):
            resp = client.get(
                "/api/admin/connections/github/install-callback?installation_id=124731131&setup_action=install&state=abc-123",
                follow_redirects=False,
            )

        assert resp.status_code == 302
        assert "success=1" in resp.headers["location"]
        assert "installation_id=124731131" in resp.headers["location"]

    def test_missing_state_redirects_to_error(self, app, mock_db):
        user = _make_user()
        client = _make_client(app, user=user, mock_db=mock_db)

        resp = client.get(
            "/api/admin/connections/github/install-callback?installation_id=100",
            follow_redirects=False,
        )
        assert resp.status_code == 302
        assert "error=missing_state" in resp.headers["location"]

    def test_expired_nonce_redirects_to_error(self, app, mock_db):
        user = _make_user()
        client = _make_client(app, user=user, mock_db=mock_db)

        from src.auth.magic_link import TokenExpiredError

        with patch(
            "src.admin.connections.routes.install_callback",
            new=AsyncMock(side_effect=TokenExpiredError("expired")),
        ):
            resp = client.get(
                "/api/admin/connections/github/install-callback?installation_id=100&state=old-state",
                follow_redirects=False,
            )

        assert resp.status_code == 302
        assert "error=invalid_state" in resp.headers["location"]

    def test_consumed_nonce_redirects_to_error(self, app, mock_db):
        user = _make_user()
        client = _make_client(app, user=user, mock_db=mock_db)

        from src.auth.magic_link import NonceAlreadyConsumedError

        with patch(
            "src.admin.connections.routes.install_callback",
            new=AsyncMock(side_effect=NonceAlreadyConsumedError("used")),
        ):
            resp = client.get(
                "/api/admin/connections/github/install-callback?installation_id=100&state=used-state",
                follow_redirects=False,
            )

        assert resp.status_code == 302
        assert "error=state_replayed" in resp.headers["location"]

    def test_cross_user_nonce_redirects_to_unauthorized(self, app, mock_db):
        user = _make_user()
        client = _make_client(app, user=user, mock_db=mock_db)

        from src.auth.magic_link import TargetUserMismatchError

        with patch(
            "src.admin.connections.routes.install_callback",
            new=AsyncMock(side_effect=TargetUserMismatchError("wrong user")),
        ):
            resp = client.get(
                "/api/admin/connections/github/install-callback?installation_id=100&state=other-state",
                follow_redirects=False,
            )

        assert resp.status_code == 302
        assert "error=unauthorized" in resp.headers["location"]

    def test_tenant_conflict_redirects_to_error(self, app, mock_db):
        user = _make_user()
        client = _make_client(app, user=user, mock_db=mock_db)

        with patch(
            "src.admin.connections.routes.install_callback",
            new=AsyncMock(side_effect=PermissionError("org claimed by another tenant")),
        ):
            resp = client.get(
                "/api/admin/connections/github/install-callback?installation_id=100&state=conflict-state",
                follow_redirects=False,
            )

        assert resp.status_code == 302
        assert "error=tenant_conflict" in resp.headers["location"]


# ---------------------------------------------------------------------------
# GET /api/admin/connections
# ---------------------------------------------------------------------------


class TestGetConnectionsRoute:
    def test_returns_connections_list(self, app, mock_db):
        user = _make_user()
        client = _make_client(app, user=user, mock_db=mock_db)

        connections_result = ConnectionsListResponse(
            connections=[
                GitHubConnectionItem(
                    installation_id=124731131,
                    account_login="sophos-test",
                    account_type="Organization",
                    repository_selection="selected",
                    repository_count=2,
                    installed_at=datetime.now(UTC),
                    configure_url="https://github.com/organizations/sophos-test/settings/installations/124731131",
                )
            ]
        )

        with patch(
            "src.admin.connections.routes.list_connections",
            new=AsyncMock(return_value=connections_result),
        ):
            resp = client.get("/api/admin/connections")

        assert resp.status_code == 200
        body = resp.json()
        assert len(body["connections"]) == 1
        assert body["connections"][0]["account_login"] == "sophos-test"
        assert body["connections"][0]["installation_id"] == 124731131

    def test_returns_empty_list_when_no_connections(self, app, mock_db):
        user = _make_user()
        client = _make_client(app, user=user, mock_db=mock_db)

        with patch(
            "src.admin.connections.routes.list_connections",
            new=AsyncMock(return_value=ConnectionsListResponse(connections=[])),
        ):
            resp = client.get("/api/admin/connections")

        assert resp.status_code == 200
        assert resp.json()["connections"] == []

    def test_returns_500_on_service_error(self, app, mock_db):
        user = _make_user()
        client = _make_client(app, user=user, mock_db=mock_db)

        with patch(
            "src.admin.connections.routes.list_connections",
            new=AsyncMock(side_effect=RuntimeError("DB error")),
        ):
            resp = client.get("/api/admin/connections")

        assert resp.status_code == 500


# ---------------------------------------------------------------------------
# DELETE /api/admin/connections/github/{installation_id}
# ---------------------------------------------------------------------------


class TestDeleteConnectionRoute:
    def test_admin_can_delete(self, app, mock_db):
        user = _make_user(is_admin=True)
        client = _make_client(app, user=user, mock_db=mock_db)

        delete_result = DeleteConnectionResponse(deleted=True, installation_id=124731131)

        with patch(
            "src.admin.connections.routes.delete_connection",
            new=AsyncMock(return_value=delete_result),
        ):
            resp = client.delete("/api/admin/connections/github/124731131")

        assert resp.status_code == 200
        body = resp.json()
        assert body["deleted"] is True
        assert body["installation_id"] == 124731131

    def test_non_admin_returns_403(self, app, mock_db):
        user = _make_user(is_admin=False)
        client = _make_client(app, user=user, mock_db=mock_db)

        resp = client.delete("/api/admin/connections/github/124731131")
        assert resp.status_code == 403

    def test_not_found_returns_404(self, app, mock_db):
        user = _make_user(is_admin=True)
        client = _make_client(app, user=user, mock_db=mock_db)

        with patch(
            "src.admin.connections.routes.delete_connection",
            new=AsyncMock(side_effect=ValueError("not found")),
        ):
            resp = client.delete("/api/admin/connections/github/9999")

        assert resp.status_code == 404

    def test_permission_error_returns_403(self, app, mock_db):
        user = _make_user(is_admin=True)
        client = _make_client(app, user=user, mock_db=mock_db)

        with patch(
            "src.admin.connections.routes.delete_connection",
            new=AsyncMock(side_effect=PermissionError("wrong tenant")),
        ):
            resp = client.delete("/api/admin/connections/github/9999")

        assert resp.status_code == 403
