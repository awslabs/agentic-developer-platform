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
    result = MagicMock()
    result.scalar_one_or_none.return_value = None
    result.scalars.return_value.all.return_value = []
    result.all.return_value = []
    db = MagicMock()
    db.execute = AsyncMock(return_value=result)
    db.get = AsyncMock(return_value=None)
    db.scalar = AsyncMock(return_value=None)
    return db


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
# POST /admin/connections/github/install-start
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
            resp = client.post("/admin/connections/github/install-start")

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
            resp = client.post("/admin/connections/github/install-start")

        assert resp.status_code == 500

    def test_requires_authentication(self, app, mock_db):
        """Unauthenticated request (override raises 401) should return 401."""
        application = FastAPI()
        application.include_router(router)
        # No dependency overrides — real dependency raises 401 without a token
        client = TestClient(application, raise_server_exceptions=False)
        resp = client.post("/admin/connections/github/install-start")
        # Without overrides the real dependency raises 401 or 503 (not configured)
        assert resp.status_code in (401, 503)


# ---------------------------------------------------------------------------
# GET /admin/connections/github/install-callback
# ---------------------------------------------------------------------------


class TestInstallCallbackRoute:
    def test_successful_callback_redirects_to_success(self, app, mock_db):
        user = _make_user()
        client = _make_client(app, user=user, mock_db=mock_db)

        success_result = {
            "success": True,
            "installation_id": 124731131,
            "account_login": "acme-test",
            "account_type": "Organization",
            "error_code": None,
            "error_message": None,
        }

        with patch(
            "src.admin.connections.routes.install_callback",
            new=AsyncMock(return_value=success_result),
        ):
            resp = client.get(
                "/admin/connections/github/install-callback?installation_id=124731131&setup_action=install&state=abc-123",
                follow_redirects=False,
            )

        assert resp.status_code == 302
        assert "success=1" in resp.headers["location"]
        assert "installation_id=124731131" in resp.headers["location"]

    def test_missing_state_returns_html_page_not_an_error_redirect(self, app, mock_db):
        """Issue #2952: Missing state triggers the no-nonce public-App install path.

        Returns an HTML page (200) rather than an error redirect, because
        public-App installs initiated from GitHub have no state nonce and no ADP
        session to redirect into.

        Issue #4016: this case resolves NO org (nothing is persisted), so the
        page must NOT say "Installation complete" — it previously did, which is
        the fail-soft this issue removes. The success wording is asserted
        separately below.
        """
        user = _make_user()
        client = _make_client(app, user=user, mock_db=mock_db)

        resp = client.get(
            "/admin/connections/github/install-callback?installation_id=100",
            follow_redirects=False,
        )
        assert resp.status_code == 200
        assert "Installation complete" not in resp.text
        assert "Installation needs attention" in resp.text
        # The operator gets something actionable to quote to their platform team.
        assert "100" in resp.text

    def test_missing_state_success_page_shown_when_install_actually_landed(self, app, mock_db):
        """Issue #4016: the honest page still says "complete" on a real success."""
        user = _make_user()
        client = _make_client(app, user=user, mock_db=mock_db)

        with patch(
            "src.admin.connections.routes.install_callback",
            new=AsyncMock(
                return_value={
                    "success": True,
                    "installation_id": 100,
                    "account_login": "acme",
                    "account_type": "Organization",
                    "error_code": None,
                    "error_message": None,
                    "no_nonce": True,
                }
            ),
        ):
            resp = client.get(
                "/admin/connections/github/install-callback?installation_id=100",
                follow_redirects=False,
            )

        assert resp.status_code == 200
        assert "Installation complete" in resp.text

    def test_missing_state_partial_install_does_not_claim_completion(self, app, mock_db):
        """Issue #4016: a promotion refusal (#2724) is a successful-but-partial
        install. success stays True by design, so the page must key on `partial`.
        """
        user = _make_user()
        client = _make_client(app, user=user, mock_db=mock_db)

        with patch(
            "src.admin.connections.routes.install_callback",
            new=AsyncMock(
                return_value={
                    "success": True,
                    "installation_id": 100,
                    "account_login": "acme",
                    "account_type": "Organization",
                    "error_code": "promotion_denied",
                    "error_message": "The installation was recorded, but this deployment does not vouch for the organisation.",
                    "no_nonce": True,
                    "partial": True,
                }
            ),
        ):
            resp = client.get(
                "/admin/connections/github/install-callback?installation_id=100",
                follow_redirects=False,
            )

        assert resp.status_code == 200
        assert "Installation complete" not in resp.text
        assert "does not vouch for the organisation" in resp.text

    def test_expired_nonce_redirects_to_error(self, app, mock_db):
        user = _make_user()
        client = _make_client(app, user=user, mock_db=mock_db)

        from src.auth.magic_link import TokenExpiredError

        with patch(
            "src.admin.connections.routes.install_callback",
            new=AsyncMock(side_effect=TokenExpiredError("expired")),
        ):
            resp = client.get(
                "/admin/connections/github/install-callback?installation_id=100&state=old-state",
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
                "/admin/connections/github/install-callback?installation_id=100&state=used-state",
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
                "/admin/connections/github/install-callback?installation_id=100&state=other-state",
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
                "/admin/connections/github/install-callback?installation_id=100&state=conflict-state",
                follow_redirects=False,
            )

        assert resp.status_code == 302
        assert "error=tenant_conflict" in resp.headers["location"]


# ---------------------------------------------------------------------------
# GET /admin/connections
# ---------------------------------------------------------------------------


class TestGetConnectionsRoute:
    @pytest.fixture(autouse=True)
    def mock_memberships(self):
        with patch(
            "src.shared.identity.workspaces.memberships_for_login",
            new=AsyncMock(return_value=(None, {})),
        ) as memberships:
            yield memberships

    def test_returns_connections_list(self, app, mock_db, mock_memberships):
        user = _make_user()
        client = _make_client(app, user=user, mock_db=mock_db)
        login = MagicMock(id="canonical-user")
        local_user = MagicMock(id="org-local-user")
        mock_memberships.return_value = (
            login,
            {"org-002": (login, None), "org-001": (local_user, None)},
        )

        connections_result = ConnectionsListResponse(
            connections=[
                GitHubConnectionItem(
                    installation_id=124731131,
                    account_login="acme-test",
                    account_type="Organization",
                    repository_selection="selected",
                    repository_count=2,
                    installed_at=datetime.now(UTC),
                    configure_url="https://github.com/organizations/acme-test/settings/installations/124731131",
                )
            ]
        )

        with patch(
            "src.admin.connections.routes.list_connections",
            new=AsyncMock(return_value=connections_result),
        ) as list_mock:
            resp = client.get("/admin/connections")

        assert resp.status_code == 200
        mock_memberships.assert_awaited_once_with(mock_db, user.user_id, username=user.cognito_username)
        list_mock.assert_awaited_once_with(
            caller_org_id="org-001",
            caller_user_id=user.user_id,
            db=mock_db,
            member_tenant_ids=["org-002", "org-001"],
            caller_is_admin=False,
            caller_pg_user_id="org-local-user",
        )
        body = resp.json()
        assert len(body["connections"]) == 1
        assert body["connections"][0]["account_login"] == "acme-test"
        assert body["connections"][0]["installation_id"] == 124731131

    def test_returns_empty_list_when_no_connections(self, app, mock_db):
        user = _make_user()
        client = _make_client(app, user=user, mock_db=mock_db)

        with patch(
            "src.admin.connections.routes.list_connections",
            new=AsyncMock(return_value=ConnectionsListResponse(connections=[])),
        ) as list_mock:
            resp = client.get("/admin/connections")

        assert resp.status_code == 200
        assert resp.json()["connections"] == []
        list_mock.assert_awaited_once_with(
            caller_org_id=user.org_id,
            caller_user_id=user.user_id,
            db=mock_db,
            member_tenant_ids=None,
            caller_is_admin=False,
            caller_pg_user_id=None,
        )

    def test_returns_500_on_service_error(self, app, mock_db):
        user = _make_user()
        client = _make_client(app, user=user, mock_db=mock_db)

        with patch(
            "src.admin.connections.routes.list_connections",
            new=AsyncMock(side_effect=RuntimeError("DB error")),
        ) as list_mock:
            resp = client.get("/admin/connections")

        assert resp.status_code == 500
        list_mock.assert_awaited_once()

    def test_membership_lookup_failure_does_not_list_connections(self, app, mock_db, mock_memberships):
        client = _make_client(app, user=_make_user(), mock_db=mock_db)
        mock_memberships.side_effect = RuntimeError("Membership lookup failed")

        with patch("src.admin.connections.routes.list_connections", new=AsyncMock()) as list_mock:
            resp = client.get("/admin/connections")

        assert resp.status_code == 500
        list_mock.assert_not_awaited()


# ---------------------------------------------------------------------------
# DELETE /admin/connections/github/{installation_id}
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
            resp = client.delete("/admin/connections/github/124731131")

        assert resp.status_code == 200
        body = resp.json()
        assert body["deleted"] is True
        assert body["installation_id"] == 124731131

    def test_non_admin_non_installer_returns_403(self, app, mock_db):
        """Issue #3073: Non-admin who is NOT the installer gets 403 from service layer."""
        user = _make_user(is_admin=False)
        client = _make_client(app, user=user, mock_db=mock_db)

        with patch(
            "src.admin.connections.routes.delete_connection",
            new=AsyncMock(side_effect=PermissionError("You do not have permission to disconnect")),
        ):
            resp = client.delete("/admin/connections/github/124731131")

        assert resp.status_code == 403

    def test_not_found_returns_404(self, app, mock_db):
        user = _make_user(is_admin=True)
        client = _make_client(app, user=user, mock_db=mock_db)

        with patch(
            "src.admin.connections.routes.delete_connection",
            new=AsyncMock(side_effect=ValueError("not found")),
        ):
            resp = client.delete("/admin/connections/github/9999")

        assert resp.status_code == 404

    def test_permission_error_returns_403(self, app, mock_db):
        user = _make_user(is_admin=True)
        client = _make_client(app, user=user, mock_db=mock_db)

        with patch(
            "src.admin.connections.routes.delete_connection",
            new=AsyncMock(side_effect=PermissionError("wrong tenant")),
        ):
            resp = client.delete("/admin/connections/github/9999")

        assert resp.status_code == 403
