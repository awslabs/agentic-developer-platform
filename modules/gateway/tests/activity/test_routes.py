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


@pytest.fixture
def app():
    """Create a test FastAPI app with activity routes."""
    app = FastAPI()
    app.include_router(router)
    return app


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
def client(app, mock_service, mock_access, regular_user):
    """Create a test client with regular user auth."""

    async def override_current_user():
        return regular_user

    def override_service():
        return mock_service

    async def override_access():
        return mock_access

    app.dependency_overrides[get_current_user] = override_current_user
    app.dependency_overrides[get_activity_service] = override_service
    app.dependency_overrides[get_access_control] = override_access
    return TestClient(app)


@pytest.fixture
def admin_client(app, mock_service, mock_access, admin_user):
    """Create a test client with platform admin auth."""

    async def override_current_user():
        return admin_user

    def override_service():
        return mock_service

    async def override_access():
        return mock_access

    app.dependency_overrides[get_current_user] = override_current_user
    app.dependency_overrides[get_activity_service] = override_service
    app.dependency_overrides[get_access_control] = override_access
    return TestClient(app)


class TestGetMyInvocations:
    """/me/agent-invocations endpoint tests."""

    def test_uses_token_user_id_not_param(self, client, mock_service, regular_user):
        """The endpoint uses user_id from token, ignoring any user_id query param."""
        resp = client.get("/me/agent-invocations?user_id=attacker-id")
        assert resp.status_code == 200

        # Service was called with the TOKEN's user_id, not the param
        mock_service.query_by_user.assert_called_once()
        call_kwargs = mock_service.query_by_user.call_args[1]
        assert call_kwargs["user_id"] == regular_user.user_id
        # user_id param should NOT be forwarded
        assert "attacker-id" not in str(call_kwargs)

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

        # Service called with user_id from token
        mock_service.get_chain.assert_called_once_with(
            correlation_id="chain-001",
            user_id=regular_user.user_id,
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

    def test_org_admin_chain_scoped_to_own_org(self, app, mock_service, mock_access, org_admin_user):
        """Org admin chain is scoped to their own org_id."""

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
        resp = test_client.get("/admin/agent-invocations/chain/chain-001")
        assert resp.status_code == 200

        mock_service.get_chain.assert_called_once_with(
            correlation_id="chain-001",
            tenant_id=org_admin_user.org_id,
        )
