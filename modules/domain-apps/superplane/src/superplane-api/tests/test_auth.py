"""Tests for auth endpoints (POST /auth/login, POST /auth/token)."""

import uuid

import pytest

from app.middleware.auth import create_access_token, decode_token
from app.routers.auth import _generate_api_key, _hash_api_key, _verify_api_key
from app.schemas.auth import TokenPayload


# -- JWT utility tests --


class TestJWTUtils:
    """Test JWT creation and decoding."""

    def test_create_and_decode_token(self):
        org_id = uuid.uuid4()
        token, expires_in = create_access_token(org_id)
        assert isinstance(token, str)
        assert expires_in == 3600  # default 60 min * 60

        payload = decode_token(token)
        assert isinstance(payload, TokenPayload)
        assert payload.org_id == org_id

    def test_decode_invalid_token_raises(self):
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc_info:
            decode_token("invalid.token.here")
        assert exc_info.value.status_code == 401

    def test_create_token_returns_string(self):
        org_id = uuid.uuid4()
        token, _ = create_access_token(org_id)
        assert token.count(".") == 2  # JWT has 3 parts


# -- API key hashing tests --


class TestApiKeyHashing:
    """Test API key SHA-256 hashing."""

    def test_hash_and_verify(self):
        raw_key = _generate_api_key()
        hashed = _hash_api_key(raw_key)
        assert _verify_api_key(raw_key, hashed)

    def test_wrong_key_fails_verify(self):
        raw_key = _generate_api_key()
        hashed = _hash_api_key(raw_key)
        wrong_key = _generate_api_key()
        assert not _verify_api_key(wrong_key, hashed)

    def test_key_prefix_format(self):
        key = _generate_api_key()
        assert key.startswith("sp_")
        assert len(key) == 3 + 32  # "sp_" + 32 hex chars

    def test_hash_is_hex_string(self):
        key = _generate_api_key()
        hashed = _hash_api_key(key)
        assert len(hashed) == 64  # SHA-256 hex digest


# -- Auth router endpoint tests --


class TestLoginEndpoint:
    """Test POST /auth/login."""

    @pytest.mark.asyncio
    async def test_login_without_key_returns_422(self, client):
        """Login without api_key field returns validation error."""
        response = await client.post("/auth/login", json={})
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_login_with_empty_body_returns_422(self, client):
        """Login with no JSON body returns validation error."""
        response = await client.post("/auth/login")
        assert response.status_code == 422


class TestCreateApiKeyEndpoint:
    """Test POST /auth/token."""

    @pytest.mark.asyncio
    async def test_create_key_requires_auth(self, client):
        """Creating an API key requires a valid JWT."""
        response = await client.post("/auth/token", json={"name": "test-key"})
        assert response.status_code in (401, 403)

    @pytest.mark.asyncio
    async def test_create_key_empty_name_returns_422(self, client):
        """Creating an API key with empty name returns 422."""
        org_id = uuid.uuid4()
        token, _ = create_access_token(org_id)
        headers = {"Authorization": f"Bearer {token}"}
        response = await client.post("/auth/token", json={"name": ""}, headers=headers)
        assert response.status_code == 422
