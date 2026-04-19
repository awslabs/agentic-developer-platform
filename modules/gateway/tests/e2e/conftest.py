"""
E2E-specific fixtures for dual-mode (unit / live) testing.

Unit mode: tests hit the FastAPI ASGI app directly via httpx.
Live mode: tests hit the deployed gateway via real HTTP.
"""

from __future__ import annotations

import json as _json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import httpx
import jwt as pyjwt
import pytest

from tests.e2e.config import is_live, load_live_config

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


# ---------------------------------------------------------------------------
# Auto-skip markers
# ---------------------------------------------------------------------------


def pytest_collection_modifyitems(config, items):  # noqa: ANN001
    """Skip ``live_only`` and ``browser`` tests when not in live mode."""
    skip_live = pytest.mark.skip(reason="live_only tests require TEST_ENV=dev")
    skip_browser = pytest.mark.skip(reason="browser tests require Playwright (pip install playwright)")
    for item in items:
        if "live_only" in item.keywords and not is_live():
            item.add_marker(skip_live)
        if "browser" in item.keywords and not is_live():
            item.add_marker(skip_browser)


# ---------------------------------------------------------------------------
# RSA key-pair for unit-mode JWT signing (generated at import time)
# ---------------------------------------------------------------------------


def _generate_rsa_keypair() -> tuple[bytes, bytes]:
    """Generate a 2048-bit RSA key pair and return (private_pem, public_pem)."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    )
    pub_pem = key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return priv_pem, pub_pem


_RSA_PRIVATE_KEY, _RSA_PUBLIC_KEY = _generate_rsa_keypair()

TEST_JWT_ISSUER = "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_TestPool"
TEST_JWT_AUD = "test-client-id"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mint_jwt(
    sub: str = "test-user-id",
    email: str = "test@example.com",
    groups: list[str] | None = None,
    aud: str = TEST_JWT_AUD,
    iss: str = TEST_JWT_ISSUER,
    exp_delta: timedelta | None = None,
    extra_claims: dict[str, Any] | None = None,
) -> str:
    """Mint a RS256-signed JWT for unit-mode tests."""
    now = datetime.now(UTC)
    exp = now + (exp_delta if exp_delta is not None else timedelta(hours=1))
    payload: dict[str, Any] = {
        "sub": sub,
        "email": email,
        "iss": iss,
        "aud": aud,
        "iat": int(now.timestamp()),
        "exp": int(exp.timestamp()),
        "token_use": "access",
        "cognito:groups": groups or [],
    }
    if extra_claims:
        payload.update(extra_claims)
    return pyjwt.encode(payload, _RSA_PRIVATE_KEY, algorithm="RS256")


# ---------------------------------------------------------------------------
# Fixtures — JWT minting
# ---------------------------------------------------------------------------


@pytest.fixture
def jwt_for_user() -> str:
    """Valid user JWT (unit mode) or Cognito token (live mode).

    In unit mode returns a locally-signed RS256 token.
    In live mode calls ``admin-initiate-auth`` against Cognito.
    """
    if is_live():
        cfg = load_live_config()
        import boto3

        client = boto3.client("cognito-idp", region_name=cfg.aws_region)
        resp = client.admin_initiate_auth(
            UserPoolId=cfg.cognito_user_pool_id,
            ClientId=cfg.cognito_client_id,
            AuthFlow="ADMIN_USER_PASSWORD_AUTH",
            AuthParameters={
                "USERNAME": cfg.test_user_email,
                "PASSWORD": cfg.test_user_password,
            },
        )
        return resp["AuthenticationResult"]["AccessToken"]

    return _mint_jwt(groups=["user"])


@pytest.fixture
def jwt_for_admin() -> str:
    """JWT with admin group claim (unit mode only)."""
    return _mint_jwt(groups=["admin"], extra_claims={"custom:is_admin": "true"})


@pytest.fixture
def jwt_for_agent() -> str:
    """Agent JWT via client_credentials (live) or locally-minted (unit).

    In live mode reads credentials from Secrets Manager and calls the
    Cognito token endpoint.
    """
    if is_live():
        cfg = load_live_config()
        import boto3

        sm = boto3.client("secretsmanager", region_name=cfg.aws_region)
        secret = _json.loads(sm.get_secret_value(SecretId=f"bedrockgw-{cfg.environment}-agent-cognito-credentials")["SecretString"])
        resp = httpx.post(
            secret["token_endpoint"],
            data={
                "grant_type": "client_credentials",
                "client_id": secret["client_id"],
                "client_secret": secret["client_secret"],
                "scope": secret.get("scope", ""),
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        resp.raise_for_status()
        return resp.json()["access_token"]

    return _mint_jwt(
        sub="agent-client-id",
        email="agent@system",
        groups=["agent"],
        extra_claims={"scope": "bedrock/invoke"},
    )


@pytest.fixture
def expired_jwt() -> str:
    """JWT whose ``exp`` is in the past."""
    return _mint_jwt(exp_delta=timedelta(hours=-1))


@pytest.fixture
def wrong_aud_jwt() -> str:
    """JWT signed with the test key but with a bogus audience."""
    return _mint_jwt(aud="bogus-audience-that-does-not-exist")


# ---------------------------------------------------------------------------
# Fixtures — HTTP clients
# ---------------------------------------------------------------------------


@pytest.fixture
async def api_client() -> AsyncGenerator[httpx.AsyncClient, None]:
    """HTTP client for the gateway **API surface** — not the SPA.

    In unit mode: FastAPI ASGI transport.
    In live mode: REST API Gateway invoke URL (``api_gateway_url``). This goes
    directly to the backend via VPC Link, bypassing CloudFront. CloudFront
    routes ``/api/*`` to the same backend but also owns the SPA fallback at
    ``/``, so tests that construct URLs like ``/v1/messages`` against the
    CloudFront root get HTML back from the S3 origin. Hit API Gateway for
    API-contract tests; use ``cloudfront_client`` for CDN-layer tests.
    """
    if is_live():
        cfg = load_live_config()
        base = cfg.api_gateway_url.rstrip("/")
        if not base:
            raise RuntimeError(
                "Live-mode API tests require API_GATEWAY_URL (the REST API Gateway "
                "invoke URL, e.g. https://<id>.execute-api.<region>.amazonaws.com/<stage>). "
                "Do not use CLOUDFRONT_DOMAIN here — CloudFront serves the SPA at /, "
                "which masks API responses. Use the cloudfront_client fixture if you "
                "genuinely need to exercise the CDN layer."
            )
        async with httpx.AsyncClient(base_url=base, timeout=60.0) as client:
            yield client
    else:
        # Unit mode — import app and use ASGI transport
        from httpx import ASGITransport

        from src.app import create_app

        app = create_app()
        transport = ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client


@pytest.fixture
async def cloudfront_client() -> AsyncGenerator[httpx.AsyncClient, None]:
    """HTTP client pointing at the CloudFront distribution root.

    Use this ONLY for CDN-layer tests — e.g. SPA smoke tests, ``/api/*``
    routing sanity checks, CloudFront response-header policies. For API
    contract tests (auth, proxy, admin, budget, ratelimit, pool), use the
    ``api_client`` fixture which targets the REST API Gateway directly.

    Skips in unit mode — CloudFront has no unit-mode equivalent.
    """
    if not is_live():
        pytest.skip("cloudfront_client requires live mode (TEST_ENV=dev)")
    cfg = load_live_config()
    base = f"https://{cfg.cloudfront_domain}"
    async with httpx.AsyncClient(base_url=base, timeout=60.0) as client:
        yield client


# ---------------------------------------------------------------------------
# Fixtures — Playwright (browser tests)
# ---------------------------------------------------------------------------


@pytest.fixture
def playwright_page():
    """Synchronous Playwright page fixture (only loaded when needed).

    Tests using this fixture should be marked ``@pytest.mark.browser``.
    """
    pytest.importorskip("playwright")
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        yield page
        browser.close()
