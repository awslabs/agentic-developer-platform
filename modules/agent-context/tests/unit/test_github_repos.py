"""Unit tests for the GitHub repo picker API.

Issue #1793 (Story D of E10 #1736).

Tests cover:
- GET /api/agent-context/github/accessible-repos: happy path, pagination, search
- No installation found (404)
- Credentials missing (404)
- GitHub API failure (502)
- App token mint + repo listing (mocked GitHub)

Uses httpx AsyncClient + FastAPI TestClient with mocked dependencies.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from agent_context.api.github_repos import (
    get_current_user_from_state,
    get_repos_db,
    router,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@dataclass
class FakeTokenContext:
    """Minimal TokenContext stub for tests."""

    user_id: str = "user-alice"
    org_id: str = "acme-corp"
    is_admin: bool = False


class FakeResult:
    """Simulates SQLAlchemy result from execute()."""

    def __init__(self, rows: list[Any] | None = None):
        self._rows = rows or []

    def fetchone(self) -> Any | None:
        return self._rows[0] if self._rows else None


class FakeDB:
    """Fake async DB session."""

    def __init__(self, rows: list[Any] | None = None):
        self._rows = rows or []

    async def execute(self, stmt: Any, params: Any = None) -> FakeResult:
        return FakeResult(self._rows)


def _build_app(
    *,
    user: FakeTokenContext | None = None,
    db_rows: list[Any] | None = None,
) -> FastAPI:
    """Build a FastAPI app with overridden dependencies."""
    app = FastAPI()
    app.include_router(router)

    ctx = user or FakeTokenContext()
    fake_db = FakeDB(db_rows)

    async def override_user():
        return ctx

    async def override_db():
        return fake_db

    app.dependency_overrides[get_current_user_from_state] = override_user
    app.dependency_overrides[get_repos_db] = override_db
    return app


# ---------------------------------------------------------------------------
# Tests — happy path
# ---------------------------------------------------------------------------


class TestAccessibleReposHappyPath:
    """GET /api/agent-context/github/accessible-repos — success cases."""

    @pytest.mark.asyncio
    async def test_returns_repos_from_installation(self):
        """Happy path: returns repos accessible to the tenant's GitHub App."""
        # DB row: ChannelTenantMap metadata with installation_id
        metadata = {"installation_id": 12345, "account_login": "acme"}
        db_rows = [(metadata,)]

        app = _build_app(db_rows=db_rows)

        mock_result = {
            "repos": [
                {"full_name": "acme/api", "private": True, "url": "https://github.com/acme/api"},
                {"full_name": "acme/docs", "private": False, "url": "https://github.com/acme/docs"},
            ],
            "total": 2,
            "page": 1,
            "has_more": False,
        }

        with (
            patch(
                "agent_context.api.github_repos.resolve_tenant_app_credentials",
                new=AsyncMock(
                    return_value=(
                        "app-123",
                        "-----BEGIN RSA PRIVATE KEY-----\nfake\n-----END RSA PRIVATE KEY-----",
                    )
                ),
            ),
            patch(
                "agent_context.api.github_repos.list_accessible_repos",
                new=AsyncMock(return_value=mock_result),
            ),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
            ) as client:
                resp = await client.get("/api/agent-context/github/accessible-repos")

        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 2
        assert body["page"] == 1
        assert body["has_more"] is False
        assert len(body["repos"]) == 2
        assert body["repos"][0]["full_name"] == "acme/api"
        assert body["repos"][0]["private"] is True
        assert body["repos"][1]["full_name"] == "acme/docs"

    @pytest.mark.asyncio
    async def test_passes_search_and_pagination_params(self):
        """Search and pagination params are forwarded to the service."""
        metadata = {"installation_id": 99}
        db_rows = [(metadata,)]
        app = _build_app(db_rows=db_rows)

        mock_result = {
            "repos": [
                {
                    "full_name": "acme/search-hit",
                    "private": False,
                    "url": "https://github.com/acme/search-hit",
                },
            ],
            "total": 1,
            "page": 2,
            "has_more": False,
        }

        mock_list = AsyncMock(return_value=mock_result)

        with (
            patch(
                "agent_context.api.github_repos.resolve_tenant_app_credentials",
                new=AsyncMock(return_value=("app-1", "key")),
            ),
            patch(
                "agent_context.api.github_repos.list_accessible_repos",
                new=mock_list,
            ),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
            ) as client:
                resp = await client.get(
                    "/api/agent-context/github/accessible-repos",
                    params={"q": "search", "page": 2, "page_size": 10},
                )

        assert resp.status_code == 200
        # Verify service was called with correct params
        mock_list.assert_awaited_once()
        call_kwargs = mock_list.call_args[1]
        assert call_kwargs["q"] == "search"
        assert call_kwargs["page"] == 2
        assert call_kwargs["page_size"] == 10


# ---------------------------------------------------------------------------
# Tests — error cases
# ---------------------------------------------------------------------------


class TestAccessibleReposErrors:
    """GET /api/agent-context/github/accessible-repos — error cases."""

    @pytest.mark.asyncio
    async def test_no_org_id_returns_409(self):
        """User with no org_id gets 409."""
        user = FakeTokenContext(org_id="")
        app = _build_app(user=user)

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            resp = await client.get("/api/agent-context/github/accessible-repos")

        assert resp.status_code == 409

    @pytest.mark.asyncio
    async def test_no_installation_returns_404(self):
        """Tenant with no GitHub App installed gets 404."""
        # Empty DB — no ChannelTenantMap row
        app = _build_app(db_rows=[])

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            resp = await client.get("/api/agent-context/github/accessible-repos")

        assert resp.status_code == 404
        assert "No GitHub App installed" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_credentials_missing_returns_404(self):
        """Credentials not in Secrets Manager → 404."""
        metadata = {"installation_id": 42}
        db_rows = [(metadata,)]
        app = _build_app(db_rows=db_rows)

        with patch(
            "agent_context.api.github_repos.resolve_tenant_app_credentials",
            new=AsyncMock(side_effect=ValueError("GitHub App not configured")),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
            ) as client:
                resp = await client.get("/api/agent-context/github/accessible-repos")

        assert resp.status_code == 404
        assert "GitHub App not configured" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_github_api_failure_returns_502(self):
        """GitHub API failure → 502."""
        metadata = {"installation_id": 42}
        db_rows = [(metadata,)]
        app = _build_app(db_rows=db_rows)

        with (
            patch(
                "agent_context.api.github_repos.resolve_tenant_app_credentials",
                new=AsyncMock(return_value=("app-1", "key")),
            ),
            patch(
                "agent_context.api.github_repos.list_accessible_repos",
                new=AsyncMock(side_effect=RuntimeError("GitHub API 500")),
            ),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
            ) as client:
                resp = await client.get("/api/agent-context/github/accessible-repos")

        assert resp.status_code == 502
        assert "Failed to fetch repos" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# Tests — service layer (mint token + list repos)
# ---------------------------------------------------------------------------


class TestGitHubAppService:
    """Unit tests for the github_app_service module."""

    @pytest.mark.asyncio
    async def test_mint_app_jwt_produces_valid_token(self):
        """_mint_app_jwt produces a decodable RS256 JWT."""
        from agent_context.api.github_app_service import _mint_app_jwt

        # Generate a test RSA key
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa

        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pem = private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode()

        token = _mint_app_jwt("12345", pem)
        assert token  # non-empty string

        # Decode and verify structure
        import jwt as pyjwt

        public_key = private_key.public_key()
        decoded = pyjwt.decode(token, public_key, algorithms=["RS256"])
        assert decoded["iss"] == "12345"
        assert "iat" in decoded
        assert "exp" in decoded

    @pytest.mark.asyncio
    async def test_list_accessible_repos_paginates_and_filters(self):
        """list_accessible_repos applies search filter + pagination."""
        import httpx

        from agent_context.api.github_app_service import list_accessible_repos

        # Mock responses: token mint, then repos
        token_response = httpx.Response(
            200,
            json={"token": "ghs_fake_token"},
            request=httpx.Request("POST", "http://test"),
        )
        repos_response = httpx.Response(
            200,
            json={
                "total_count": 3,
                "repositories": [
                    {
                        "full_name": "acme/api",
                        "private": True,
                        "html_url": "https://github.com/acme/api",
                    },
                    {
                        "full_name": "acme/docs",
                        "private": False,
                        "html_url": "https://github.com/acme/docs",
                    },
                    {
                        "full_name": "acme/frontend",
                        "private": True,
                        "html_url": "https://github.com/acme/frontend",
                    },
                ],
            },
            request=httpx.Request("GET", "http://test"),
        )

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post = AsyncMock(return_value=token_response)
        mock_client.get = AsyncMock(return_value=repos_response)
        mock_client.aclose = AsyncMock()

        # Mock _mint_app_jwt to avoid needing a real RSA key
        with patch(
            "agent_context.api.github_app_service._mint_app_jwt",
            return_value="fake.jwt.token",
        ):
            # No filter, page 1, size 2
            result = await list_accessible_repos(
                installation_id=100,
                app_id="app-1",
                private_key_pem="fake-key",
                page=1,
                page_size=2,
                http_client=mock_client,
            )
            assert result["total"] == 3
            assert result["page"] == 1
            assert result["has_more"] is True
            assert len(result["repos"]) == 2

            # With search filter
            result = await list_accessible_repos(
                installation_id=100,
                app_id="app-1",
                private_key_pem="fake-key",
                q="doc",
                page=1,
                page_size=50,
                http_client=mock_client,
            )
            assert result["total"] == 1
            assert result["repos"][0]["full_name"] == "acme/docs"
            assert result["has_more"] is False

    @pytest.mark.asyncio
    async def test_mint_installation_token_calls_github(self):
        """mint_installation_token POSTs to GitHub and returns the token."""
        import httpx

        from agent_context.api.github_app_service import mint_installation_token

        token_response = httpx.Response(
            200,
            json={"token": "ghs_test123"},
            request=httpx.Request("POST", "http://test"),
        )
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post = AsyncMock(return_value=token_response)
        mock_client.aclose = AsyncMock()

        # Mock _mint_app_jwt to avoid needing a real RSA key
        with patch(
            "agent_context.api.github_app_service._mint_app_jwt",
            return_value="fake.jwt.token",
        ):
            token = await mint_installation_token(
                "app-42", "fake-key", 999, http_client=mock_client
            )
        assert token == "ghs_test123"
        mock_client.post.assert_awaited_once()
        call_args = mock_client.post.call_args
        assert "/app/installations/999/access_tokens" in call_args[0][0]
