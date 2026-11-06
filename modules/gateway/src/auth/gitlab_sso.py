"""GitLab SSO endpoint: RS256 JWT minting + JWKS discovery.

Issue #3775 (Wave 2): Mints a short-lived RS256-signed JWT asserting the caller's
identity + tenant and 302-redirects to GitLab's `jwt` OmniAuth callback. Also
exposes `/.well-known/jwks.json` (the public key) for future OIDC clients.

Zero deployment dependency: everything is discovered lazily at request time.
On GitLab-less deployments the endpoint returns 404 and nothing else exists.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from urllib.parse import urlsplit

import boto3
import jwt
from botocore.exceptions import ClientError
from cryptography.hazmat.primitives.serialization import load_pem_private_key
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth.dependencies import get_current_user
from src.shared.database import get_db
from src.shared.schemas.auth import TokenContext

logger = logging.getLogger(__name__)

router = APIRouter(tags=["authentication"])

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_CACHE_TTL_SECONDS = 300  # 5 minutes
_JWT_LIFETIME_SECONDS = 60  # 1 minute validity


def _get_environment() -> str:
    """Return the current ADP environment name (default: 'dev')."""
    return os.environ.get("BG_ENVIRONMENT", "dev")


def _get_region() -> str:
    """Return the AWS region from settings or default."""
    return os.environ.get("AWS_DEFAULT_REGION", os.environ.get("AWS_REGION", "us-east-1"))


# ---------------------------------------------------------------------------
# In-process caches (time.monotonic pattern from admin/connections/service.py)
# ---------------------------------------------------------------------------

_gitlab_url_cache: tuple[float, str | None] | None = None
_signing_key_cache: tuple[float, bytes | None] | None = None


def _cache_valid(entry: tuple[float, object] | None) -> bool:
    """Return True if cache entry exists and is within TTL."""
    if entry is None:
        return False
    cached_at, _ = entry
    return (time.monotonic() - cached_at) < _CACHE_TTL_SECONDS


# ---------------------------------------------------------------------------
# Lazy discovery: GitLab URL from SSM
# ---------------------------------------------------------------------------


def _discover_gitlab_url() -> str | None:
    """Fetch GitLab URL from SSM Parameter Store (cached 5 min).

    Returns None if the parameter does not exist (GitLab not configured).
    """
    global _gitlab_url_cache

    if _cache_valid(_gitlab_url_cache):
        return _gitlab_url_cache[1]  # type: ignore[index]

    env = _get_environment()
    region = _get_region()
    param_name = f"/adp/{env}/gitlab/url"

    try:
        ssm = boto3.client("ssm", region_name=region)
        response = ssm.get_parameter(Name=param_name)
        value = response["Parameter"]["Value"]
        # Strip trailing slash for consistent URL construction
        value = value.rstrip("/")
        _gitlab_url_cache = (time.monotonic(), value)
        return value
    except ClientError as e:
        code = e.response["Error"]["Code"]
        if code == "ParameterNotFound":
            logger.info("GitLab URL parameter %s not found — feature disabled", param_name)
            _gitlab_url_cache = (time.monotonic(), None)
            return None
        logger.error("SSM GetParameter failed for %s: %s", param_name, e)
        # Don't cache errors — retry on next request
        return None


# ---------------------------------------------------------------------------
# Lazy discovery: RSA signing key from Secrets Manager
# ---------------------------------------------------------------------------


def _load_signing_key() -> bytes | None:
    """Fetch RSA private key PEM from Secrets Manager (cached 5 min).

    Returns None if the secret does not exist (key not provisioned).
    """
    global _signing_key_cache

    if _cache_valid(_signing_key_cache):
        return _signing_key_cache[1]  # type: ignore[index]

    env = _get_environment()
    region = _get_region()
    secret_id = f"adp/{env}/gateway-oidc-signing-key"

    try:
        sm = boto3.client("secretsmanager", region_name=region)
        response = sm.get_secret_value(SecretId=secret_id)
        pem_bytes = response["SecretString"].encode("utf-8")
        # Validate that it's actually a valid PEM private key
        load_pem_private_key(pem_bytes, password=None)
        _signing_key_cache = (time.monotonic(), pem_bytes)
        return pem_bytes
    except ClientError as e:
        code = e.response["Error"]["Code"]
        if code == "ResourceNotFoundException":
            logger.info("Signing key secret %s not found — SSO unavailable", secret_id)
            _signing_key_cache = (time.monotonic(), None)
            return None
        logger.error("Secrets Manager GetSecretValue failed for %s: %s", secret_id, e)
        return None
    except (ValueError, TypeError) as e:
        logger.error("Signing key PEM is invalid: %s", e)
        return None


# ---------------------------------------------------------------------------
# JWT minting
# ---------------------------------------------------------------------------


def _mint_gitlab_jwt(
    *,
    canonical_user_id: str,
    cognito_sub: str,
    tenant_id: str,
    email: str,
    name: str,
    username: str,
    private_key_pem: bytes,
    return_path: str | None = None,
) -> str:
    """Mint an RS256-signed JWT for GitLab SSO.

    Claims follow the schema defined in the design note (rev 4).
    """
    env = _get_environment()
    now = int(time.time())

    payload = {
        "iss": f"urn:adp:gateway:{env}",
        "sub": canonical_user_id,
        "aud": f"adp-gitlab-{env}",
        "iat": now,
        "exp": now + _JWT_LIFETIME_SECONDS,
        "jti": str(uuid.uuid4()),
        "tenant_id": tenant_id,
        "uid": canonical_user_id,
        "cognito_sub": cognito_sub,
        "name": name,
        "email": email,
        "username": username,
        "groups": [f"tenant-{tenant_id}"],
        "pre_authorized": True,
    }

    if return_path is not None:
        payload["return_path"] = return_path
    return jwt.encode(payload, private_key_pem, algorithm="RS256")


@router.post("/auth/gitlab-cli", operation_id="authorizeGitlabCli")
async def gitlab_cli_authorize(current_user: TokenContext = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """One-use CLI handoff. Cognito credentials never leave the ADP gateway."""
    from src.gitlab import homes
    from src.gitlab.config import host

    url = await asyncio.to_thread(_discover_gitlab_url) if homes.enabled() else None
    if not url:
        return JSONResponse({"detail": {"code": "gitlab_not_configured"}}, status_code=404)
    # Same deployment-controlled HTTPS authority used by Product homes.
    url = host(url)
    identity = await homes.managed_login(db, current_user, url)
    key = await asyncio.to_thread(_load_signing_key)
    if not key:
        return JSONResponse({"detail": {"code": "gitlab_unavailable"}}, status_code=503)
    now = int(time.time())
    assertion = jwt.encode(
        {
            "iss": f"urn:adp:gateway:{_get_environment()}",
            "aud": f"adp-gitlab-cli-{_get_environment()}",
            "purpose": "cli_oauth_bootstrap",
            "sub": identity["id"],
            "tenant_id": current_user.org_id,
            "iat": now,
            "exp": now + 60,
            "jti": str(uuid.uuid4()),
        },
        key,
        algorithm="RS256",
    )
    return JSONResponse(
        {"gitlab_url": url, "subject": identity["id"], "assertion": assertion},
        headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
    )


# ---------------------------------------------------------------------------
# User profile resolution
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get(
    "/auth/gitlab-sso",
    summary="GitLab SSO redirect",
    description="Mints a JWT and redirects the user to their GitLab instance.",
    responses={
        302: {"description": "Redirect to GitLab JWT callback"},
        404: {"description": "GitLab not configured for this environment"},
        503: {"description": "Signing key not provisioned"},
    },
)
async def gitlab_sso_redirect(
    request: Request,
    current_user: TokenContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Mint a JWT and redirect user to their tenant's GitLab instance."""
    tenant_id = current_user.org_id

    # 1. Discover GitLab URL (lazy — SSM lookup, cached 5 min)
    gitlab_url = await asyncio.to_thread(_discover_gitlab_url)
    if not gitlab_url:
        return JSONResponse(
            status_code=404,
            content={
                "detail": "GitLab is not configured for this environment. Contact your administrator to enable source control.",
                "error_code": "GITLAB_NOT_CONFIGURED",
            },
        )

    # The configured deployment URL is the handoff authority. Never derive the
    # destination from a followed redirect or a caller-provided URL.
    try:
        parsed_url = urlsplit(gitlab_url)
        valid_url = (
            parsed_url.scheme == "https"
            and parsed_url.hostname
            and not parsed_url.username
            and not parsed_url.password
            and not parsed_url.query
            and not parsed_url.fragment
            and not any(character.isspace() or ord(character) < 32 for character in gitlab_url)
            and "\\" not in gitlab_url
        )
        parsed_url.port  # Reject malformed ports before minting a credential.
    except ValueError:
        valid_url = False
    if not valid_url:
        return JSONResponse(status_code=503, content={"error_code": "GITLAB_SSO_URL_INVALID"})

    # 2. Load RSA private key (lazy — Secrets Manager, cached 5 min)
    private_key = await asyncio.to_thread(_load_signing_key)
    if not private_key:
        return JSONResponse(
            status_code=503,
            content={
                "detail": "GitLab SSO is temporarily unavailable. The signing key has not been provisioned.",
                "error_code": "GITLAB_SSO_KEY_MISSING",
            },
        )

    from src.gitlab.homes import managed_login

    identity = await managed_login(db, current_user, gitlab_url)
    token = _mint_gitlab_jwt(
        canonical_user_id=identity["id"],
        cognito_sub=identity["subject"],
        tenant_id=tenant_id,
        email=identity["email"],
        name=identity["name"],
        username=f"adp-{identity['id']}",
        private_key_pem=private_key,
        return_path=urlsplit(gitlab_url).path.rstrip("/") + "/",
    )

    # 5. Browser fetches obtain the authorized handoff without following it.
    redirect_url = f"{gitlab_url}/users/auth/jwt/callback?jwt={token}"
    if "application/json" in request.headers.get("accept", "").lower():
        return JSONResponse(content={"redirect_url": redirect_url}, headers={"Cache-Control": "no-store", "Vary": "Accept"})
    return RedirectResponse(
        url=redirect_url,
        status_code=302,
        headers={"Cache-Control": "no-store", "Vary": "Accept"},
    )


@router.get(
    "/.well-known/jwks.json",
    summary="JWKS endpoint",
    description="Serves the public key for verifying GitLab SSO JWTs.",
    responses={
        200: {"description": "JWKS key set"},
    },
)
async def jwks_endpoint():
    """Serve the JWKS (public key) for JWT verification.

    Returns an empty keyset if the signing key has not been provisioned.
    GitLab doesn't consume this (takes PEM in config), but it makes the
    signing identity reusable by future OIDC clients.
    """
    private_key_pem = await asyncio.to_thread(_load_signing_key)
    if not private_key_pem:
        # Return empty keyset — no keys provisioned
        return JSONResponse(
            content={"keys": []},
            headers={"Cache-Control": "public, max-age=300"},
        )

    try:
        from jwt import algorithms

        # Load the private key and extract the public key
        private_key = load_pem_private_key(private_key_pem, password=None)
        public_key = private_key.public_key()

        # Use PyJWT's built-in JWK conversion
        algo = algorithms.RSAAlgorithm(algorithms.RSAAlgorithm.SHA256)
        jwk = algo.to_jwk(public_key, as_dict=True)

        # Add standard JWKS fields
        jwk["use"] = "sig"
        jwk["alg"] = "RS256"
        jwk["kid"] = f"adp-gateway-{_get_environment()}"

        return JSONResponse(
            content={"keys": [jwk]},
            headers={"Cache-Control": "public, max-age=300"},
        )
    except Exception:
        logger.error("Failed to derive public key from signing key", exc_info=True)
        return JSONResponse(
            content={"keys": []},
            headers={"Cache-Control": "public, max-age=60"},
        )
