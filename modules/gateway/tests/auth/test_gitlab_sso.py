"""Tests for GitLab SSO endpoint: RS256 JWT minting + JWKS.

Issue #3775 (Wave 2): Validates:
- Valid RS256 token with exact claim schema
- sub == users.id (canonical), not cognito_sub
- SSM miss → 404 (GITLAB_NOT_CONFIGURED)
- Secret miss → 503 (GITLAB_SSO_KEY_MISSING)
- 60s JWT expiry
- Unauthenticated request → 401 from middleware
- JWKS returns the public key matching the signing key
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from src.auth.gitlab_sso import (
    _CACHE_TTL_SECONDS,
    _JWT_LIFETIME_SECONDS,
    _discover_gitlab_url,
    _load_signing_key,
    _mint_gitlab_jwt,
    router,
)
from src.auth.middleware import get_current_user_context
from src.shared.database import get_db
from src.shared.schemas.auth import TokenContext

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_caches():
    """Clear module-level caches between tests."""
    import src.auth.gitlab_sso as mod

    mod._gitlab_url_cache = None
    mod._signing_key_cache = None
    yield
    mod._gitlab_url_cache = None
    mod._signing_key_cache = None


@pytest.fixture
def rsa_key_pair():
    """Generate an RSA key pair for testing."""
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_key = private_key.public_key()
    public_pem = public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return private_pem, public_pem, public_key


@pytest.fixture
def mock_token_context():
    """Create a mock TokenContext for an authenticated user."""
    from datetime import UTC, datetime, timedelta

    return TokenContext(
        user_id="cognito-sub-12345",
        org_id="tenant-abc-123",
        team_id="team-xyz",
        department_id="dept-001",
        account_type="human",
        is_admin=False,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        auth_source="jwt",
    )


@pytest.fixture
def mock_user_row():
    """Create a mock User object."""
    user = MagicMock()
    user.id = "canonical-user-uuid-999"
    user.email = "alice@example.com"
    user.name = "Alice Smith"
    user.cognito_username = "alice.smith"
    user.cognito_sub = "cognito-sub-12345"
    return user


@pytest.fixture
def app(mock_token_context, mock_user_row):
    """Create a test FastAPI app with the GitLab SSO router."""
    test_app = FastAPI()
    test_app.include_router(router)

    # Override auth dependency
    async def override_auth():
        return mock_token_context

    # Override DB dependency with a mock session
    async def override_db():
        mock_db = MagicMock()
        # Mock the execute call for user profile resolution
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_user_row
        mock_db.execute = MagicMock(return_value=mock_result)
        # Make it async

        async def async_execute(*args, **kwargs):
            return mock_result

        mock_db.execute = async_execute
        return mock_db

    test_app.dependency_overrides[get_current_user_context] = override_auth
    test_app.dependency_overrides[get_db] = override_db

    return test_app


@pytest.fixture
def unauthenticated_app():
    """Create a test FastAPI app WITHOUT auth override (tests 401)."""
    test_app = FastAPI()
    test_app.include_router(router)
    return test_app


# ---------------------------------------------------------------------------
# Tests: JWT minting
# ---------------------------------------------------------------------------


class TestMintGitlabJWT:
    """Tests for the JWT minting function."""

    def test_valid_token_structure(self, rsa_key_pair):
        """Token contains all required claims with correct values."""
        private_pem, public_pem, public_key = rsa_key_pair

        with patch.dict("os.environ", {"BG_ENVIRONMENT": "dev"}):
            token = _mint_gitlab_jwt(
                canonical_user_id="user-uuid-123",
                cognito_sub="cognito-sub-456",
                tenant_id="tenant-abc",
                email="test@example.com",
                name="Test User",
                username="testuser",
                private_key_pem=private_pem,
            )

        # Decode and verify
        decoded = jwt.decode(token, public_key, algorithms=["RS256"], audience="adp-gitlab-dev")

        assert decoded["iss"] == "urn:adp:gateway:dev"
        assert decoded["sub"] == "user-uuid-123"
        assert decoded["uid"] == "user-uuid-123"
        assert decoded["aud"] == "adp-gitlab-dev"
        assert decoded["cognito_sub"] == "cognito-sub-456"
        assert decoded["tenant_id"] == "tenant-abc"
        assert decoded["email"] == "test@example.com"
        assert decoded["name"] == "Test User"
        assert decoded["username"] == "testuser"
        assert decoded["groups"] == ["tenant-tenant-abc"]
        assert decoded["pre_authorized"] is True
        assert "jti" in decoded
        assert "iat" in decoded
        assert "exp" in decoded

    def test_sub_is_canonical_user_id_not_cognito_sub(self, rsa_key_pair):
        """sub/uid must be the canonical users.id, NOT the cognito_sub."""
        private_pem, _, public_key = rsa_key_pair

        with patch.dict("os.environ", {"BG_ENVIRONMENT": "dev"}):
            token = _mint_gitlab_jwt(
                canonical_user_id="canonical-uuid-999",
                cognito_sub="cognito-sub-different",
                tenant_id="t1",
                email="x@x.com",
                name="X",
                username="x",
                private_key_pem=private_pem,
            )

        decoded = jwt.decode(token, public_key, algorithms=["RS256"], audience="adp-gitlab-dev")
        assert decoded["sub"] == "canonical-uuid-999"
        assert decoded["uid"] == "canonical-uuid-999"
        assert decoded["cognito_sub"] == "cognito-sub-different"
        # sub must NOT equal cognito_sub in this test case
        assert decoded["sub"] != decoded["cognito_sub"]

    def test_60_second_expiry(self, rsa_key_pair):
        """Token expires exactly 60 seconds after issuance."""
        private_pem, _, public_key = rsa_key_pair

        with patch.dict("os.environ", {"BG_ENVIRONMENT": "dev"}):
            token = _mint_gitlab_jwt(
                canonical_user_id="u1",
                cognito_sub="c1",
                tenant_id="t1",
                email="a@b.com",
                name="A",
                username="a",
                private_key_pem=private_pem,
            )

        decoded = jwt.decode(token, public_key, algorithms=["RS256"], audience="adp-gitlab-dev")
        assert decoded["exp"] - decoded["iat"] == _JWT_LIFETIME_SECONDS
        assert decoded["exp"] - decoded["iat"] == 60

    def test_environment_in_claims(self, rsa_key_pair):
        """Claims reflect the configured environment."""
        private_pem, _, public_key = rsa_key_pair

        with patch.dict("os.environ", {"BG_ENVIRONMENT": "prod"}):
            token = _mint_gitlab_jwt(
                canonical_user_id="u1",
                cognito_sub="c1",
                tenant_id="t1",
                email="a@b.com",
                name="A",
                username="a",
                private_key_pem=private_pem,
            )

        decoded = jwt.decode(token, public_key, algorithms=["RS256"], audience="adp-gitlab-prod")
        assert decoded["iss"] == "urn:adp:gateway:prod"
        assert decoded["aud"] == "adp-gitlab-prod"

    def test_unique_jti_per_call(self, rsa_key_pair):
        """Each token gets a unique jti."""
        private_pem, _, public_key = rsa_key_pair

        with patch.dict("os.environ", {"BG_ENVIRONMENT": "dev"}):
            token1 = _mint_gitlab_jwt(
                canonical_user_id="u1",
                cognito_sub="c1",
                tenant_id="t1",
                email="a@b.com",
                name="A",
                username="a",
                private_key_pem=private_pem,
            )
            token2 = _mint_gitlab_jwt(
                canonical_user_id="u1",
                cognito_sub="c1",
                tenant_id="t1",
                email="a@b.com",
                name="A",
                username="a",
                private_key_pem=private_pem,
            )

        decoded1 = jwt.decode(token1, public_key, algorithms=["RS256"], audience="adp-gitlab-dev")
        decoded2 = jwt.decode(token2, public_key, algorithms=["RS256"], audience="adp-gitlab-dev")
        assert decoded1["jti"] != decoded2["jti"]


# ---------------------------------------------------------------------------
# Tests: SSO redirect endpoint
# ---------------------------------------------------------------------------


class TestGitlabSSOEndpoint:
    """Tests for GET /auth/gitlab-sso."""

    @pytest.mark.anyio
    async def test_success_redirect(self, app, rsa_key_pair):
        """Successful request returns 302 to GitLab callback with JWT."""
        private_pem, public_pem, public_key = rsa_key_pair

        with (
            patch("src.auth.gitlab_sso._discover_gitlab_url", return_value="https://gitlab.example.com"),
            patch("src.auth.gitlab_sso._load_signing_key", return_value=private_pem),
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", follow_redirects=False) as client:
                response = await client.get("/auth/gitlab-sso")

        assert response.status_code == 302
        location = response.headers["location"]
        assert location.startswith("https://gitlab.example.com/users/auth/jwt/callback?jwt=")
        assert response.headers["cache-control"] == "no-store"

        # Verify the JWT in the redirect URL
        token = location.split("jwt=")[1]
        decoded = jwt.decode(token, public_key, algorithms=["RS256"], audience="adp-gitlab-dev")
        assert decoded["sub"] == "canonical-user-uuid-999"
        assert decoded["tenant_id"] == "tenant-abc-123"

    @pytest.mark.anyio
    async def test_gitlab_not_configured_returns_404(self, app):
        """SSM miss → 404 with GITLAB_NOT_CONFIGURED error code."""
        with (
            patch("src.auth.gitlab_sso._discover_gitlab_url", return_value=None),
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                response = await client.get("/auth/gitlab-sso")

        assert response.status_code == 404
        body = response.json()
        assert body["error_code"] == "GITLAB_NOT_CONFIGURED"
        assert "not configured" in body["detail"].lower()

    @pytest.mark.anyio
    async def test_signing_key_missing_returns_503(self, app):
        """Secret miss → 503 with GITLAB_SSO_KEY_MISSING error code."""
        with (
            patch("src.auth.gitlab_sso._discover_gitlab_url", return_value="https://gitlab.example.com"),
            patch("src.auth.gitlab_sso._load_signing_key", return_value=None),
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                response = await client.get("/auth/gitlab-sso")

        assert response.status_code == 503
        body = response.json()
        assert body["error_code"] == "GITLAB_SSO_KEY_MISSING"
        assert "signing key" in body["detail"].lower()

    @pytest.mark.anyio
    async def test_unauthenticated_returns_401(self, unauthenticated_app):
        """Request without Bearer token → 401 from auth middleware."""
        async with AsyncClient(transport=ASGITransport(app=unauthenticated_app), base_url="http://test") as client:
            response = await client.get("/auth/gitlab-sso")

        assert response.status_code == 401


# ---------------------------------------------------------------------------
# Tests: JWKS endpoint
# ---------------------------------------------------------------------------


class TestJWKSEndpoint:
    """Tests for GET /.well-known/jwks.json."""

    @pytest.mark.anyio
    async def test_jwks_returns_public_key(self, app, rsa_key_pair):
        """JWKS endpoint returns the public key when signing key is available."""
        private_pem, _, public_key = rsa_key_pair

        with patch("src.auth.gitlab_sso._load_signing_key", return_value=private_pem):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                response = await client.get("/.well-known/jwks.json")

        assert response.status_code == 200
        body = response.json()
        assert "keys" in body
        assert len(body["keys"]) == 1

        key = body["keys"][0]
        assert key["kty"] == "RSA"
        assert key["use"] == "sig"
        assert key["alg"] == "RS256"
        assert "kid" in key
        assert "n" in key
        assert "e" in key

    @pytest.mark.anyio
    async def test_jwks_empty_when_no_key(self, app):
        """JWKS returns empty keyset when signing key is not provisioned."""
        with patch("src.auth.gitlab_sso._load_signing_key", return_value=None):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                response = await client.get("/.well-known/jwks.json")

        assert response.status_code == 200
        body = response.json()
        assert body == {"keys": []}

    @pytest.mark.anyio
    async def test_jwks_key_verifies_minted_token(self, app, rsa_key_pair):
        """Token minted by the SSO endpoint can be verified using the JWKS public key."""
        private_pem, _, _ = rsa_key_pair

        with (
            patch("src.auth.gitlab_sso._discover_gitlab_url", return_value="https://gitlab.example.com"),
            patch("src.auth.gitlab_sso._load_signing_key", return_value=private_pem),
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", follow_redirects=False) as client:
                # Get the JWT from the SSO redirect
                sso_response = await client.get("/auth/gitlab-sso")
                location = sso_response.headers["location"]
                token = location.split("jwt=")[1]

                # Get the JWKS
                jwks_response = await client.get("/.well-known/jwks.json")

        jwks = jwks_response.json()
        jwk_key = jwks["keys"][0]

        # Reconstruct the public key from JWKS and verify the token
        from jwt.algorithms import RSAAlgorithm

        public_key = RSAAlgorithm.from_jwk(jwk_key)
        decoded = jwt.decode(token, public_key, algorithms=["RS256"], audience="adp-gitlab-dev")
        assert decoded["sub"] == "canonical-user-uuid-999"
        assert decoded["tenant_id"] == "tenant-abc-123"

    @pytest.mark.anyio
    async def test_jwks_no_auth_required(self, unauthenticated_app, rsa_key_pair):
        """JWKS endpoint is public — no auth required."""
        private_pem, _, _ = rsa_key_pair

        with patch("src.auth.gitlab_sso._load_signing_key", return_value=private_pem):
            async with AsyncClient(transport=ASGITransport(app=unauthenticated_app), base_url="http://test") as client:
                response = await client.get("/.well-known/jwks.json")

        assert response.status_code == 200
        body = response.json()
        assert len(body["keys"]) == 1


# ---------------------------------------------------------------------------
# Tests: Caching behavior
# ---------------------------------------------------------------------------


class TestCaching:
    """Tests for the 5-minute cache pattern."""

    def test_gitlab_url_cache_hit(self):
        """Subsequent calls within TTL use cached value."""
        import src.auth.gitlab_sso as mod

        # Prime cache
        mod._gitlab_url_cache = (time.monotonic(), "https://cached.example.com")

        # Should return cached value without calling SSM
        with patch("boto3.client") as mock_boto:
            result = _discover_gitlab_url()

        mock_boto.assert_not_called()
        assert result == "https://cached.example.com"

    def test_gitlab_url_cache_expired(self):
        """Expired cache entry triggers a fresh SSM lookup."""
        import src.auth.gitlab_sso as mod

        # Set cache entry that's expired
        mod._gitlab_url_cache = (time.monotonic() - _CACHE_TTL_SECONDS - 1, "https://stale.example.com")

        mock_ssm = MagicMock()
        mock_ssm.get_parameter.return_value = {"Parameter": {"Value": "https://fresh.example.com"}}

        with patch("boto3.client", return_value=mock_ssm):
            result = _discover_gitlab_url()

        assert result == "https://fresh.example.com"
        mock_ssm.get_parameter.assert_called_once()

    def test_signing_key_cache_hit(self, rsa_key_pair):
        """Cached signing key is returned without Secrets Manager call."""
        import src.auth.gitlab_sso as mod

        private_pem, _, _ = rsa_key_pair
        mod._signing_key_cache = (time.monotonic(), private_pem)

        with patch("boto3.client") as mock_boto:
            result = _load_signing_key()

        mock_boto.assert_not_called()
        assert result == private_pem

    def test_signing_key_cache_expired(self, rsa_key_pair):
        """Expired signing key cache triggers fresh Secrets Manager lookup."""
        import src.auth.gitlab_sso as mod

        private_pem, _, _ = rsa_key_pair
        mod._signing_key_cache = (time.monotonic() - _CACHE_TTL_SECONDS - 1, private_pem)

        mock_sm = MagicMock()
        mock_sm.get_secret_value.return_value = {"SecretString": private_pem.decode()}

        with patch("boto3.client", return_value=mock_sm):
            result = _load_signing_key()

        assert result == private_pem
        mock_sm.get_secret_value.assert_called_once()
