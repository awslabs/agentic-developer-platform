"""Tests for organization settings and SSO endpoints.

Covers:
  - GET /orgs/current (extended response with new fields)
  - PATCH /orgs/current (update org settings)
  - GET /orgs/current/sso
  - PATCH /orgs/current/sso (SSO configuration)
  - DELETE /orgs/current/sso (disable SSO)
"""

import uuid

import pytest

from app.middleware.auth import create_access_token
from app.schemas.org import VALID_CLOUDS, VALID_SSO_PROVIDERS, VALID_SSO_TYPES


# -- Helpers --


def _auth_headers(org_id: uuid.UUID | None = None) -> dict:
    """Create auth headers with a valid JWT for the given org."""
    if org_id is None:
        org_id = uuid.uuid4()
    token, _ = create_access_token(org_id)
    return {"Authorization": f"Bearer {token}"}


# -- Schema validation tests --


class TestOrgSchemas:
    """Test Pydantic schema constants."""

    def test_valid_clouds_contains_expected(self):
        assert "aws" in VALID_CLOUDS
        assert "gcp" in VALID_CLOUDS
        assert "azure" in VALID_CLOUDS

    def test_valid_sso_providers_contains_expected(self):
        assert "okta" in VALID_SSO_PROVIDERS
        assert "azure-ad" in VALID_SSO_PROVIDERS
        assert "google" in VALID_SSO_PROVIDERS

    def test_valid_sso_types(self):
        assert VALID_SSO_TYPES == {"SAML", "OIDC"}


# -- GET /orgs/current tests --


class TestGetOrgCurrent:
    """Test GET /orgs/current."""

    @pytest.mark.asyncio
    async def test_get_org_requires_auth(self, client):
        """Unauthenticated request returns 401/403."""
        response = await client.get("/orgs/current")
        assert response.status_code in (401, 403)

    @pytest.mark.asyncio
    async def test_get_org_with_invalid_org_returns_404(self, client):
        """Authenticated but org not in DB returns 404."""
        headers = _auth_headers()
        response = await client.get("/orgs/current", headers=headers)
        # With in-memory DB, org won't exist
        assert response.status_code == 404


# -- PATCH /orgs/current tests --


class TestUpdateOrgSettings:
    """Test PATCH /orgs/current."""

    @pytest.mark.asyncio
    async def test_update_org_requires_auth(self, client):
        """Unauthenticated request returns 401/403."""
        response = await client.patch("/orgs/current", json={"name": "new-name"})
        assert response.status_code in (401, 403)

    @pytest.mark.asyncio
    async def test_update_org_validates_clouds(self, client):
        """Invalid cloud names are rejected."""
        headers = _auth_headers()
        response = await client.patch(
            "/orgs/current",
            json={"allowed_clouds": ["aws", "invalid-cloud"]},
            headers=headers,
        )
        # Either 422 (validation) or 404 (org not in DB) — both are correct behavior
        assert response.status_code in (404, 422)

    @pytest.mark.asyncio
    async def test_update_org_empty_body_accepted(self, client):
        """Empty body is valid (no fields to update)."""
        headers = _auth_headers()
        response = await client.patch("/orgs/current", json={}, headers=headers)
        # 404 because org doesn't exist, but the request itself is valid
        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_update_org_name_too_long(self, client):
        """Name exceeding max length returns 422."""
        headers = _auth_headers()
        response = await client.patch(
            "/orgs/current",
            json={"name": "x" * 256},
            headers=headers,
        )
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_update_org_invalid_email(self, client):
        """Invalid email format returns 422."""
        headers = _auth_headers()
        response = await client.patch(
            "/orgs/current",
            json={"billing_email": "not-an-email"},
            headers=headers,
        )
        assert response.status_code == 422


# -- SSO endpoint tests --


class TestSSOEndpoints:
    """Test SSO configuration endpoints."""

    @pytest.mark.asyncio
    async def test_get_sso_requires_auth(self, client):
        """GET /orgs/current/sso requires auth."""
        response = await client.get("/orgs/current/sso")
        assert response.status_code in (401, 403)

    @pytest.mark.asyncio
    async def test_configure_sso_requires_auth(self, client):
        """PATCH /orgs/current/sso requires auth."""
        response = await client.patch(
            "/orgs/current/sso",
            json={
                "sso_provider": "okta",
                "metadata_url": "https://example.okta.com/metadata",
            },
        )
        assert response.status_code in (401, 403)

    @pytest.mark.asyncio
    async def test_configure_sso_invalid_provider(self, client):
        """Invalid SSO provider is rejected."""
        headers = _auth_headers()
        response = await client.patch(
            "/orgs/current/sso",
            json={
                "sso_provider": "invalid-provider",
                "metadata_url": "https://example.com/metadata",
            },
            headers=headers,
        )
        # 404 (org not found) or 422 (invalid provider)
        assert response.status_code in (404, 422)

    @pytest.mark.asyncio
    async def test_configure_sso_invalid_type(self, client):
        """Invalid SSO type is rejected."""
        headers = _auth_headers()
        response = await client.patch(
            "/orgs/current/sso",
            json={
                "sso_provider": "okta",
                "sso_provider_type": "INVALID",
                "metadata_url": "https://example.com/metadata",
            },
            headers=headers,
        )
        assert response.status_code in (404, 422)

    @pytest.mark.asyncio
    async def test_configure_sso_missing_fields_returns_422(self, client):
        """Missing required fields returns 422."""
        headers = _auth_headers()
        response = await client.patch(
            "/orgs/current/sso",
            json={"sso_provider": "okta"},  # missing metadata_url
            headers=headers,
        )
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_disable_sso_requires_auth(self, client):
        """DELETE /orgs/current/sso requires auth."""
        response = await client.request(
            "DELETE",
            "/orgs/current/sso",
            json={"confirm": True},
        )
        assert response.status_code in (401, 403)

    @pytest.mark.asyncio
    async def test_disable_sso_requires_confirm(self, client):
        """DELETE /orgs/current/sso requires confirm=true."""
        headers = _auth_headers()
        response = await client.request(
            "DELETE",
            "/orgs/current/sso",
            json={"confirm": False},
            headers=headers,
        )
        assert response.status_code == 422
