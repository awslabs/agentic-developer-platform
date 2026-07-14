"""GitLab SSO endpoint: RS256 JWT minting + JWKS discovery.

Issue #3775 (Wave 2): Mints a short-lived RS256-signed JWT asserting the caller's
identity + tenant and 302-redirects to GitLab's `jwt` OmniAuth callback. Also
exposes `/.well-known/jwks.json` (the public key) for future OIDC clients.

Zero deployment dependency: everything is discovered lazily at request time.
On GitLab-less deployments the endpoint returns 404 and nothing else exists.
"""

from __future__ import annotations

import logging
import os
import time
import uuid

import boto3
import jwt
from botocore.exceptions import ClientError
from cryptography.hazmat.primitives.serialization import load_pem_private_key
from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth.middleware import get_current_user_context
from src.shared.database import get_db
from src.shared.models.organization import User
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

    return jwt.encode(payload, private_key_pem, algorithm="RS256")


# ---------------------------------------------------------------------------
# User profile resolution
# ---------------------------------------------------------------------------


async def _resolve_user_profile(db: AsyncSession, cognito_sub: str) -> dict:
    """Resolve user profile (email, name, username) from the users table.

    Falls back to placeholder values if the user row isn't found.
    """
    from sqlalchemy import select

    try:
        result = await db.execute(select(User).where(User.cognito_sub == cognito_sub))
        user = result.scalar_one_or_none()
        if user:
            return {
                "canonical_id": user.id,
                "email": user.email or "",
                "name": user.name or user.email or "",
                "username": user.cognito_username or (user.email.split("@")[0] if user.email else ""),
            }
    except Exception:
        logger.warning("User profile lookup failed for cognito_sub=%s", cognito_sub, exc_info=True)

    return {
        "canonical_id": cognito_sub,
        "email": "",
        "name": "",
        "username": "",
    }


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
    current_user: TokenContext = Depends(get_current_user_context),
    db: AsyncSession = Depends(get_db),
):
    """Mint a JWT and redirect user to their tenant's GitLab instance."""
    tenant_id = current_user.org_id

    # 1. Discover GitLab URL (lazy — SSM lookup, cached 5 min)
    gitlab_url = _discover_gitlab_url()
    if not gitlab_url:
        return JSONResponse(
            status_code=404,
            content={
                "detail": "GitLab is not configured for this environment. Contact your administrator to enable source control.",
                "error_code": "GITLAB_NOT_CONFIGURED",
            },
        )

    # 2. Load RSA private key (lazy — Secrets Manager, cached 5 min)
    private_key = _load_signing_key()
    if not private_key:
        return JSONResponse(
            status_code=503,
            content={
                "detail": "GitLab SSO is temporarily unavailable. The signing key has not been provisioned.",
                "error_code": "GITLAB_SSO_KEY_MISSING",
            },
        )

    # 3. Resolve canonical user identity (users.id, not cognito_sub)
    cognito_sub = current_user.user_id
    profile = await _resolve_user_profile(db, cognito_sub)
    canonical_user_id = profile["canonical_id"]

    # 4. Mint JWT (includes tenant_id for group assignment)
    token = _mint_gitlab_jwt(
        canonical_user_id=canonical_user_id,
        cognito_sub=cognito_sub,
        tenant_id=tenant_id,
        email=profile["email"],
        name=profile["name"],
        username=profile["username"],
        private_key_pem=private_key,
    )

    # 5. Redirect
    redirect_url = f"{gitlab_url}/users/auth/jwt/callback?jwt={token}"
    return RedirectResponse(
        url=redirect_url,
        status_code=302,
        headers={"Cache-Control": "no-store"},
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
    private_key_pem = _load_signing_key()
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
