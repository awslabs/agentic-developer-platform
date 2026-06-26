"""Unit tests for github_app_service — resolve_installation_for_repo & verify_installation_ownership.

Issue #2086: GitHub accessibility helpers.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from src.knowledge.github_app_service import (
    resolve_installation_for_repo,
    verify_installation_ownership,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_global_creds():
    """Patch _get_global_app_credentials to return configured credentials."""
    with patch(
        "src.knowledge.github_app_service._get_global_app_credentials",
        return_value=(
            "3410773",
            "-----BEGIN RSA PRIVATE KEY-----\nfake\n-----END RSA PRIVATE KEY-----",
        ),
    ) as mock:
        yield mock


@pytest.fixture
def mock_jwt():
    """Patch _mint_app_jwt to return a predictable token."""
    with patch(
        "src.knowledge.github_app_service._mint_app_jwt",
        return_value="fake-jwt-token",
    ) as mock:
        yield mock


# ---------------------------------------------------------------------------
# resolve_installation_for_repo tests
# ---------------------------------------------------------------------------


class TestResolveInstallationForRepo:
    """Tests for resolve_installation_for_repo."""

    @pytest.mark.asyncio
    async def test_returns_installation_id_on_200(self, mock_global_creds, mock_jwt):
        """200 response → returns the installation_id from the response body."""
        mock_response = httpx.Response(
            200,
            json={"id": 98765, "app_id": 3410773, "target_type": "Organization"},
            request=httpx.Request("GET", "https://api.github.com/repos/acme/my-repo/installation"),
        )
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get = AsyncMock(return_value=mock_response)

        result = await resolve_installation_for_repo("acme", "my-repo", http_client=mock_client)

        assert result == 98765
        mock_client.get.assert_called_once_with(
            "/repos/acme/my-repo/installation",
            headers={"Authorization": "Bearer fake-jwt-token"},
        )

    @pytest.mark.asyncio
    async def test_returns_none_on_404(self, mock_global_creds, mock_jwt):
        """404 response (App not installed on repo) → returns None."""
        mock_response = httpx.Response(
            404,
            json={"message": "Not Found"},
            request=httpx.Request(
                "GET",
                "https://api.github.com/repos/acme/unknown-repo/installation",
            ),
        )
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get = AsyncMock(return_value=mock_response)

        result = await resolve_installation_for_repo("acme", "unknown-repo", http_client=mock_client)

        assert result is None

    @pytest.mark.asyncio
    async def test_raises_on_other_errors(self, mock_global_creds, mock_jwt):
        """Non-200/non-404 (e.g., 403) → raises HTTPStatusError."""
        mock_response = httpx.Response(
            403,
            json={"message": "Forbidden"},
            request=httpx.Request(
                "GET",
                "https://api.github.com/repos/acme/secret-repo/installation",
            ),
        )
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get = AsyncMock(return_value=mock_response)

        with pytest.raises(httpx.HTTPStatusError):
            await resolve_installation_for_repo("acme", "secret-repo", http_client=mock_client)

    @pytest.mark.asyncio
    async def test_raises_when_credentials_not_configured(self):
        """Missing global App credentials → raises ValueError."""
        with patch(
            "src.knowledge.github_app_service._get_global_app_credentials",
            return_value=("", ""),
        ):
            with pytest.raises(ValueError, match="Global GitHub App credentials not configured"):
                await resolve_installation_for_repo("acme", "my-repo")


# ---------------------------------------------------------------------------
# verify_installation_ownership tests
# ---------------------------------------------------------------------------


class TestVerifyInstallationOwnership:
    """Tests for verify_installation_ownership."""

    @pytest.mark.asyncio
    async def test_returns_true_for_matching_tenant(self):
        """Installation belonging to the caller's tenant → True."""
        fake_db = AsyncMock()
        # Use a MagicMock for the result so fetchone is synchronous
        fake_result = MagicMock()
        fake_result.fetchone.return_value = (1,)
        fake_db.execute.return_value = fake_result

        result = await verify_installation_ownership("acme-corp", 98765, db=fake_db)

        assert result is True
        # Verify the query uses metadata->>'installation_id'
        call_args = fake_db.execute.call_args
        query_text = str(call_args[0][0].text)
        assert "metadata->>'installation_id'" in query_text
        assert call_args[0][1] == {
            "tenant_id": "acme-corp",
            "installation_id": "98765",
        }

    @pytest.mark.asyncio
    async def test_returns_false_for_different_tenant(self):
        """Installation belonging to ANOTHER tenant → False (cross-tenant guard)."""
        fake_db = AsyncMock()
        fake_result = MagicMock()
        fake_result.fetchone.return_value = None
        fake_db.execute.return_value = fake_result

        result = await verify_installation_ownership("evil-corp", 98765, db=fake_db)

        assert result is False

    @pytest.mark.asyncio
    async def test_returns_true_for_personal_install(self):
        """Personal install (only in channel_tenant_map, not in organizations.github_installation_ids) → True.

        This proves I1 handling: personal installs are registered via
        channel_tenant_map and this function correctly resolves them.
        """
        fake_db = AsyncMock()
        fake_result = MagicMock()
        # Personal install row exists in channel_tenant_map
        fake_result.fetchone.return_value = (1,)
        fake_db.execute.return_value = fake_result

        result = await verify_installation_ownership("personal-user-tenant", 55555, db=fake_db)

        assert result is True
        # Verify the query does NOT reference organizations table
        call_args = fake_db.execute.call_args
        query_text = str(call_args[0][0].text)
        assert "organizations" not in query_text.lower()
        assert "channel_tenant_map" in query_text

    @pytest.mark.asyncio
    async def test_uses_string_cast_for_installation_id(self):
        """Confirms installation_id is passed as a string for JSON text comparison."""
        fake_db = AsyncMock()
        fake_result = MagicMock()
        fake_result.fetchone.return_value = None
        fake_db.execute.return_value = fake_result

        await verify_installation_ownership("acme-corp", 12345, db=fake_db)

        call_args = fake_db.execute.call_args
        params = call_args[0][1]
        # installation_id must be passed as string for metadata->>'...' comparison
        assert params["installation_id"] == "12345"
        assert isinstance(params["installation_id"], str)
