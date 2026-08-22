"""Authorization regression tests for the agent-registry list + policy preview.

Issue #3988 (finding f-a757e062): ``AccessControl.is_platform_admin`` is
``async``, but two call sites in ``src/admin/routes.py`` invoked it WITHOUT
``await``. A coroutine object is always truthy, so ``not <coroutine>`` was
always ``False`` and both scope checks were dead code:

* ``GET /admin/registry/agents`` — a non-platform caller that omitted ``org_id``
  was never pinned to their own org, so ``org_id=None`` reached
  ``AgentRegistryService.list_agents`` and fell through to an unfiltered
  ``dynamodb.scan`` returning EVERY tenant's agents.
* ``POST /admin/policies/preview`` — an org admin could preview IAM policies for
  any other org's hierarchy.

These tests pin the fixed behaviour so the ``await`` cannot be dropped again.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from src.admin.access_control import AccessControl
from src.admin.agent_registry_schemas import AgentRegistryListResponse
from src.admin.agent_registry_service import AgentRegistryService
from src.admin.routes import (
    get_access_control,
    get_agent_registry_service,
    get_current_user,
    router,
)
from src.shared.exceptions import BedrockGatewayError
from src.shared.schemas.auth import TokenContext


@pytest.fixture
def app():
    """Test app with the admin router and app.py's error handler."""
    app = FastAPI()
    app.include_router(router)

    @app.exception_handler(BedrockGatewayError)
    async def gateway_error_handler(request: Request, exc: BedrockGatewayError):
        content = {"error": exc.error, "message": exc.message}
        if exc.details:
            content["details"] = exc.details
        return JSONResponse(status_code=exc.status_code, content=content)

    return app


@pytest.fixture
def mock_registry_service():
    """Registry service returning an empty page for any list call."""
    service = MagicMock(spec=AgentRegistryService)
    service.list_agents = AsyncMock(return_value=AgentRegistryListResponse(items=[], count=0, last_key=None))
    return service


def _token(*, org_id: str, is_admin: bool) -> TokenContext:
    return TokenContext(
        user_id=f"user-{org_id or 'noorg'}",
        org_id=org_id,
        team_id="team-a",
        department_id="dept-a",
        account_type="user",
        is_admin=is_admin,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )


def _client(app, *, user: TokenContext, is_platform_admin: bool, registry_service=None) -> TestClient:
    """Wire dependency overrides for a given caller identity."""
    access = MagicMock(spec=AccessControl)
    access.check_permission = AsyncMock(return_value=True)
    # The real method is async — mock it as async so a missing `await` in the
    # route under test produces a coroutine, exactly like production.
    access.is_platform_admin = AsyncMock(return_value=is_platform_admin)

    app.dependency_overrides[get_access_control] = lambda: access
    app.dependency_overrides[get_current_user] = lambda: user
    if registry_service is not None:
        app.dependency_overrides[get_agent_registry_service] = lambda: registry_service

    return TestClient(app)


class TestRegistryListOrgScope:
    """GET /admin/registry/agents org-scope pinning."""

    def test_non_platform_caller_without_org_id_is_pinned_to_own_org(self, app, mock_registry_service):
        """The core finding: no org_id query param must NOT mean 'all tenants'."""
        client = _client(
            app,
            user=_token(org_id="org-caller", is_admin=False),
            is_platform_admin=False,
            registry_service=mock_registry_service,
        )

        response = client.get("/admin/registry/agents")

        assert response.status_code == 200
        kwargs = mock_registry_service.list_agents.await_args.kwargs
        assert kwargs["org_id"] == "org-caller"
        # Never permitted to reach the cross-tenant scan.
        assert kwargs["allow_scan"] is False

    def test_non_platform_caller_with_empty_org_id_is_denied(self, app, mock_registry_service):
        """N2: TokenContext.org_id is `claims.org_id or ""`.

        An empty string is falsy, so pinning alone would still leave org_id
        falsy and reach _scan_all. A caller with no org membership must get 403.
        """
        client = _client(
            app,
            user=_token(org_id="", is_admin=False),
            is_platform_admin=False,
            registry_service=mock_registry_service,
        )

        response = client.get("/admin/registry/agents")

        assert response.status_code == 403
        mock_registry_service.list_agents.assert_not_awaited()

    def test_platform_admin_without_org_id_may_scan_all_tenants(self, app, mock_registry_service):
        """Regression: the platform-admin cross-tenant listing must still work."""
        client = _client(
            app,
            user=_token(org_id="platform", is_admin=True),
            is_platform_admin=True,
            registry_service=mock_registry_service,
        )

        response = client.get("/admin/registry/agents")

        assert response.status_code == 200
        kwargs = mock_registry_service.list_agents.await_args.kwargs
        assert kwargs["org_id"] is None
        assert kwargs["allow_scan"] is True

    def test_explicit_org_id_is_permission_checked_and_not_scanned(self, app, mock_registry_service):
        """An explicit org_id goes through check_permission, never through scan."""
        user = _token(org_id="org-caller", is_admin=False)
        client = _client(app, user=user, is_platform_admin=False, registry_service=mock_registry_service)
        access = app.dependency_overrides[get_access_control]()

        response = client.get("/admin/registry/agents", params={"org_id": "org-caller"})

        assert response.status_code == 200
        access.check_permission.assert_awaited_once()
        kwargs = mock_registry_service.list_agents.await_args.kwargs
        assert kwargs["org_id"] == "org-caller"
        assert kwargs["allow_scan"] is False


class TestPolicyPreviewOrgScope:
    """POST /admin/policies/preview org-hierarchy scope check."""

    _REQUEST = {
        "level": "team",
        "hierarchy": {"platform": "bedrockgw", "org": "org-victim", "team": "team-x"},
        "agent_type": "app-dev",
        "region": "us-east-1",
        "account_id": "123456789012",
        "api_gateway_arn": "arn:aws:execute-api:us-east-1:123456789012:abc123/*",
    }

    def test_org_admin_cannot_preview_another_orgs_hierarchy(self, app):
        """The un-awaited call made this whole branch unreachable."""
        client = _client(app, user=_token(org_id="org-caller", is_admin=False), is_platform_admin=False)

        response = client.post("/admin/policies/preview", json=self._REQUEST)

        assert response.status_code == 403

    def test_org_admin_can_preview_own_hierarchy(self, app):
        """Regression: the legitimate same-org preview still succeeds."""
        client = _client(app, user=_token(org_id="org-victim", is_admin=False), is_platform_admin=False)

        response = client.post("/admin/policies/preview", json=self._REQUEST)

        assert response.status_code == 200

    def test_platform_admin_can_preview_any_hierarchy(self, app):
        """Regression: platform admins keep cross-org preview."""
        client = _client(app, user=_token(org_id="platform", is_admin=True), is_platform_admin=True)

        response = client.post("/admin/policies/preview", json=self._REQUEST)

        assert response.status_code == 200
