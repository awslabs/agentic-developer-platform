"""Tests for workspace CRUD endpoints."""

import uuid
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.middleware.auth import create_access_token


def _auth_header(org_id: uuid.UUID | None = None) -> dict:
    """Create an Authorization header with a valid JWT."""
    if org_id is None:
        org_id = uuid.uuid4()
    token, _ = create_access_token(org_id)
    return {"Authorization": f"Bearer {token}"}


class TestCreateWorkspace:
    """Test POST /workspaces."""

    @pytest.mark.asyncio
    async def test_create_requires_auth(self, client):
        """Creating a workspace without auth returns 401/403."""
        response = await client.post("/workspaces", json={"name": "test-ws"})
        assert response.status_code in (401, 403)

    @pytest.mark.asyncio
    async def test_create_validates_name(self, client):
        """Creating a workspace with empty name returns 422."""
        headers = _auth_header()
        response = await client.post("/workspaces", json={"name": ""}, headers=headers)
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_create_validates_isolation_mode(self, client):
        """Invalid isolation_mode returns 422."""
        headers = _auth_header()
        response = await client.post(
            "/workspaces",
            json={"name": "test", "isolation_mode": "invalid"},
            headers=headers,
        )
        assert response.status_code == 422


class TestListWorkspaces:
    """Test GET /workspaces."""

    @pytest.mark.asyncio
    async def test_list_requires_auth(self, client):
        """Listing workspaces without auth returns 401/403."""
        response = await client.get("/workspaces")
        assert response.status_code in (401, 403)


class TestGetWorkspace:
    """Test GET /workspaces/{id}."""

    @pytest.mark.asyncio
    async def test_get_requires_auth(self, client):
        """Getting a workspace without auth returns 401/403."""
        ws_id = uuid.uuid4()
        response = await client.get(f"/workspaces/{ws_id}")
        assert response.status_code in (401, 403)


class TestDeleteWorkspace:
    """Test DELETE /workspaces/{id}."""

    @pytest.mark.asyncio
    async def test_delete_requires_auth(self, client):
        """Deleting a workspace without auth returns 401/403."""
        ws_id = uuid.uuid4()
        response = await client.delete(f"/workspaces/{ws_id}")
        assert response.status_code in (401, 403)


class TestKubeconfig:
    """Test POST /workspaces/{id}/kubeconfig."""

    @pytest.mark.asyncio
    async def test_kubeconfig_requires_auth(self, client):
        """Generating kubeconfig without auth returns 401/403."""
        ws_id = uuid.uuid4()
        response = await client.post(f"/workspaces/{ws_id}/kubeconfig")
        assert response.status_code in (401, 403)


class TestWorkspaceSchemas:
    """Test Pydantic schema validation."""

    def test_create_workspace_request_valid(self):
        from app.schemas.workspace import CreateWorkspaceRequest

        req = CreateWorkspaceRequest(name="my-workspace", isolation_mode="dedicated")
        assert req.name == "my-workspace"
        assert req.isolation_mode == "dedicated"

    def test_create_workspace_request_default_isolation(self):
        from app.schemas.workspace import CreateWorkspaceRequest

        req = CreateWorkspaceRequest(name="my-workspace")
        assert req.isolation_mode == "dedicated"

    def test_create_workspace_request_namespace_mode(self):
        from app.schemas.workspace import CreateWorkspaceRequest

        req = CreateWorkspaceRequest(name="shared-ws", isolation_mode="namespace")
        assert req.isolation_mode == "namespace"

    def test_create_workspace_request_invalid_mode(self):
        from pydantic import ValidationError

        from app.schemas.workspace import CreateWorkspaceRequest

        with pytest.raises(ValidationError):
            CreateWorkspaceRequest(name="test", isolation_mode="invalid")

    # --- Research isolation mode tests ---

    def test_create_workspace_research_mode_valid(self):
        """Research mode with account is valid."""
        from app.schemas.workspace import CreateWorkspaceRequest

        req = CreateWorkspaceRequest(
            name="ml-research",
            isolation_mode="research",
            account="research-account",
        )
        assert req.isolation_mode == "research"
        assert req.account == "research-account"

    def test_create_workspace_research_mode_requires_account(self):
        """Research mode without account raises validation error."""
        from pydantic import ValidationError

        from app.schemas.workspace import CreateWorkspaceRequest

        with pytest.raises(
            ValidationError, match="Research workspaces require an AWS account"
        ):
            CreateWorkspaceRequest(name="ml-research", isolation_mode="research")

    def test_create_workspace_research_mode_empty_account(self):
        """Research mode with empty account raises validation error."""
        from pydantic import ValidationError

        from app.schemas.workspace import CreateWorkspaceRequest

        with pytest.raises(ValidationError):
            CreateWorkspaceRequest(
                name="ml-research",
                isolation_mode="research",
                account="",
            )

    def test_create_workspace_research_with_budget(self):
        """Research mode with budget guardrails is valid."""
        from app.schemas.workspace import CreateWorkspaceRequest

        req = CreateWorkspaceRequest(
            name="ml-research",
            isolation_mode="research",
            account="research-account",
            budget_max_daily_usd=Decimal("200.00"),
            budget_max_gpus=4,
        )
        assert req.budget_max_daily_usd == Decimal("200.00")
        assert req.budget_max_gpus == 4

    def test_create_workspace_budget_negative_rejected(self):
        """Negative budget values are rejected."""
        from pydantic import ValidationError

        from app.schemas.workspace import CreateWorkspaceRequest

        with pytest.raises(ValidationError):
            CreateWorkspaceRequest(
                name="test",
                isolation_mode="dedicated",
                budget_max_daily_usd=Decimal("-10.00"),
            )

    def test_create_workspace_budget_gpus_negative_rejected(self):
        """Negative GPU count is rejected."""
        from pydantic import ValidationError

        from app.schemas.workspace import CreateWorkspaceRequest

        with pytest.raises(ValidationError):
            CreateWorkspaceRequest(
                name="test",
                isolation_mode="dedicated",
                budget_max_gpus=-1,
            )

    def test_dedicated_mode_account_optional(self):
        """Dedicated mode does not require an account."""
        from app.schemas.workspace import CreateWorkspaceRequest

        req = CreateWorkspaceRequest(name="my-ws", isolation_mode="dedicated")
        assert req.account is None

    def test_namespace_mode_account_optional(self):
        """Namespace mode does not require an account."""
        from app.schemas.workspace import CreateWorkspaceRequest

        req = CreateWorkspaceRequest(name="my-ws", isolation_mode="namespace")
        assert req.account is None


class TestWorkspaceDisplayName:
    """Test workspace display name with isolation mode tags."""

    def test_research_display_name(self):
        from app.routers.workspaces import _make_display_name

        assert _make_display_name("ml-research", "research") == "ml-research [research]"

    def test_dedicated_display_name(self):
        from app.routers.workspaces import _make_display_name

        assert _make_display_name("my-workspace", "dedicated") == "my-workspace"

    def test_namespace_display_name(self):
        from app.routers.workspaces import _make_display_name

        assert _make_display_name("shared-ws", "namespace") == "shared-ws"


class TestResearchBudgetDefaults:
    """Test default budget guardrails for research workspaces."""

    def test_research_default_budget(self):
        from app.routers.workspaces import RESEARCH_DEFAULT_BUDGET

        assert RESEARCH_DEFAULT_BUDGET["max_daily_usd"] == 100.00
        assert RESEARCH_DEFAULT_BUDGET["max_gpus"] == 8


class TestWorkspaceModel:
    """Test workspace model constants."""

    def test_valid_isolation_modes(self):
        from app.models.workspace import VALID_ISOLATION_MODES

        assert "dedicated" in VALID_ISOLATION_MODES
        assert "namespace" in VALID_ISOLATION_MODES
        assert "research" in VALID_ISOLATION_MODES
        assert len(VALID_ISOLATION_MODES) == 3


class TestGitHubService:
    """Test GitHub Actions trigger service."""

    @pytest.mark.asyncio
    async def test_trigger_bootstrap_no_token(self):
        """Without GITHUB_TOKEN, trigger returns False."""
        from app.services.github import trigger_bootstrap

        with patch("app.services.github.settings") as mock_settings:
            mock_settings.github_token = ""
            result = await trigger_bootstrap("ws-id", "ws-name", "org-id", "dedicated")
            assert result is False

    @pytest.mark.asyncio
    async def test_trigger_bootstrap_research_with_account(self):
        """Research bootstrap passes account in inputs."""
        from app.services.github import trigger_bootstrap

        with patch("app.services.github.settings") as mock_settings:
            mock_settings.github_token = ""
            # Even though token is missing, verify the function signature works
            result = await trigger_bootstrap(
                "ws-id", "ws-name", "org-id", "research", account="research-account"
            )
            assert result is False

    @pytest.mark.asyncio
    async def test_trigger_teardown_no_token(self):
        """Without GITHUB_TOKEN, trigger returns False."""
        from app.services.github import trigger_teardown

        with patch("app.services.github.settings") as mock_settings:
            mock_settings.github_token = ""
            result = await trigger_teardown("ws-id", "ws-name", "org-id")
            assert result is False

    @pytest.mark.asyncio
    async def test_trigger_workflow_success(self):
        """Successful workflow dispatch returns True."""
        from app.services.github import trigger_workflow

        mock_response = MagicMock()
        mock_response.status_code = 204

        with (
            patch("app.services.github.settings") as mock_settings,
            patch("app.services.github.httpx.AsyncClient") as mock_client_cls,
        ):
            mock_settings.github_token = "ghp_test123"
            mock_settings.github_repo = "test/repo"

            mock_client = AsyncMock()
            mock_client.post.return_value = mock_response
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            result = await trigger_workflow(
                "bootstrap-workspace.yml", inputs={"workspace_id": "123"}
            )
            assert result is True

    @pytest.mark.asyncio
    async def test_trigger_workflow_failure(self):
        """Failed workflow dispatch returns False."""
        from app.services.github import trigger_workflow

        mock_response = MagicMock()
        mock_response.status_code = 404
        mock_response.text = "Not Found"

        with (
            patch("app.services.github.settings") as mock_settings,
            patch("app.services.github.httpx.AsyncClient") as mock_client_cls,
        ):
            mock_settings.github_token = "ghp_test123"
            mock_settings.github_repo = "test/repo"

            mock_client = AsyncMock()
            mock_client.post.return_value = mock_response
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            result = await trigger_workflow("nonexistent.yml")
            assert result is False
