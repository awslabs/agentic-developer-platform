"""Unit tests for Activity API routes.

Covers:
- /me/agent-invocations uses token.user_id and ignores any user_id query param (privacy)
- Missing GSI → 200 with empty items, not 500 (deploy-order independence)
- Filtered empty page with non-null last_key preserved in response (short-page contract)
- Cursor round-trip; bad cursor → 400
- /admin/agent-invocations returns 403 for non-admin without USAGE_READ permission
- Org admin is scoped to own tenant_id; platform admin can pass tenant_id
- Phase 6 (#1461): chain endpoints scoped by user/tenant
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.activity.routes import get_access_control, get_activity_service, router
from src.activity.schemas import (
    InvocationChainItem,
    InvocationChainResponse,
    InvocationItem,
    InvocationListResponse,
)
from src.activity.service import ActivityService
from src.admin.access_control import AccessControl
from src.admin.exceptions import AccessDeniedError
from src.auth.dependencies import get_current_user
from src.shared.database import get_db

# The canonical users.id that the regular_user's Cognito sub (token.user_id)
# resolves to. Invocation rows are stored under this canonical id, not the sub.
CANONICAL_USER_ID = "canonical-abc-999"


@pytest.fixture
def app():
    """Create a test FastAPI app with activity routes."""
    app = FastAPI()
    app.include_router(router)
    return app


@pytest.fixture
def mock_db():
    """Mock AsyncSession whose scalar() resolves any cognito_sub to the
    canonical users.id (mirrors `select(User.id).where(User.cognito_sub == sub)`)."""
    db = MagicMock()
    db.scalar = AsyncMock(return_value=CANONICAL_USER_ID)
    return db


@pytest.fixture
def mock_service():
    """Create a mock ActivityService."""
    service = MagicMock(spec=ActivityService)
    service.query_by_user = MagicMock(return_value=InvocationListResponse(items=[], count=0, last_key=None))
    service.query_by_tenant = MagicMock(return_value=InvocationListResponse(items=[], count=0, last_key=None))
    service.get_chain = MagicMock(
        return_value=InvocationChainResponse(
            correlation_id="chain-001",
            root_human_id=None,
            is_human_rooted=True,
            items=[],
            total_count=0,
            depth_capped=False,
        )
    )
    return service


@pytest.fixture
def mock_access():
    """Create a mock AccessControl that allows everything."""
    ac = MagicMock(spec=AccessControl)
    ac.check_permission = AsyncMock(return_value=True)
    ac.get_accessible_organizations = AsyncMock(return_value=None)
    return ac


@pytest.fixture
def client(app, mock_service, mock_access, mock_db, regular_user):
    """Create a test client with regular user auth."""

    async def override_current_user():
        return regular_user

    def override_service():
        return mock_service

    async def override_access():
        return mock_access

    async def override_db():
        return mock_db

    app.dependency_overrides[get_current_user] = override_current_user
    app.dependency_overrides[get_activity_service] = override_service
    app.dependency_overrides[get_access_control] = override_access
    app.dependency_overrides[get_db] = override_db
    return TestClient(app)


@pytest.fixture
def admin_client(app, mock_service, mock_access, mock_db, admin_user):
    """Create a test client with platform admin auth."""

    async def override_current_user():
        return admin_user

    def override_service():
        return mock_service

    async def override_access():
        return mock_access

    async def override_db():
        return mock_db

    app.dependency_overrides[get_current_user] = override_current_user
    app.dependency_overrides[get_activity_service] = override_service
    app.dependency_overrides[get_access_control] = override_access
    app.dependency_overrides[get_db] = override_db
    return TestClient(app)


class TestGetMyInvocations:
    """/me/agent-invocations endpoint tests."""

    def test_uses_token_user_id_not_param(self, client, mock_service, regular_user):
        """The endpoint resolves the token's Cognito sub to the canonical user_id,
        ignoring any user_id query param (privacy)."""
        resp = client.get("/me/agent-invocations?user_id=attacker-id")
        assert resp.status_code == 200

        # Service was called with the CANONICAL user_id resolved from the token's
        # sub — NOT the raw sub and NOT the attacker-supplied param.
        mock_service.query_by_user.assert_called_once()
        call_kwargs = mock_service.query_by_user.call_args[1]
        assert call_kwargs["user_id"] == CANONICAL_USER_ID
        # neither the attacker param nor the raw sub is forwarded
        assert "attacker-id" not in str(call_kwargs)
        assert call_kwargs["user_id"] != regular_user.user_id

    def test_falls_back_to_token_sub_when_no_users_row(self, app, mock_service, mock_access, regular_user):
        """If no users row maps the Cognito sub to a canonical id (unprovisioned
        identity), fall back to the raw token user_id so behavior is unchanged."""

        async def override_current_user():
            return regular_user

        unresolved_db = MagicMock()
        unresolved_db.scalar = AsyncMock(return_value=None)  # no matching users row

        async def override_db():
            return unresolved_db

        app.dependency_overrides[get_current_user] = override_current_user
        app.dependency_overrides[get_activity_service] = lambda: mock_service
        app.dependency_overrides[get_access_control] = lambda: mock_access
        app.dependency_overrides[get_db] = override_db
        client = TestClient(app)

        resp = client.get("/me/agent-invocations")
        assert resp.status_code == 200
        call_kwargs = mock_service.query_by_user.call_args[1]
        assert call_kwargs["user_id"] == regular_user.user_id

    def test_missing_gsi_returns_empty_200(self, client, mock_service):
        """When service returns empty (GSI missing), endpoint returns 200 with empty items."""
        mock_service.query_by_user.return_value = InvocationListResponse(items=[], count=0, last_key=None)
        resp = client.get("/me/agent-invocations")
        assert resp.status_code == 200
        data = resp.json()
        assert data["items"] == []
        assert data["count"] == 0
        assert data["last_key"] is None

    def test_short_page_preserves_last_key(self, client, mock_service):
        """Filtered query with 0 items but non-null last_key is returned correctly."""
        mock_service.query_by_user.return_value = InvocationListResponse(items=[], count=0, last_key="eyJwayI6ICJpbnYtMDUwIn0=")
        resp = client.get("/me/agent-invocations?status=completed")
        assert resp.status_code == 200
        data = resp.json()
        assert data["items"] == []
        assert data["count"] == 0
        assert data["last_key"] is not None

    def test_bad_cursor_returns_400(self, client, mock_service):
        """Malformed cursor triggers ValueError → 400."""
        mock_service.query_by_user.side_effect = ValueError("Invalid cursor: ...")
        resp = client.get("/me/agent-invocations?last_key=garbage!")
        assert resp.status_code == 400
        assert "Invalid cursor" in resp.json()["detail"]

    def test_returns_items_correctly(self, client, mock_service):
        """Valid response with items is serialized correctly."""
        mock_service.query_by_user.return_value = InvocationListResponse(
            items=[
                InvocationItem(
                    invocation_id="inv-001",
                    invoked_at="2026-06-09T14:32:00Z",
                    channel="github",
                    status="in_progress",
                    topic="Deploy ADP",
                    persona="operations",
                    summary="operations — deploy run started",
                    source_url="https://github.com/aws-e/adp/issues/1320",
                    repo="aws-e/adp",
                    issue_number=1320,
                    correlation_id="corr-uuid",
                    run_id="agent-issue-1320-xyz",
                )
            ],
            count=1,
            last_key=None,
        )
        resp = client.get("/me/agent-invocations")
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 1
        item = data["items"][0]
        assert item["invocation_id"] == "inv-001"
        assert item["channel"] == "github"
        assert item["issue_number"] == 1320

    def test_returns_lineage_fields(self, client, mock_service):
        """Response includes Phase 6 lineage fields."""
        mock_service.query_by_user.return_value = InvocationListResponse(
            items=[
                InvocationItem(
                    invocation_id="inv-child",
                    invoked_at="2026-06-14T10:05:00Z",
                    channel="github",
                    status="in_progress",
                    topic="Child task",
                    trigger_kind="agent",
                    triggered_by_invocation_id="inv-parent",
                    triggered_by_topic="Parent task",
                    root_human_id="user-abc-123",
                    is_human_rooted=True,
                    correlation_id="chain-001",
                )
            ],
            count=1,
            last_key=None,
        )
        resp = client.get("/me/agent-invocations")
        assert resp.status_code == 200
        item = resp.json()["items"][0]
        assert item["trigger_kind"] == "agent"
        assert item["triggered_by_invocation_id"] == "inv-parent"
        assert item["triggered_by_topic"] == "Parent task"
        assert item["root_human_id"] == "user-abc-123"
        assert item["is_human_rooted"] is True

    def test_page_size_passed_to_service(self, client, mock_service):
        """page_size query param is forwarded to service."""
        client.get("/me/agent-invocations?page_size=50")
        call_kwargs = mock_service.query_by_user.call_args[1]
        assert call_kwargs["page_size"] == 50

    def test_filters_passed_to_service(self, client, mock_service):
        """status/channel/persona/since/until params are forwarded."""
        client.get("/me/agent-invocations?status=completed&channel=github&persona=developer&since=2026-06-01T00:00:00Z&until=2026-06-10T00:00:00Z")
        call_kwargs = mock_service.query_by_user.call_args[1]
        assert call_kwargs["status"] == "completed"
        assert call_kwargs["channel"] == "github"
        assert call_kwargs["persona"] == "developer"
        assert call_kwargs["since"] == "2026-06-01T00:00:00Z"
        assert call_kwargs["until"] == "2026-06-10T00:00:00Z"


class TestGetAdminInvocations:
    """/admin/agent-invocations endpoint tests."""

    def test_non_admin_gets_403(self, app, mock_service, regular_user):
        """Non-admin user (no USAGE_READ) gets 403."""
        mock_access_deny = MagicMock(spec=AccessControl)
        mock_access_deny.check_permission = AsyncMock(
            side_effect=AccessDeniedError(
                message="Permission 'usage:read' is required",
                required_permission="usage:read",
                user_role="dept_admin",
            )
        )

        async def override_current_user():
            return regular_user

        def override_service():
            return mock_service

        async def override_access():
            return mock_access_deny

        app.dependency_overrides[get_current_user] = override_current_user
        app.dependency_overrides[get_activity_service] = override_service
        app.dependency_overrides[get_access_control] = override_access

        # Need to register the gateway error handler for AccessDeniedError
        from fastapi import Request
        from fastapi.responses import JSONResponse

        from src.shared.exceptions import BedrockGatewayError

        @app.exception_handler(BedrockGatewayError)
        async def gateway_error_handler(request: Request, exc: BedrockGatewayError):
            return JSONResponse(
                status_code=exc.status_code,
                content={"error": exc.error, "message": exc.message},
            )

        test_client = TestClient(app)
        resp = test_client.get("/admin/agent-invocations")
        assert resp.status_code == 403

    def test_org_admin_scoped_to_own_tenant(self, app, mock_service, mock_access, org_admin_user):
        """Org admin queries their own org_id as tenant_id."""

        async def override_current_user():
            return org_admin_user

        def override_service():
            return mock_service

        async def override_access():
            return mock_access

        app.dependency_overrides[get_current_user] = override_current_user
        app.dependency_overrides[get_activity_service] = override_service
        app.dependency_overrides[get_access_control] = override_access

        test_client = TestClient(app)
        resp = test_client.get("/admin/agent-invocations")
        assert resp.status_code == 200

        # Service called with org admin's own org_id
        mock_service.query_by_tenant.assert_called_once()
        call_kwargs = mock_service.query_by_tenant.call_args[1]
        assert call_kwargs["tenant_id"] == org_admin_user.org_id

    def test_platform_admin_can_pass_tenant_id(self, admin_client, mock_service, admin_user):
        """Platform admin may specify an explicit tenant_id."""
        resp = admin_client.get("/admin/agent-invocations?tenant_id=org-other-999")
        assert resp.status_code == 200

        call_kwargs = mock_service.query_by_tenant.call_args[1]
        assert call_kwargs["tenant_id"] == "org-other-999"

    def test_admin_user_id_filter(self, admin_client, mock_service):
        """Admin can filter by user_id within the tenant."""
        resp = admin_client.get("/admin/agent-invocations?user_id=specific-user")
        assert resp.status_code == 200

        call_kwargs = mock_service.query_by_tenant.call_args[1]
        assert call_kwargs["user_id"] == "specific-user"

    def test_bad_cursor_returns_400(self, admin_client, mock_service):
        """Malformed cursor → 400."""
        mock_service.query_by_tenant.side_effect = ValueError("Invalid cursor: ...")
        resp = admin_client.get("/admin/agent-invocations?last_key=bad!")
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Phase 6 chain endpoint tests (#1461)
# ---------------------------------------------------------------------------


class TestGetMyInvocationChain:
    """/me/agent-invocations/chain/{correlation_id} endpoint tests."""

    def test_returns_chain_for_user(self, client, mock_service, regular_user):
        """Chain endpoint calls service with user_id from token."""
        mock_service.get_chain.return_value = InvocationChainResponse(
            correlation_id="chain-001",
            root_human_id=regular_user.user_id,
            is_human_rooted=True,
            items=[
                InvocationChainItem(
                    invocation_id="inv-A",
                    invoked_at="2026-06-14T10:00:00Z",
                    status="complete",
                    topic="Root task",
                    children=[
                        InvocationChainItem(
                            invocation_id="inv-B",
                            invoked_at="2026-06-14T10:05:00Z",
                            status="in_progress",
                            topic="Child task",
                            parent_invocation_id="inv-A",
                            children=[],
                        )
                    ],
                )
            ],
            total_count=2,
            depth_capped=False,
        )

        resp = client.get("/me/agent-invocations/chain/chain-001")
        assert resp.status_code == 200

        # Service called with the canonical user_id resolved from the token sub
        mock_service.get_chain.assert_called_once_with(
            correlation_id="chain-001",
            user_id=CANONICAL_USER_ID,
        )

        data = resp.json()
        assert data["correlation_id"] == "chain-001"
        assert data["total_count"] == 2
        assert data["is_human_rooted"] is True
        assert len(data["items"]) == 1
        assert data["items"][0]["invocation_id"] == "inv-A"
        assert len(data["items"][0]["children"]) == 1
        assert data["items"][0]["children"][0]["invocation_id"] == "inv-B"

    def test_empty_chain_returns_200(self, client, mock_service):
        """Empty chain returns 200 with empty items (not 404)."""
        mock_service.get_chain.return_value = InvocationChainResponse(
            correlation_id="chain-nonexistent",
            items=[],
            total_count=0,
            depth_capped=False,
        )

        resp = client.get("/me/agent-invocations/chain/chain-nonexistent")
        assert resp.status_code == 200
        data = resp.json()
        assert data["items"] == []
        assert data["total_count"] == 0

    def test_depth_capped_flag(self, client, mock_service):
        """Depth-capped chain sets flag in response."""
        mock_service.get_chain.return_value = InvocationChainResponse(
            correlation_id="chain-deep",
            items=[InvocationChainItem(invocation_id="inv-1", invoked_at="2026-06-14T10:00:00Z", children=[])],
            total_count=50,
            depth_capped=True,
        )

        resp = client.get("/me/agent-invocations/chain/chain-deep")
        assert resp.status_code == 200
        data = resp.json()
        assert data["depth_capped"] is True
        assert data["total_count"] == 50


class TestGetAdminInvocationChain:
    """/admin/agent-invocations/chain/{correlation_id} endpoint tests."""

    def test_admin_chain_uses_tenant_scope(self, admin_client, mock_service, admin_user):
        """Admin chain endpoint uses tenant_id scope."""
        resp = admin_client.get("/admin/agent-invocations/chain/chain-001?tenant_id=org-xyz")
        assert resp.status_code == 200

        mock_service.get_chain.assert_called_once_with(
            correlation_id="chain-001",
            tenant_id="org-xyz",
        )

    def test_org_admin_chain_scoped_to_own_org(self, app, mock_service, mock_access, mock_db, org_admin_user):
        """Org admin chain is scoped to their own org_id."""

        async def override_current_user():
            return org_admin_user

        def override_service():
            return mock_service

        async def override_access():
            return mock_access

        async def override_db():
            return mock_db

        app.dependency_overrides[get_current_user] = override_current_user
        app.dependency_overrides[get_activity_service] = override_service
        app.dependency_overrides[get_access_control] = override_access
        app.dependency_overrides[get_db] = override_db

        test_client = TestClient(app)
        resp = test_client.get("/admin/agent-invocations/chain/chain-001")
        assert resp.status_code == 200

        mock_service.get_chain.assert_called_once_with(
            correlation_id="chain-001",
            tenant_id=org_admin_user.org_id,
        )


# ---------------------------------------------------------------------------
# Issue #1653: Detail endpoint tests
# ---------------------------------------------------------------------------


class TestGetMyInvocationDetail:
    """/me/agent-invocations/{invocation_id} endpoint tests."""

    def test_returns_item_when_found(self, client, mock_service):
        """Detail endpoint returns 200 with item when found."""
        mock_service.get_invocation = MagicMock(
            return_value=InvocationItem(
                invocation_id="inv-detail",
                invoked_at="2026-06-20T10:00:00Z",
                channel="github",
                status="complete",
                topic="Deploy service",
                completed_at="2026-06-20T10:02:14Z",
            )
        )

        resp = client.get("/me/agent-invocations/inv-detail")
        assert resp.status_code == 200
        data = resp.json()
        assert data["invocation_id"] == "inv-detail"
        assert data["topic"] == "Deploy service"
        assert data["completed_at"] == "2026-06-20T10:02:14Z"

    def test_returns_404_when_not_found(self, client, mock_service):
        """Detail endpoint returns 404 when invocation doesn't exist for user."""
        mock_service.get_invocation = MagicMock(return_value=None)

        resp = client.get("/me/agent-invocations/inv-nonexistent")
        assert resp.status_code == 404

    def test_scoped_to_canonical_user_id(self, client, mock_service):
        """Detail uses canonical user_id from token (not raw param)."""
        mock_service.get_invocation = MagicMock(return_value=None)

        client.get("/me/agent-invocations/inv-xyz")
        mock_service.get_invocation.assert_called_once_with("inv-xyz", user_id=CANONICAL_USER_ID)

    def test_does_not_match_chain_route(self, client, mock_service):
        """Requesting /me/agent-invocations/chain/abc hits chain route, not detail."""
        # Chain route should be matched, not detail with invocation_id="chain"
        mock_service.get_chain.return_value = InvocationChainResponse(
            correlation_id="abc",
            items=[],
            total_count=0,
            depth_capped=False,
        )

        resp = client.get("/me/agent-invocations/chain/abc")
        assert resp.status_code == 200
        # Should have called get_chain, NOT get_invocation
        mock_service.get_chain.assert_called()


class TestGetAdminInvocationDetail:
    """/admin/agent-invocations/{invocation_id} endpoint tests."""

    def test_returns_item_for_admin(self, admin_client, mock_service):
        """Admin detail endpoint returns item for specified tenant."""
        mock_service.get_invocation = MagicMock(
            return_value=InvocationItem(
                invocation_id="inv-admin-detail",
                invoked_at="2026-06-20T10:00:00Z",
                channel="github",
                status="failed",
                error_message="timeout reached",
            )
        )

        resp = admin_client.get("/admin/agent-invocations/inv-admin-detail?tenant_id=org-xyz")
        assert resp.status_code == 200
        data = resp.json()
        assert data["invocation_id"] == "inv-admin-detail"
        assert data["error_message"] == "timeout reached"

    def test_returns_404_when_not_found(self, admin_client, mock_service):
        """Admin detail returns 404 for non-existent invocation."""
        mock_service.get_invocation = MagicMock(return_value=None)

        resp = admin_client.get("/admin/agent-invocations/inv-missing")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Issue #1658: include_non_triggering query param tests
# ---------------------------------------------------------------------------


class TestIncludeNonTriggeringParam:
    """Tests for the include_non_triggering query param on list endpoints."""

    def test_me_default_excludes_non_triggering(self, client, mock_service):
        """/me default (no param) passes include_non_triggering=False to service."""
        client.get("/me/agent-invocations")
        call_kwargs = mock_service.query_by_user.call_args[1]
        assert call_kwargs["include_non_triggering"] is False

    def test_me_include_non_triggering_true(self, client, mock_service):
        """/me with include_non_triggering=true passes True to service."""
        client.get("/me/agent-invocations?include_non_triggering=true")
        call_kwargs = mock_service.query_by_user.call_args[1]
        assert call_kwargs["include_non_triggering"] is True

    def test_me_include_non_triggering_false_explicit(self, client, mock_service):
        """/me with include_non_triggering=false passes False to service."""
        client.get("/me/agent-invocations?include_non_triggering=false")
        call_kwargs = mock_service.query_by_user.call_args[1]
        assert call_kwargs["include_non_triggering"] is False

    def test_me_explicit_status_with_default_toggle(self, client, mock_service):
        """Explicit status=no_op is passed through even with default toggle off."""
        client.get("/me/agent-invocations?status=no_op")
        call_kwargs = mock_service.query_by_user.call_args[1]
        assert call_kwargs["status"] == "no_op"
        # The toggle is still False — backend handles the precedence logic
        assert call_kwargs["include_non_triggering"] is False

    def test_admin_default_excludes_non_triggering(self, admin_client, mock_service):
        """/admin default (no param) passes include_non_triggering=False."""
        admin_client.get("/admin/agent-invocations")
        call_kwargs = mock_service.query_by_tenant.call_args[1]
        assert call_kwargs["include_non_triggering"] is False

    def test_admin_include_non_triggering_true(self, admin_client, mock_service):
        """/admin with include_non_triggering=true passes True."""
        admin_client.get("/admin/agent-invocations?include_non_triggering=true")
        call_kwargs = mock_service.query_by_tenant.call_args[1]
        assert call_kwargs["include_non_triggering"] is True
