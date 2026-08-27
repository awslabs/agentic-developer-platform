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


# Least-privilege default for agent runs (issue #4272). The GitHub App's full
# permission set is much broader; an agent run needs to read/write code, open and
# update pull requests, and comment on issues. Anything else (members, admin,
# secrets, workflows...) is deliberately withheld so a hijacked run's live token
# is narrower than the App that minted it.
AGENT_RUN_PERMISSIONS: dict[str, str] = {
    "contents": "write",
    "pull_requests": "write",
    "issues": "write",
    "checks": "write",
    "metadata": "read",
}


async def mint_installation_token_with_expiry(
    app_id: str,
    private_key_pem: str,
    installation_id: int,
    *,
    repositories: list[str] | None = None,
    permissions: dict[str, str] | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> tuple[str, str]:
    """Mint an installation access token and return ``(token, expires_at)``.

    Sibling of :func:`mint_installation_token`, which returns a bare string and
    discards GitHub's ``expires_at``. Callers that must schedule their own
    refresh (the agent worker's TokenManager, issue #4272) need the real expiry
    — a local ``now + 1h`` guess drifts and the run dies mid-flight. The
    existing function's signature is left alone because its caller in
    ``list_accessible_repos`` depends on the string return.

    ``repositories`` and ``permissions`` narrow the token below the
    installation's own grant (issue #4272 least-privilege note). GitHub already
    scopes a token to one org — one installation is one org — so this is
    defence-in-depth: a run assigned one repo gets a token good for that repo
    only, with only the verbs it needs. Omitting either falls back to GitHub's
    default (all repos / the App's full permission set), so callers acting on
    behalf of an agent run should always pass both.

    Returns:
        ``(token, expires_at)`` where ``expires_at`` is GitHub's own ISO-8601
        string, passed through verbatim.

    Raises:
        httpx.HTTPStatusError: on a non-2xx response from GitHub.
        ValueError: if GitHub returns a 2xx without a token or an expiry — a
            token we cannot schedule a refresh for is not usable, so this fails
            loudly rather than handing back a value that expires unpredictably.
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

    body: dict[str, Any] = {}
    if repositories:
        body["repositories"] = repositories
    if permissions:
        body["permissions"] = permissions

    try:
        resp = await client.post(
            f"/app/installations/{installation_id}/access_tokens",
            headers={"Authorization": f"Bearer {jwt_token}"},
            json=body,
        )
        resp.raise_for_status()
        payload = resp.json()
    finally:
        if http_client is None:
            await client.aclose()

    token = payload.get("token") or ""
    expires_at = payload.get("expires_at") or ""
    if not token:
        raise ValueError(f"GitHub returned no token for installation {installation_id}")
    if not expires_at:
        raise ValueError(f"GitHub returned no expires_at for installation {installation_id}; cannot schedule refresh")

    return token, expires_at


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
# Per-repo installation resolution (Issue #2086, #3529)
# ---------------------------------------------------------------------------

# Issue #3529: Ops-App secret names (Secrets Manager). The worker mints
# tokens with these credentials, so the resolver MUST use the same App to
# ensure the stored installation_id is valid for the worker.
_OPS_APP_ID_SECRET_ENV = "OPS_GITHUB_APP_ID_SECRET"
_OPS_APP_KEY_SECRET_ENV = "OPS_GITHUB_APP_KEY_SECRET"
# Defaults match the ingestion worker's config (GITHUB_APP_ID_SECRET /
# GITHUB_APP_KEY_SECRET) as deployed via the agent-context ConfigMap.
_OPS_APP_ID_SECRET_DEFAULT = "adp/aws-e/gh-app-ops-id"
_OPS_APP_KEY_SECRET_DEFAULT = "adp/aws-e/gh-app-ops-key"


def _get_global_app_credentials() -> tuple[str, str]:
    """Return (app_id, private_key_pem) for the global ADP GitHub App.

    Reads BG_GITHUB_APP_ID / BG_GITHUB_APP_PRIVATE_KEY from gateway settings.
    """
    from src.shared.config import get_settings

    settings = get_settings()
    return settings.github_app_id or "", settings.github_app_private_key or ""


async def _get_ops_app_credentials(
    *,
    sm_client: Any | None = None,
) -> tuple[str, str]:
    """Return (app_id, private_key_pem) for the ops GitHub App.

    Issue #3529: Reads credentials from Secrets Manager using the same secret
    names the ingestion worker uses (GITHUB_APP_ID_SECRET / GITHUB_APP_KEY_SECRET).
    This ensures the installation_id we resolve is for the SAME App the worker
    mints tokens with.

    Configurable via OPS_GITHUB_APP_ID_SECRET / OPS_GITHUB_APP_KEY_SECRET env vars.

    Raises:
        ValueError: if secrets cannot be read.
    """
    import asyncio

    import boto3

    app_id_secret = os.environ.get(_OPS_APP_ID_SECRET_ENV, _OPS_APP_ID_SECRET_DEFAULT)
    app_key_secret = os.environ.get(_OPS_APP_KEY_SECRET_ENV, _OPS_APP_KEY_SECRET_DEFAULT)

    def _read_secrets() -> tuple[str, str]:
        client = sm_client or boto3.client(
            "secretsmanager",
            region_name=os.environ.get("AWS_REGION", "us-east-1"),
        )
        app_id_resp = client.get_secret_value(SecretId=app_id_secret)
        app_key_resp = client.get_secret_value(SecretId=app_key_secret)
        return app_id_resp["SecretString"], app_key_resp["SecretString"]

    try:
        app_id, private_key = await asyncio.to_thread(_read_secrets)
    except Exception as exc:
        error_code = _classify_boto3_error(exc)
        if error_code == "AccessDeniedException":
            raise ValueError(
                f"Platform IAM policy does not permit reading ops App credentials "
                f"({app_id_secret}, {app_key_secret}). Run Gateway Infra Apply to "
                f"add the adp/*/gh-app-ops-* pattern."
            ) from exc
        raise ValueError(f"Failed to read ops App credentials from Secrets Manager ({app_id_secret}, {app_key_secret}): {exc}") from exc

    if not app_id or not private_key:
        raise ValueError(f"Ops App credentials empty (secrets: {app_id_secret}, {app_key_secret}).")

    return app_id.strip(), private_key.strip()


async def resolve_installation_for_repo(
    owner: str,
    repo: str,
    *,
    http_client: httpx.AsyncClient | None = None,
) -> int | None:
    """Resolve the GitHub App installation_id via the global/dev App.

    Uses BG_GITHUB_APP_ID credentials to call GET /repos/{owner}/{repo}/installation.
    The returned installation_id matches what channel_tenant_map stores (written by
    the UI connect flow using the same global App), so it is valid for Step 4
    ownership verification.

    NOTE: This id is NOT valid for the ingestion worker to mint tokens with if
    the worker uses a different App. Use resolve_worker_installation_for_repo()
    to get the id the worker can actually mint against.

    Returns:
        The installation_id (int) on success, or None if the App is not
        installed on that repo (404).

    Raises:
        httpx.HTTPStatusError: on non-200/non-404 responses.
        ValueError: if global App credentials are not configured.
    """
    app_id, private_key = _get_global_app_credentials()

    if not app_id or not private_key:
        raise ValueError("No GitHub App credentials available. Configure global App (BG_GITHUB_APP_ID / BG_GITHUB_APP_PRIVATE_KEY).")

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


async def resolve_worker_installation_for_repo(
    owner: str,
    repo: str,
    *,
    http_client: httpx.AsyncClient | None = None,
    sm_client: Any | None = None,
) -> int | None:
    """Resolve the ops-App installation_id that the ingestion worker can mint with.

    Issue #3529: Uses the ops App credentials (GITHUB_APP_ID_SECRET / GITHUB_APP_KEY_SECRET
    — the same App the ingestion worker mints tokens with) to call
    GET /repos/{owner}/{repo}/installation.

    The returned installation_id is what should be stored on knowledge_assets and
    passed to the worker via SQS — it is the ONLY id the worker can mint against.

    Called AFTER ownership verification passes (Step 4 uses resolve_installation_for_repo
    with the global App, since channel_tenant_map stores global-App installation ids).

    Returns:
        The installation_id (int) on success, or None if the ops App is not
        installed on that repo (404).

    Raises:
        httpx.HTTPStatusError: on non-200/non-404 responses.
        ValueError: if ops App credentials cannot be read (no silent fallback).
    """
    # No fallback — if ops creds can't be read, fail loudly. A silent fallback
    # would store a dev-App id the worker can't mint with, re-introducing the
    # exact queued-then-access_revoked failure this issue exists to kill.
    app_id, private_key = await _get_ops_app_credentials(sm_client=sm_client)

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

    Closes the cross-tenant hole: the global App JWT can resolve ANY tenant's
    installation, so registration must verify the resolved installation belongs
    to the caller.

    Issue #4070 (·A0): this is now a thin delegate to the canonical resolver in
    ``src.admin.installations.resolver``. It used to run its own
    ``metadata->>'installation_id'`` query, which made it a *second* notion of
    ownership alongside the admin/connections plane's — the divergence ·A0 exists
    to remove. Behaviour is preserved for existing callers (bool in, bool out),
    with two deliberate improvements that follow from the shared rule:

    * an installation claimed by more than one tenant now returns False for
      *everyone* (fail closed) instead of True for whoever asked first;
    * the lookup is a portable indexed-column comparison, so it works on both
      Postgres and the SQLite used by the test suite. The old ``->>`` was
      Postgres-only, which is why its own test could only assert on the SQL
      string rather than on the access decision.

    Read-path check: no GitHub call (``attest=False``). Writes that BIND an
    installation to a tenant should call ``assert_installation_owned_by`` with
    ``attest=True`` instead.

    Returns:
        True if the installation provably belongs to the tenant, False otherwise.
    """
    from src.admin.installations.resolver import (
        InstallationOwnershipError,
        assert_installation_owned_by,
    )

    try:
        await assert_installation_owned_by(tenant_id, installation_id, db=db)
    except InstallationOwnershipError:
        return False
    return True


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
