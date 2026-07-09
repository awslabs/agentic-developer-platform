"""GitHub App service — mint installation tokens and list accessible repos.

Issue #2045: Relocated from agent_context.api.github_app_service into the gateway.
Original: Issue #1793 (Story D of E10 #1736).

Resolves per-tenant GitHub App credentials from Secrets Manager at:
    adp/<env>/tenants/<org_id>/github-app

The secret payload is JSON: {"app_id": "...", "private_key": "..."}

Uses the same RS256 JWT minting pattern as the gateway's github_client.py
(issue #465 / #1672).
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger("bedrockgateway.knowledge.github_app_service")

GITHUB_API_BASE = "https://api.github.com"
_APP_JWT_EXPIRY_SECONDS = 600  # 10 min max per GitHub spec
_APP_JWT_BACKDATE_SECONDS = 60  # clock-skew buffer


# ---------------------------------------------------------------------------
# JWT minting (reuses pattern from gateway's github_client.py)
# ---------------------------------------------------------------------------


def _mint_app_jwt(app_id: str, private_key_pem: str) -> str:
    """Generate a short-lived RS256 JWT to authenticate as the GitHub App."""
    import jwt as pyjwt

    now = int(time.time())
    payload = {
        "iat": now - _APP_JWT_BACKDATE_SECONDS,
        "exp": now + _APP_JWT_EXPIRY_SECONDS,
        "iss": app_id,
    }
    return pyjwt.encode(payload, private_key_pem, algorithm="RS256")


# ---------------------------------------------------------------------------
# Secrets Manager — resolve tenant GitHub App credentials
# ---------------------------------------------------------------------------


def _get_environment() -> str:
    """Return the deployment environment (dev/staging/prod)."""
    return os.environ.get("ENVIRONMENT", "dev")


def _classify_boto3_error(exc: Exception) -> str | None:
    """Extract the AWS error code from a boto3 ClientError.

    Issue #3358: Uses the structured response['Error']['Code'] field when
    available (proper ClientError), falls back to string matching for generic
    exceptions (e.g. connection timeouts wrapped in non-ClientError types).

    Returns the error code string (e.g. 'ResourceNotFoundException',
    'AccessDeniedException') or None if it cannot be determined.
    """
    # Try structured boto3 ClientError first
    if hasattr(exc, "response"):
        try:
            return exc.response.get("Error", {}).get("Code")
        except (AttributeError, TypeError):
            pass

    # Fallback: string matching for exceptions that don't carry .response
    exc_str = str(exc)
    if "ResourceNotFoundException" in exc_str:
        return "ResourceNotFoundException"
    elif "AccessDeniedException" in exc_str:
        return "AccessDeniedException"
    return None


async def resolve_tenant_app_credentials(
    org_id: str,
    *,
    sm_client: Any | None = None,
) -> tuple[str, str]:
    """Resolve tenant GitHub App credentials from Secrets Manager.

    Returns (app_id, private_key_pem).

    The secret is at: adp/<env>/tenants/<org_id>/github-app
    Payload: {"app_id": "...", "private_key": "..."}

    Raises:
        ValueError: if credentials cannot be resolved.
    """
    import asyncio

    import boto3

    env = _get_environment()
    secret_id = f"adp/{env}/tenants/{org_id}/github-app"

    def _read_secret() -> dict[str, str]:
        client = sm_client or boto3.client(
            "secretsmanager",
            region_name=os.environ.get("AWS_REGION", "us-east-1"),
        )
        resp = client.get_secret_value(SecretId=secret_id)
        return json.loads(resp["SecretString"])

    try:
        creds = await asyncio.to_thread(_read_secret)
    except Exception as exc:
        logger.warning(
            "Failed to resolve GitHub App credentials for tenant %s at %s: %s",
            org_id,
            secret_id,
            exc,
        )
        # Issue #3266/#3358: Classify boto3 ClientError by response code for
        # actionable error messages; fall back to string matching for non-
        # ClientError exceptions (e.g. connection timeouts).
        error_code = _classify_boto3_error(exc)
        if error_code == "ResourceNotFoundException":
            raise ValueError(
                f"No GitHub App connection configured for tenant '{org_id}'. "
                f"Set up a connection via Settings → Connections, or register "
                f"repos covered by your organization's existing installation."
            ) from exc
        elif error_code == "AccessDeniedException":
            raise ValueError(
                f"Platform IAM policy does not permit reading credentials for "
                f"tenant '{org_id}'. This is a platform configuration issue — "
                f"please contact your administrator."
            ) from exc
        else:
            raise ValueError(f"Failed to resolve GitHub App credentials for tenant '{org_id}'. Please try again later.") from exc

    app_id = creds.get("app_id", "")
    private_key = creds.get("private_key", "")

    if not app_id or not private_key:
        raise ValueError(f"GitHub App credentials incomplete for tenant '{org_id}' (secret: {secret_id}). Missing app_id or private_key.")

    return app_id, private_key


# ---------------------------------------------------------------------------
# Installation token minting
# ---------------------------------------------------------------------------


async def mint_installation_token(
    app_id: str,
    private_key_pem: str,
    installation_id: int,
    *,
    http_client: httpx.AsyncClient | None = None,
) -> str:
    """Exchange App JWT for a short-lived installation access token.

    Returns the token string. Raises httpx.HTTPStatusError on failure.
    """
    jwt_token = _mint_app_jwt(app_id, private_key_pem)
    client = http_client or httpx.AsyncClient(
        base_url=GITHUB_API_BASE,
        headers={
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        timeout=10.0,
    )
    try:
        resp = await client.post(
            f"/app/installations/{installation_id}/access_tokens",
            headers={"Authorization": f"Bearer {jwt_token}"},
        )
        resp.raise_for_status()
        return resp.json().get("token", "")
    finally:
        if http_client is None:
            await client.aclose()


# ---------------------------------------------------------------------------
# List accessible repositories
# ---------------------------------------------------------------------------


async def list_accessible_repos(
    installation_id: int,
    app_id: str,
    private_key_pem: str,
    *,
    q: str | None = None,
    page: int = 1,
    page_size: int = 50,
    http_client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """List repos accessible to the GitHub App installation.

    Returns {"repos": [...], "total": int, "page": int, "has_more": bool}.

    The GitHub API returns all installation repos; we apply server-side search
    filtering on full_name (case-insensitive contains) and pagination.
    """
    token = await mint_installation_token(app_id, private_key_pem, installation_id, http_client=http_client)

    client = http_client or httpx.AsyncClient(
        base_url=GITHUB_API_BASE,
        headers={
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        timeout=15.0,
    )

    try:
        all_repos: list[dict[str, Any]] = []
        gh_page = 1
        while True:
            resp = await client.get(
                "/installation/repositories",
                headers={"Authorization": f"Bearer {token}"},
                params={"per_page": 100, "page": gh_page},
            )
            resp.raise_for_status()
            body = resp.json()
            repos = body.get("repositories", []) or []
            for repo in repos:
                all_repos.append(
                    {
                        "full_name": repo.get("full_name", ""),
                        "private": repo.get("private", False),
                        "url": repo.get("html_url", ""),
                    }
                )
            total_count = body.get("total_count")
            if not repos or (total_count is not None and len(all_repos) >= total_count) or len(repos) < 100:
                break
            gh_page += 1
    finally:
        if http_client is None:
            await client.aclose()

    # Apply search filter (case-insensitive contains on full_name)
    if q:
        q_lower = q.lower()
        filtered = [r for r in all_repos if q_lower in r["full_name"].lower()]
    else:
        filtered = all_repos

    total = len(filtered)

    # Apply pagination
    start = (page - 1) * page_size
    end = start + page_size
    page_repos = filtered[start:end]

    return {
        "repos": page_repos,
        "total": total,
        "page": page,
        "has_more": end < total,
    }


# ---------------------------------------------------------------------------
# Per-repo installation resolution (Issue #2086)
# ---------------------------------------------------------------------------


def _get_global_app_credentials() -> tuple[str, str]:
    """Return (app_id, private_key_pem) for the global ADP GitHub App.

    Reads BG_GITHUB_APP_ID / BG_GITHUB_APP_PRIVATE_KEY from gateway settings.
    """
    from src.shared.config import get_settings

    settings = get_settings()
    return settings.github_app_id or "", settings.github_app_private_key or ""


async def resolve_installation_for_repo(
    owner: str,
    repo: str,
    *,
    http_client: httpx.AsyncClient | None = None,
) -> int | None:
    """Resolve the GitHub App installation_id that has access to a specific repo.

    Uses the global ADP App JWT (BG_GITHUB_APP_ID / BG_GITHUB_APP_PRIVATE_KEY)
    to call GitHub's GET /repos/{owner}/{repo}/installation endpoint.

    Returns:
        The installation_id (int) on success, or None if the App is not
        installed on that repo (404).

    Raises:
        httpx.HTTPStatusError: on non-200/non-404 responses.
        ValueError: if global App credentials are not configured.
    """
    app_id, private_key = _get_global_app_credentials()

    if not app_id or not private_key:
        raise ValueError("Global GitHub App credentials not configured (BG_GITHUB_APP_ID / BG_GITHUB_APP_PRIVATE_KEY).")

    jwt_token = _mint_app_jwt(app_id, private_key)

    client = http_client or httpx.AsyncClient(
        base_url=GITHUB_API_BASE,
        headers={
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        timeout=10.0,
    )
    try:
        resp = await client.get(
            f"/repos/{owner}/{repo}/installation",
            headers={"Authorization": f"Bearer {jwt_token}"},
        )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return int(resp.json()["id"])
    finally:
        if http_client is None:
            await client.aclose()


# ---------------------------------------------------------------------------
# Tenant ownership check (Issue #2086)
# ---------------------------------------------------------------------------


async def verify_installation_ownership(
    tenant_id: str,
    installation_id: int,
    *,
    db: AsyncSession,
) -> bool:
    """Verify that an installation_id belongs to the given tenant.

    Queries channel_tenant_map using metadata->>'installation_id' to confirm
    the installation was registered under the caller's tenant. This closes the
    cross-tenant hole: the global App JWT can resolve ANY tenant's installation,
    so registration must verify the resolved installation belongs to the caller.

    Uses metadata JSON extraction (NOT provider_scope_id, which is the GitHub
    numeric account id). Handles both org and personal installs (both write
    channel_tenant_map rows).

    Returns:
        True if the installation belongs to the tenant, False otherwise.
    """
    result = await db.execute(
        text("""
            SELECT 1 FROM channel_tenant_map
            WHERE provider = 'github'
              AND org_id = :tenant_id
              AND metadata->>'installation_id' = :installation_id
        """),
        {"tenant_id": tenant_id, "installation_id": str(installation_id)},
    )
    return result.fetchone() is not None


# ---------------------------------------------------------------------------
# Membership-based installation ownership (Issue #3266)
# ---------------------------------------------------------------------------


async def check_membership_for_installation(
    caller_user_id: str,
    installation_id: int,
    *,
    db: AsyncSession,
) -> str | None:
    """Check if the caller is a member of any tenant that owns the installation.

    Issue #3266: Migrates Knowledge Layer registration onto the tenant-membership
    model (D5). When the per-tenant ownership check fails, this fallback resolves
    the caller's Cognito sub → users.id → tenant_memberships, and checks whether
    any of those tenants own the installation in channel_tenant_map.

    Returns:
        The owning tenant_id if the caller is a member of an org that owns the
        installation, or None if no membership match is found.
    """
    # Resolve Postgres users.id from Cognito sub (same pattern as connections/routes.py)
    user_result = await db.execute(
        text("SELECT id FROM users WHERE cognito_sub = :cognito_sub"),
        {"cognito_sub": caller_user_id},
    )
    user_row = user_result.fetchone()
    if user_row is None:
        return None

    pg_user_id = user_row[0]

    # Find all tenant_ids the user is a member of
    # Then check if any of those tenants own the installation
    result = await db.execute(
        text("""
            SELECT ctm.org_id
            FROM channel_tenant_map ctm
            INNER JOIN tenant_memberships tm
                ON tm.tenant_id = ctm.org_id
            WHERE ctm.provider = 'github'
              AND ctm.metadata->>'installation_id' = :installation_id
              AND tm.user_id = :pg_user_id
            LIMIT 1
        """),
        {"installation_id": str(installation_id), "pg_user_id": pg_user_id},
    )
    row = result.fetchone()
    return row[0] if row is not None else None
