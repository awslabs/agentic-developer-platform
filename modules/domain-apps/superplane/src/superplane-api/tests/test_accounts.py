"""Tests for account onboarding and vault credential endpoints."""

import uuid

import pytest

from app.middleware.auth import create_access_token


def _auth_header(org_id: uuid.UUID | None = None) -> dict:
    """Create an Authorization header with a valid JWT."""
    if org_id is None:
        org_id = uuid.uuid4()
    token, _ = create_access_token(org_id)
    return {"Authorization": f"Bearer {token}"}


class TestRegisterAccount:
    """Test POST /accounts."""

    @pytest.mark.asyncio
    async def test_register_requires_auth(self, client):
        """Registering an account without auth returns 401/403."""
        response = await client.post(
            "/accounts",
            json={
                "name": "test-account",
                "provider": "aws",
                "account_id": "123456789012",
                "role_arn": "arn:aws:iam::123456789012:role/SuperplaneAccess",
                "external_id": "sp-org-test1234",
            },
        )
        assert response.status_code in (401, 403)

    @pytest.mark.asyncio
    async def test_register_validates_provider(self, client):
        """Invalid provider returns 422."""
        headers = _auth_header()
        response = await client.post(
            "/accounts",
            json={
                "name": "test",
                "provider": "invalid_provider",
                "account_id": "123",
                "role_arn": "arn:aws:iam::123:role/Test",
                "external_id": "sp-test",
            },
            headers=headers,
        )
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_register_validates_required_fields(self, client):
        """Missing required fields returns 422."""
        headers = _auth_header()
        response = await client.post(
            "/accounts",
            json={"name": "test"},
            headers=headers,
        )
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_register_validates_empty_name(self, client):
        """Empty name returns 422."""
        headers = _auth_header()
        response = await client.post(
            "/accounts",
            json={
                "name": "",
                "provider": "aws",
                "account_id": "123456789012",
                "role_arn": "arn:aws:iam::123456789012:role/Test",
                "external_id": "sp-test",
            },
            headers=headers,
        )
        assert response.status_code == 422


class TestListAccounts:
    """Test GET /accounts."""

    @pytest.mark.asyncio
    async def test_list_requires_auth(self, client):
        """Listing accounts without auth returns 401/403."""
        response = await client.get("/accounts")
        assert response.status_code in (401, 403)


class TestDeleteAccount:
    """Test DELETE /accounts/{id}."""

    @pytest.mark.asyncio
    async def test_delete_requires_auth(self, client):
        """Deleting an account without auth returns 401/403."""
        account_id = uuid.uuid4()
        response = await client.delete(f"/accounts/{account_id}")
        assert response.status_code in (401, 403)

    @pytest.mark.asyncio
    async def test_delete_nonexistent_returns_404(self, client):
        """Deleting a non-existent account returns 404."""
        headers = _auth_header()
        account_id = uuid.uuid4()
        response = await client.delete(f"/accounts/{account_id}", headers=headers)
        assert response.status_code == 404


class TestRegisterCredential:
    """Test POST /vault/credentials."""

    @pytest.mark.asyncio
    async def test_register_requires_auth(self, client):
        """Registering a credential without auth returns 401/403."""
        response = await client.post(
            "/vault/credentials",
            json={
                "name": "test-cred",
                "provider": "nebius",
                # Issue #5046 (U13b): the field is an ADP credential ID — an opaque vault
                # reference — not a Secrets Manager ARN.
                "adp_credential_id": "adp-cred-01HQ8V3XK2WERTY",
            },
        )
        assert response.status_code in (401, 403)

    @pytest.mark.asyncio
    async def test_register_validates_required_fields(self, client):
        """Missing required fields returns 422."""
        headers = _auth_header()
        response = await client.post(
            "/vault/credentials",
            json={"name": "test"},
            headers=headers,
        )
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_register_rejects_a_secret_arn_at_the_route(self, client):
        """Issue #5046 (U13b): an authenticated caller cannot register a secret ARN.

        The model-level rule is covered in test_models.py; this asserts it is reachable
        through the actual HTTP route, so a client sending the old ARN-shaped payload gets
        a 422 rather than persisting a second reference to secret material.
        """
        headers = _auth_header()
        response = await client.post(
            "/vault/credentials",
            json={
                "name": "test-cred",
                "provider": "nebius",
                "adp_credential_id": (
                    "arn:aws:secretsmanager:us-east-1:123456789012:secret:test-AbCdEf"
                ),
            },
            headers=headers,
        )
        assert response.status_code == 422
        assert "must not be an ARN" in response.text


class TestListCredentials:
    """Test GET /vault/credentials."""

    @pytest.mark.asyncio
    async def test_list_requires_auth(self, client):
        """Listing credentials without auth returns 401/403."""
        response = await client.get("/vault/credentials")
        assert response.status_code in (401, 403)


class TestDeleteCredential:
    """Test DELETE /vault/credentials/{id}."""

    @pytest.mark.asyncio
    async def test_delete_requires_auth(self, client):
        """Deleting a credential without auth returns 401/403."""
        cred_id = uuid.uuid4()
        response = await client.delete(f"/vault/credentials/{cred_id}")
        assert response.status_code in (401, 403)

    @pytest.mark.asyncio
    async def test_delete_nonexistent_returns_404(self, client):
        """Deleting a non-existent credential returns 404."""
        headers = _auth_header()
        cred_id = uuid.uuid4()
        response = await client.delete(f"/vault/credentials/{cred_id}", headers=headers)
        assert response.status_code == 404
